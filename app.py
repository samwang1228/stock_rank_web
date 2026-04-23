from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

from flask import Flask, redirect, render_template, request, url_for

from stock_db import (
    db_session,
    get_bars_for_code,
    get_code_name,
    latest_trading_date,
    list_trading_dates,
    select_closes_for_dates,
    select_closes_for_last_n_trading_days,
)
from sync_data import sync_last_n_trading_days


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
                keep_trading_days=60,
                cache_dir=cache_dir,
                use_cache=True,
                per_day_delay=float(os.environ.get("STOCK_FETCH_DELAY", "1.2")),
            )

    @app.get("/")
    def index():
        return redirect(url_for("gainers"))

    @app.post("/sync")
    def sync():
        latest = sync_now()
        ref = request.headers.get("Referer")
        # 盡量回到來源頁，否則回排行榜
        return redirect(ref or url_for("gainers", synced="1", asof=latest.isoformat()))

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
                dates = list_trading_dates(conn, desc=True)[:60]
                dates.sort()
                bars = get_bars_for_code(conn, code, dates=dates)
                name = get_code_name(conn, code)

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

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5001")), debug=True)
