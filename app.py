from __future__ import annotations

import datetime as dt
import os
import threading
import time
from dataclasses import dataclass

from flask import Flask, jsonify, redirect, render_template, request, url_for

from stock_db import (
    db_session,
    get_bars_for_code,
    get_last_n_bars_for_code,
    get_code_name,
    get_market_cap_meta,
    latest_trading_date,
    latest_market_cap_date,
    list_trading_dates,
    select_closes_for_dates,
    select_closes_for_last_n_trading_days,
    select_close_volume_for_dates,
    select_institution_net_buy_rank,
    select_institution_net_buy_rank_full,
    select_market_cap_top,
    upsert_market_caps,
    upsert_bars_partial,
)
from sync_data import sync_last_n_trading_days
from tw_market_data import fetch_twse_company_share_info


BASE_DIR = os.path.dirname(__file__)
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "stock.db")
DEFAULT_CACHE_DIR = os.path.join(BASE_DIR, ".cache")


@dataclass(frozen=True)
class ReturnRow:
    code: str
    name: str
    close: float
    returns: dict[int, float | None]


def _ma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def create_app() -> Flask:
    app = Flask(__name__)

    db_path = os.environ.get("STOCK_DB_PATH", DEFAULT_DB_PATH)
    cache_dir = os.environ.get("STOCK_CACHE_DIR", DEFAULT_CACHE_DIR)

    def get_db_latest_date() -> dt.date | None:
        with db_session(db_path) as conn:
            return latest_trading_date(conn)

    def sync_now() -> dt.date:
        with db_session(db_path) as conn:
            return sync_last_n_trading_days(
                conn,
                end_date=dt.date.today(),
                # 要算「60 個交易日前 -> 今日」漲幅，需要至少 61 個交易日資料
                keep_trading_days=61,
                cache_dir=cache_dir,
                use_cache=True,
                per_day_delay=float(os.environ.get("STOCK_FETCH_DELAY", "1.2")),
                on_log=_log,
            )

    sync_lock = threading.Lock()
    sync_state: dict[str, object] = {
        "running": False,
        "started_at": None,
        "finished_at": None,
        "latest": None,
        "error": None,
        "logs": [],
    }

    def _log(line: str) -> None:
        # keep last 500 lines
        logs: list[str] = sync_state["logs"]  # type: ignore[assignment]
        ts = dt.datetime.now().strftime("%H:%M:%S")
        logs.append(f"[{ts}] {line}")
        if len(logs) > 500:
            del logs[: len(logs) - 500]

    def _start_sync_background() -> None:
        with sync_lock:
            if bool(sync_state.get("running")):
                return
            sync_state["running"] = True
            sync_state["started_at"] = dt.datetime.now().isoformat(timespec="seconds")
            sync_state["finished_at"] = None
            sync_state["latest"] = None
            sync_state["error"] = None
            sync_state["logs"] = []
            _log("開始更新 DB…")

        def runner():
            t0 = time.time()
            try:
                _log("同步近 61 個交易日（含自動補齊/刪除舊資料）")
                latest = sync_now()
                _log(f"完成，同步至：{latest.isoformat()}")
                sync_state["latest"] = latest.isoformat()
            except Exception as e:  # noqa: BLE001
                _log(f"失敗：{e}")
                sync_state["error"] = str(e)
            finally:
                sync_state["running"] = False
                sync_state["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
                _log(f"總耗時 {time.time() - t0:.1f}s")

        threading.Thread(target=runner, daemon=True).start()

    def _volume_up_rank(conn, *, days: int, limit: int) -> tuple[list[dict[str, object]], list[dt.date], list[dt.date]]:
        windows = [1, 3, 5, 10, 15]
        if days not in windows:
            days = 5
        if limit <= 0:
            limit = 200

        used_desc = list_trading_dates(conn, limit=2 * days, desc=True)
        used_dates = list(reversed(used_desc))
        if len(used_dates) != 2 * days:
            return ([], [], [])

        older_dates = used_dates[:days]
        recent_dates = used_dates[days:]

        raw = select_close_volume_for_dates(conn, used_dates)

        older_set = {d.isoformat() for d in older_dates}
        recent_set = {d.isoformat() for d in recent_dates}

        agg: dict[str, dict[str, object]] = {}
        for r in raw:
            code = str(r["code"])
            d = agg.setdefault(
                code,
                {
                    "code": code,
                    "name": str(r["name"]),
                    "older": 0,
                    "recent": 0,
                    "older_n": 0,
                    "recent_n": 0,
                },
            )
            date_s = str(r["date"])
            vol = int(r["volume"])
            if date_s in older_set:
                d["older"] = int(d["older"]) + vol
                d["older_n"] = int(d["older_n"]) + 1
            elif date_s in recent_set:
                d["recent"] = int(d["recent"]) + vol
                d["recent_n"] = int(d["recent_n"]) + 1

        rows: list[dict[str, object]] = []
        for _, d in agg.items():
            if int(d["older_n"]) != days or int(d["recent_n"]) != days:
                continue
            older = int(d["older"])
            recent = int(d["recent"])
            if older <= 0:
                continue
            ratio = float(recent) / float(older)
            pct = (ratio - 1.0) * 100.0
            rows.append(
                {
                    "code": str(d["code"]),
                    "name": str(d["name"]),
                    "older": older,
                    "recent": recent,
                    "ratio": ratio,
                    "pct": pct,
                }
            )

        rows.sort(key=lambda x: (-float(x["ratio"]), str(x["code"])))
        return (rows[:limit], older_dates, recent_dates)

    def _turnover_rank(conn, *, days: int, limit: int) -> tuple[list[dict[str, object]], list[dt.date]]:
        windows = [1, 5, 10, 20, 30]
        if days not in windows:
            days = 5
        if limit <= 0:
            limit = 200

        used_desc = list_trading_dates(conn, limit=days, desc=True)
        used_dates = list(reversed(used_desc))
        if len(used_dates) != days:
            return ([], [])

        raw = select_close_volume_for_dates(conn, used_dates)

        # 聚合：code -> sum_close, sum_volume, count
        agg: dict[str, dict[str, object]] = {}
        for r in raw:
            code = str(r["code"])
            d = agg.setdefault(
                code,
                {
                    "code": code,
                    "name": str(r["name"]),
                    "sum_close": 0.0,
                    "sum_volume": 0,
                    "count": 0,
                },
            )
            d["sum_close"] = float(d["sum_close"]) + float(r["close"])
            d["sum_volume"] = int(d["sum_volume"]) + int(r["volume"])
            d["count"] = int(d["count"]) + 1

        rows: list[dict[str, object]] = []
        for code, d in agg.items():
            if int(d["count"]) != days:
                continue
            sum_close = float(d["sum_close"])
            sum_volume = int(d["sum_volume"])
            score = (sum_close * float(sum_volume)) / float(days)
            rows.append(
                {
                    "code": code,
                    "name": str(d["name"]),
                    "score": score,
                    "sum_close": sum_close,
                    "sum_volume": sum_volume,
                }
            )

        rows.sort(key=lambda x: (-float(x["score"]), str(x["code"])))
        return (rows[:limit], used_dates)

    @app.get("/")
    def index():
        return redirect(url_for("gainers"))

    @app.post("/sync")
    def sync():
        _start_sync_background()
        return redirect(url_for("sync_status"))

    @app.get("/sync/status")
    def sync_status():
        return render_template("sync_status.html")

    @app.get("/sync/state")
    def sync_state_api():
        # shallow copy for json
        payload = {
            "running": bool(sync_state.get("running")),
            "started_at": sync_state.get("started_at"),
            "finished_at": sync_state.get("finished_at"),
            "latest": sync_state.get("latest"),
            "error": sync_state.get("error"),
            "logs": list(sync_state.get("logs") or []),
        }
        return jsonify(payload)

    @app.post("/market-cap/update")
    def update_market_cap():
        """更新市值快照（獨立資料表；更新頻率較低）。"""

        asof = get_db_latest_date()
        if not asof:
            return redirect(url_for("market_cap", error="no_db"))

        with db_session(db_path) as conn:
            closes = select_closes_for_dates(conn, [asof])

            close_by_code: dict[str, float] = {}
            name_by_code: dict[str, str] = {}
            for r in closes:
                code = str(r["code"])
                close_by_code[code] = float(r["close"])
                name_by_code[code] = str(r["name"])

            company_info_date, share_rows = fetch_twse_company_share_info(cache_dir=cache_dir, use_cache=True)
            shares_by_code = {r.code: r.issued_shares for r in share_rows}
            share_name_by_code = {r.code: r.name for r in share_rows}

            rows: list[tuple[str, str, int, float, int]] = []
            for code, close in close_by_code.items():
                shares = shares_by_code.get(code)
                if not shares:
                    continue
                name = name_by_code.get(code) or share_name_by_code.get(code) or ""
                if not name:
                    continue
                mcap = int(round(float(close) * int(shares)))
                rows.append((code, name, int(shares), float(close), int(mcap)))

            now_utc = dt.datetime.now(dt.timezone.utc)
            upsert_market_caps(
                conn,
                asof_date=asof,
                company_info_date=company_info_date,
                rows=rows,
                fetched_at=now_utc,
            )

        return redirect(url_for("market_cap", updated="1"))

    @app.get("/market-cap")
    def market_cap():
        asof = get_db_latest_date()
        latest_cap = None
        error = request.args.get("error")
        updated = request.args.get("updated")
        limit = int(request.args.get("limit", "200"))
        if limit <= 0:
            limit = 200

        cap_rows = []
        meta = None
        if asof:
            with db_session(db_path) as conn:
                latest_cap = latest_market_cap_date(conn)
                use_asof = latest_cap or asof
                meta = get_market_cap_meta(conn, use_asof) if latest_cap else None
                if latest_cap:
                    cap_rows = select_market_cap_top(conn, asof_date=latest_cap, limit=limit)

        return render_template(
            "market_cap.html",
            asof=asof,
            latest_cap=latest_cap,
            meta=meta,
            rows=cap_rows,
            limit=limit,
            updated=updated,
            error=error,
        )

    @app.get("/gainers")
    def gainers():
        asof = get_db_latest_date()
        synced_flag = request.args.get("synced")

        sort_days = int(request.args.get("sort", "5"))
        windows = [5, 10, 20, 30, 40, 50, 60]
        if sort_days not in windows:
            sort_days = 5

        with db_session(db_path) as conn:
            trading_dates = list_trading_dates(conn, desc=False)
            if not trading_dates:
                return render_template(
                    "gainers.html",
                    asof=None,
                    windows=windows,
                    sort_days=sort_days,
                    rows=[],
                    synced_flag=synced_flag,
                )

            latest = trading_dates[-1]
            needed_dates = [latest]
            for w in windows:
                # 「w 個交易日前（只看交易日）」：比較 today vs trading_dates[-1-w]
                if len(trading_dates) >= (w + 1):
                    needed_dates.append(trading_dates[-1 - w])

            # 去重保持順序
            seen = set()
            needed_dates = [d for d in needed_dates if not (d in seen or seen.add(d))]

            rows = select_closes_for_dates(conn, needed_dates)

        # 組裝：每檔股票在各日期的收盤
        close_by_code_date: dict[str, dict[str, float]] = {}
        name_by_code: dict[str, str] = {}
        for r in rows:
            code = str(r["code"])
            name_by_code[code] = str(r["name"])
            close_by_code_date.setdefault(code, {})[str(r["date"])] = float(r["close"])

        latest_s = latest.isoformat()
        past_date_by_w = {}
        for w in windows:
            if len(trading_dates) >= (w + 1):
                past_date_by_w[w] = trading_dates[-1 - w].isoformat()

        table: list[ReturnRow] = []
        for code, closes in close_by_code_date.items():
            if latest_s not in closes:
                continue
            latest_close = closes[latest_s]

            returns: dict[int, float | None] = {}
            for w in windows:
                past_s = past_date_by_w.get(w)
                if not past_s or past_s not in closes:
                    returns[w] = None
                    continue
                past_close = closes[past_s]
                if past_close == 0:
                    returns[w] = None
                else:
                    returns[w] = (latest_close / past_close - 1.0) * 100.0

            table.append(
                ReturnRow(
                    code=code,
                    name=name_by_code.get(code, ""),
                    close=latest_close,
                    returns=returns,
                )
            )

        def sort_key_fn(r: ReturnRow):
            v = r.returns.get(sort_days)
            if v is None:
                return (1, 0.0, r.code)
            return (0, -float(v), r.code)

        table.sort(key=sort_key_fn)

        return render_template(
            "gainers.html",
            asof=asof,
            windows=windows,
            sort_days=sort_days,
            rows=table,
            synced_flag=synced_flag,
        )

    @app.get("/kline")
    def kline():
        asof = get_db_latest_date()
        code = (request.args.get("code") or "").strip()
        name = None
        plot_json = None

        if code:
            with db_session(db_path) as conn:
                bars = get_last_n_bars_for_code(conn, code, 60)
                name = get_code_name(conn, code)

                # Best-effort OTC (TPEX) backfill: only when we already know this
                # code is TPEX and DB bars are insufficient.
                if len(bars) < 60:
                    row = conn.execute(
                        "SELECT market FROM bars WHERE code=? ORDER BY date DESC LIMIT 1",
                        (code,),
                    ).fetchone()
                    is_tpex = bool(row and str(row["market"]) == "TPEX")
                    if is_tpex:
                        try:
                            from otc_backfill import backfill_otc_ohlcv

                            end_date = asof or dt.date.today()
                            # Roughly 1 year window to collect >= 60 trading bars.
                            start_date = end_date - dt.timedelta(days=365)
                            name_by_code = {code: name or ""}

                            grouped = backfill_otc_ohlcv(
                                [code],
                                start_date=start_date,
                                end_date=end_date,
                                name_by_code=name_by_code,
                                chunk_size=1,
                                chunk_delay=0.0,
                            )
                            extra = [b for day_bars in grouped.values() for b in day_bars]
                            if extra:
                                upsert_bars_partial(conn, extra)
                                bars = get_last_n_bars_for_code(conn, code, 60)
                                if not name:
                                    name = get_code_name(conn, code)
                        except Exception:
                            # If backfill fails (no internet, rate limit, missing deps),
                            # we still render whatever data we have.
                            pass

            if bars:
                import plotly.graph_objects as go
                from plotly.subplots import make_subplots

                x = [b.date for b in bars]
                o = [b.open for b in bars]
                h = [b.high for b in bars]
                l = [b.low for b in bars]
                c = [b.close for b in bars]
                v = [b.volume for b in bars]

                def ma_line(window: int):
                    out = []
                    for i in range(len(c)):
                        seg = c[: i + 1]
                        m = _ma(seg, window)
                        out.append(m)
                    return out

                ma5 = ma_line(5)
                ma10 = ma_line(10)
                ma20 = ma_line(20)
                ma60 = ma_line(60)

                fig = make_subplots(
                    rows=2,
                    cols=1,
                    shared_xaxes=True,
                    vertical_spacing=0.06,
                    row_heights=[0.7, 0.3],
                )

                fig.add_trace(
                    go.Candlestick(
                        x=x,
                        open=o,
                        high=h,
                        low=l,
                        close=c,
                        name="日K",
                        increasing_line_color="red",
                        increasing_fillcolor="red",
                        decreasing_line_color="green",
                        decreasing_fillcolor="green",
                    ),
                    row=1,
                    col=1,
                )
                fig.add_trace(go.Scatter(x=x, y=ma5, mode="lines", name="MA5"), row=1, col=1)
                fig.add_trace(go.Scatter(x=x, y=ma10, mode="lines", name="MA10"), row=1, col=1)
                fig.add_trace(go.Scatter(x=x, y=ma20, mode="lines", name="MA20"), row=1, col=1)
                fig.add_trace(go.Scatter(x=x, y=ma60, mode="lines", name="MA60"), row=1, col=1)

                # 量：上漲紅、下跌綠
                colors = ["red" if c[i] >= o[i] else "green" for i in range(len(c))]
                fig.add_trace(go.Bar(x=x, y=v, marker_color=colors, name="成交量"), row=2, col=1)

                fig.update_layout(
                    xaxis_rangeslider_visible=False,
                    height=700,
                    margin=dict(l=40, r=20, t=40, b=30),
                    legend=dict(orientation="h"),
                )

                plot_json = fig.to_json()

        return render_template("kline.html", asof=asof, code=code, name=name, plot_json=plot_json)

    @app.get("/near-ma")
    def near_ma():
        asof = get_db_latest_date()
        sort_key = request.args.get("sort", "d20")
        threshold_pct = float(request.args.get("threshold", "1.0"))

        # 我們固定這三個 MA
        windows = [5, 20, 60]

        with db_session(db_path) as conn:
            rows = select_closes_for_last_n_trading_days(conn, 60)

        if not rows:
            return render_template(
                "near_ma.html",
                asof=None,
                threshold_pct=threshold_pct,
                rows=[],
                sort_key=sort_key,
            )

        # code -> closes (by date asc)
        closes_by_code: dict[str, list[float]] = {}
        name_by_code: dict[str, str] = {}
        for r in rows:
            code = str(r["code"])
            name_by_code[code] = str(r["name"])
            closes_by_code.setdefault(code, []).append(float(r["close"]))

        table = []
        for code, closes in closes_by_code.items():
            if not closes:
                continue
            close = closes[-1]

            ma = {w: _ma(closes, w) for w in windows}
            dist = {}
            for w in windows:
                if ma[w] is None or ma[w] == 0:
                    dist[w] = None
                else:
                    dist[w] = abs(close - float(ma[w])) / float(ma[w]) * 100.0

            # 任一個 MA 距離 <= threshold 就列出
            if not any((dist[w] is not None and dist[w] <= threshold_pct) for w in windows):
                continue

            table.append(
                {
                    "code": code,
                    "name": name_by_code.get(code, ""),
                    "close": close,
                    "ma5": ma[5],
                    "ma20": ma[20],
                    "ma60": ma[60],
                    "d5": dist[5],
                    "d20": dist[20],
                    "d60": dist[60],
                }
            )

        valid_sort = {"code", "close", "d5", "d20", "d60"}
        if sort_key not in valid_sort:
            sort_key = "d20"

        def key_fn(r):
            v = r.get(sort_key)
            if v is None:
                return (1, 0)
            if sort_key in {"d5", "d20", "d60"}:
                return (0, float(v))  # 距離越小越前
            if sort_key == "close":
                return (0, -float(v))
            return (0, str(v))

        table.sort(key=key_fn)

        return render_template(
            "near_ma.html",
            asof=asof,
            threshold_pct=threshold_pct,
            rows=table,
            sort_key=sort_key,
        )

    @app.get("/turnover")
    def turnover():
        """成交值排行。

        公式（依使用者定義）：
        score = (近 N 日收盤價加總 × 近 N 日成交量加總) / N
        """

        asof = get_db_latest_date()
        days = int(request.args.get("days", "5"))
        windows = [1, 5, 10, 20, 30]
        if days not in windows:
            days = 5

        limit = int(request.args.get("limit", "200"))
        if limit <= 0:
            limit = 200

        if not asof:
            return render_template(
                "turnover.html",
                title="成交值排行",
                asof=None,
                days=days,
                windows=windows,
                used_dates=[],
                rows=[],
                limit=limit,
            )

        with db_session(db_path) as conn:
            rows, used_dates = _turnover_rank(conn, days=days, limit=limit)

        return render_template(
            "turnover.html",
            title="成交值排行",
            asof=asof,
            days=days,
            windows=windows,
            used_dates=used_dates,
            rows=rows,
            limit=limit,
        )

    @app.get("/vol-up")
    def vol_up():
        asof = get_db_latest_date()
        days = int(request.args.get("days", "5"))
        windows = [1, 3, 5, 10, 15]
        if days not in windows:
            days = 5

        limit = int(request.args.get("limit", "200"))
        if limit <= 0:
            limit = 200

        if not asof:
            return render_template(
                "vol_up.html",
                title="量能提升排行",
                asof=None,
                days=days,
                windows=windows,
                older_dates=[],
                recent_dates=[],
                rows=[],
                limit=limit,
            )

        with db_session(db_path) as conn:
            rows, older_dates, recent_dates = _volume_up_rank(conn, days=days, limit=limit)

        return render_template(
            "vol_up.html",
            title="量能提升排行",
            asof=asof,
            days=days,
            windows=windows,
            older_dates=older_dates,
            recent_dates=recent_dates,
            rows=rows,
            limit=limit,
        )

    @app.get("/inst")
    def inst_rank():
        """上市三大法人買超排行（依日資料彙總）。"""

        asof = get_db_latest_date()
        days = int(request.args.get("days", "1"))
        inst = (request.args.get("inst", "foreign") or "foreign").strip().lower()

        windows = [1, 5, 20, 30]
        inst_options = [
            ("foreign", "外資"),
            ("trust", "投信"),
            ("dealer", "自營商"),
            ("total", "三大合計"),
        ]
        inst_allowed = {k for k, _ in inst_options}

        if days not in windows:
            days = 1
        if inst not in inst_allowed:
            inst = "foreign"

        with db_session(db_path) as conn:
            raw_rows, used_dates = select_institution_net_buy_rank(conn, days=days, inst=inst, limit=200)

        rows = [
            {
                "code": str(r["code"]),
                "name": str(r["name"]),
                "net": int(r["net"]),
            }
            for r in raw_rows
        ]

        return render_template(
            "inst.html",
            asof=asof,
            days=days,
            inst=inst,
            windows=windows,
            inst_options=inst_options,
            used_dates=used_dates,
            rows=rows,
        )

    @app.get("/inst-otc")
    def inst_rank_otc():
        """上櫃三大法人買超排行（依日資料彙總）。"""

        asof = get_db_latest_date()
        days = int(request.args.get("days", "1"))
        inst = (request.args.get("inst", "foreign") or "foreign").strip().lower()

        windows = [1, 5, 20, 30]
        inst_options = [
            ("foreign", "外資"),
            ("trust", "投信"),
            ("dealer", "自營商"),
            ("total", "三大合計"),
            ("foreign_trust", "外資+投信"),
        ]
        inst_allowed = {k for k, _ in inst_options}

        if days not in windows:
            days = 1
        if inst not in inst_allowed:
            inst = "foreign"

        with db_session(db_path) as conn:
            raw_rows, used_dates = select_institution_net_buy_rank_full(conn, days=days, inst=inst, market="TPEX", limit=200)

        rows = [
            {
                "code": str(r["code"]),
                "name": str(r["name"]),
                "foreign_net": int(r["foreign_net"]),
                "trust_net": int(r["trust_net"]),
                "dealer_net": int(r["dealer_net"]),
                "total_net": int(r["total_net"]),
                "sort_net": int(r["sort_net"]),
            }
            for r in raw_rows
        ]

        return render_template(
            "inst_otc.html",
            asof=asof,
            days=days,
            inst=inst,
            windows=windows,
            inst_options=inst_options,
            used_dates=used_dates,
            rows=rows,
        )

    @app.get("/strategy")
    def strategy():
        """三條件 AND 篩選；空白代表不採用該條件。"""

        asof = get_db_latest_date()

        def to_int(value: str | None) -> int | None:
            if value is None:
                return None
            s = str(value).strip()
            if not s:
                return None
            try:
                v = int(s)
            except ValueError:
                return None
            return v if v > 0 else None

        n_days = to_int(request.args.get("n_days"))
        n_topk = to_int(request.args.get("n_topk"))
        m_days = to_int(request.args.get("m_days"))
        m_topk = to_int(request.args.get("m_topk"))
        inst = (request.args.get("inst") or "").strip().lower() or None
        cap_topl = to_int(request.args.get("cap_topl"))
        v_days = to_int(request.args.get("v_days"))
        v_topk = to_int(request.args.get("v_topk"))
        t_days = to_int(request.args.get("t_days"))
        t_topk = to_int(request.args.get("t_topk"))

        vol_windows = [1, 3, 5, 10, 15]
        if v_days is not None and v_days not in vol_windows:
            v_days = None

        turn_windows = [1, 5, 10, 20, 30]
        if t_days is not None and t_days not in turn_windows:
            t_days = None

        inst_options = [
            ("foreign", "外資"),
            ("trust", "投信"),
            ("dealer", "自營商"),
            ("foreign_trust", "外資+投信"),
        ]
        inst_allowed = {k for k, _ in inst_options}
        if inst is not None and inst not in inst_allowed:
            inst = None

        results: list[dict[str, object]] = []
        used = {
            "returns": bool(n_days and n_topk and asof),
            "turn": bool(t_days and t_topk and asof),
            "vol": bool(v_days and v_topk and asof),
            "inst": bool(m_days and m_topk and inst and asof),
            "cap": bool(cap_topl),
        }

        if not any(used.values()):
            return render_template(
                "strategy.html",
                asof=asof,
                n_days=n_days,
                n_topk=n_topk,
                m_days=m_days,
                m_topk=m_topk,
                inst=inst,
                cap_topl=cap_topl,
                v_days=v_days,
                v_topk=v_topk,
                vol_windows=vol_windows,
                t_days=t_days,
                t_topk=t_topk,
                turn_windows=turn_windows,
                inst_options=inst_options,
                used=used,
                rows=[],
                note="請至少選一個條件（漲幅 / 成交值 / 量能提升 / 法人買超 / 市值）。",
            )

        with db_session(db_path) as conn:
            code_set: set[str] | None = None
            metrics: dict[str, dict[str, object]] = {}
            hard_fail = False

            # 1) 漲幅 TopK（N 交易日）
            if used["returns"] and n_days and n_topk and asof:
                trading_dates = list_trading_dates(conn, desc=False)
                if not trading_dates or len(trading_dates) < (n_days + 1):
                    hard_fail = True
                else:
                    latest = trading_dates[-1]
                    past = trading_dates[-1 - n_days]
                    rows2 = select_closes_for_dates(conn, [latest, past])

                    close_by_code_date: dict[str, dict[str, float]] = {}
                    name_by_code: dict[str, str] = {}
                    for r in rows2:
                        code = str(r["code"])
                        name_by_code[code] = str(r["name"])
                        close_by_code_date.setdefault(code, {})[str(r["date"])] = float(r["close"])

                    latest_s = latest.isoformat()
                    past_s = past.isoformat()
                    rank = []
                    for code, closes in close_by_code_date.items():
                        if latest_s not in closes or past_s not in closes:
                            continue
                        past_close = float(closes[past_s])
                        if past_close == 0:
                            continue
                        latest_close = float(closes[latest_s])
                        ret = (latest_close / past_close - 1.0) * 100.0
                        rank.append((ret, code, name_by_code.get(code, "")))
                    rank.sort(key=lambda x: (-float(x[0]), x[1]))
                    rank = rank[:n_topk]

                    codes = {c for _, c, _ in rank}
                    code_set = codes if code_set is None else (code_set & codes)
                    for ret, code, name in rank:
                        metrics.setdefault(code, {})["name"] = name
                        metrics.setdefault(code, {})["ret_pct"] = float(ret)

            # 2) 成交量提升 TopK（V 天；近 2V 交易日）
            if used["vol"] and v_days and v_topk and asof:
                vol_rows, _older_dates, _recent_dates = _volume_up_rank(conn, days=v_days, limit=v_topk)
                codes = {str(r["code"]) for r in vol_rows}
                code_set = codes if code_set is None else (code_set & codes)
                for r in vol_rows:
                    code = str(r["code"])
                    metrics.setdefault(code, {})["name"] = str(r.get("name") or "")
                    metrics.setdefault(code, {})["vol_ratio"] = float(r["ratio"])
                    metrics.setdefault(code, {})["vol_pct"] = float(r["pct"])

            # 3) 成交值 TopK（T 交易日）
            if used["turn"] and t_days and t_topk and asof:
                turn_rows, _used_dates2 = _turnover_rank(conn, days=t_days, limit=t_topk)
                codes = {str(r["code"]) for r in turn_rows}
                code_set = codes if code_set is None else (code_set & codes)
                for r in turn_rows:
                    code = str(r["code"])
                    metrics.setdefault(code, {})["name"] = str(r.get("name") or "")
                    metrics.setdefault(code, {})["turn_score"] = float(r["score"])

            # 4) 法人買超 TopK（M 交易日）
            if used["inst"] and m_days and m_topk and inst:
                raw, _used_dates = select_institution_net_buy_rank(conn, days=m_days, inst=inst, limit=m_topk)
                codes = {str(r["code"]) for r in raw}
                code_set = codes if code_set is None else (code_set & codes)
                for r in raw:
                    code = str(r["code"])
                    metrics.setdefault(code, {})["name"] = str(r["name"])
                    metrics.setdefault(code, {})["inst_net"] = int(r["net"])

            # 5) 市值 TopL
            if used["cap"] and cap_topl:
                cap_date = latest_market_cap_date(conn)
                if not cap_date:
                    hard_fail = True
                else:
                    cap_raw = select_market_cap_top(conn, asof_date=cap_date, limit=cap_topl)
                    codes = {str(r["code"]) for r in cap_raw}
                    code_set = codes if code_set is None else (code_set & codes)
                    for r in cap_raw:
                        code = str(r["code"])
                        metrics.setdefault(code, {})["name"] = str(r["name"])
                        metrics.setdefault(code, {})["mcap"] = int(r["market_cap"])

            if hard_fail:
                results = []
            elif not code_set:
                results = []
            else:
                for code in sorted(code_set):
                    item = {"code": code}
                    item.update(metrics.get(code, {}))
                    results.append(item)

        note = None
        if used["cap"] and cap_topl and not results:
            note = "你有設定市值條件，但尚未有市值快照；請先按上方『更新市值』。"
        if used["returns"] and n_days and n_topk and not results and asof is not None:
            # 可能是交易日資料不足
            note = note or "你有設定漲幅條件，但 DB 交易日資料不足；請先按上方『更新DB』補齊。"
        if used["vol"] and v_days and v_topk and not results and asof is not None:
            note = note or "你有設定量能提升條件，但近 2V 個交易日成交量資料可能不足；請先按上方『更新DB』補齊。"
        if used["turn"] and t_days and t_topk and not results and asof is not None:
            note = note or "你有設定成交值條件，但近 T 個交易日資料可能不足；請先按上方『更新DB』補齊。"

        return render_template(
            "strategy.html",
            asof=asof,
            n_days=n_days,
            n_topk=n_topk,
            m_days=m_days,
            m_topk=m_topk,
            inst=inst,
            cap_topl=cap_topl,
            v_days=v_days,
            v_topk=v_topk,
            vol_windows=vol_windows,
            t_days=t_days,
            t_topk=t_topk,
            turn_windows=turn_windows,
            inst_options=inst_options,
            used=used,
            rows=results,
            note=note,
        )

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5001")), debug=True)
