from __future__ import annotations

import datetime as dt
import random
import time
from typing import Callable

from stock_db import prune_to_last_n_days, upsert_bars, upsert_institution_trades
from tw_market_data import (
    fetch_daily_bars,
    fetch_tpex_daily_bars,
    fetch_tpex_institution_trades,
    fetch_twse_daily_bars,
    fetch_twse_institution_trades,
    iter_calendar_days_back,
)


def sync_last_n_trading_days(
    conn,
    *,
    end_date: dt.date | None = None,
    keep_trading_days: int = 60,
    cache_dir: str,
    use_cache: bool = True,
    per_day_delay: float = 1.2,
    on_log: Callable[[str], None] | None = None,
) -> dt.date:
    """確保 DB 內有最近 keep_trading_days 個交易日資料；缺的會自動補齊（增量）。"""

    if end_date is None:
        end_date = dt.date.today()

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    # 清除誤存的週末資料（台股不在週末交易）
    log("清理 DB：刪除週六/週日的資料（若有）")
    with conn:
        conn.execute("DELETE FROM bars WHERE strftime('%w', date) IN ('0','6')")
        conn.execute("DELETE FROM day_meta WHERE strftime('%w', date) IN ('0','6')")
        conn.execute("DELETE FROM inst_trades WHERE strftime('%w', date) IN ('0','6')")

    # 清除「疑似休市日誤寫」：TWSE=0 但 TPEX>0。
    # 實務上台股休市多半兩市場同步；若出現 TPEX-only，通常是 TPEx 端點回前一交易日資料造成。
    log("清理 DB：刪除 TWSE=0 且 TPEX>0 的日期（若有）")
    with conn:
        bad_dates = [
            r["date"]
            for r in conn.execute(
                "SELECT date FROM day_meta WHERE twse_count=0 AND tpex_count>0"
            ).fetchall()
        ]
        for ds in bad_dates:
            conn.execute("DELETE FROM bars WHERE date=?", (ds,))
            conn.execute("DELETE FROM inst_trades WHERE date=?", (ds,))
            conn.execute("DELETE FROM day_meta WHERE date=?", (ds,))

    def day_counts(d: dt.date) -> tuple[int, int] | None:
        row = conn.execute(
            "SELECT twse_count, tpex_count FROM day_meta WHERE date=?",
            (d.isoformat(),),
        ).fetchone()
        if not row:
            return None
        return int(row["twse_count"]), int(row["tpex_count"])

    def has_inst_trades(d: dt.date, *, market: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM inst_trades WHERE date=? AND market=? LIMIT 1",
            (d.isoformat(), str(market).upper()),
        ).fetchone()
        return row is not None

    def inst_attempted(d: dt.date, *, market: str) -> bool:
        market_u = str(market).upper()
        row = conn.execute(
            "SELECT 1 FROM inst_fetch_meta WHERE date=? AND market=? LIMIT 1",
            (d.isoformat(), market_u),
        ).fetchone()
        if row is not None:
            return True
        # Backward compatibility: older DB only had TWSE attempt tracking.
        if market_u == "TWSE":
            row2 = conn.execute(
                "SELECT 1 FROM inst_meta WHERE date=? LIMIT 1",
                (d.isoformat(),),
            ).fetchone()
            return row2 is not None
        return False

    def mark_inst_attempt(d: dt.date, *, market: str, count: int) -> None:
        now_utc = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with conn:
            conn.execute(
                "INSERT INTO inst_fetch_meta(date, market, fetched_at, count) VALUES(?,?,?,?) "
                "ON CONFLICT(date, market) DO UPDATE SET fetched_at=excluded.fetched_at, count=excluded.count",
                (d.isoformat(), str(market).upper(), now_utc, int(count)),
            )

    def is_known_non_trading(d: dt.date) -> bool:
        row = conn.execute(
            "SELECT 1 FROM non_trading_days WHERE date=? LIMIT 1",
            (d.isoformat(),),
        ).fetchone()
        return row is not None

    def mark_non_trading(d: dt.date, *, reason: str) -> None:
        now_utc = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with conn:
            conn.execute(
                "INSERT INTO non_trading_days(date, reason, fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET reason=excluded.reason, fetched_at=excluded.fetched_at",
                (d.isoformat(), str(reason), now_utc),
            )

    # 1) 往回掃描直到湊滿 keep_trading_days 個交易日
    collected: list[dt.date] = []
    for day in iter_calendar_days_back(end_date):
        if len(collected) >= keep_trading_days:
            break

        # 週末直接略過
        if day.weekday() >= 5:
            continue

        # 已知休市日直接略過（避免每次都打 request）
        if is_known_non_trading(day):
            continue

        counts = day_counts(day)

        # 兩個市場都齊全時才直接略過。
        if counts is not None and counts[0] > 0 and counts[1] > 0:
            collected.append(day)

            # 法人資料只針對「最新幾天」補齊，且只嘗試一次（避免每次 sync 都重抓同一天）
            if len(collected) <= 5:
                if not has_inst_trades(day, market="TWSE") and not inst_attempted(day, market="TWSE"):
                    log(f"補抓法人：{day.isoformat()}（TWSE T86）")
                    inst_trades = fetch_twse_institution_trades(day, cache_dir=cache_dir, use_cache=use_cache)
                    if inst_trades:
                        upsert_institution_trades(conn, day, inst_trades)
                    mark_inst_attempt(day, market="TWSE", count=len(inst_trades))

                if not has_inst_trades(day, market="TPEX") and not inst_attempted(day, market="TPEX"):
                    log(f"補抓法人：{day.isoformat()}（TPEX 3inst）")
                    tpex_trades = fetch_tpex_institution_trades(day, cache_dir=cache_dir, use_cache=use_cache)
                    if tpex_trades:
                        upsert_institution_trades(conn, day, tpex_trades)
                    mark_inst_attempt(day, market="TPEX", count=len(tpex_trades))
            continue

        # 若已有其中一邊的行情資料，但另一邊缺漏，補抓缺的市場。
        if counts is not None:
            if counts[0] <= 0:
                log(f"補抓行情：{day.isoformat()}（TWSE）")
                twse = fetch_twse_daily_bars(day, cache_dir=cache_dir, use_cache=use_cache)
                if twse:
                    upsert_bars(conn, day, twse, fetched_at=dt.datetime.now(dt.timezone.utc))

            if counts[1] <= 0:
                log(f"補抓行情：{day.isoformat()}（TPEX）")
                tpex = fetch_tpex_daily_bars(day, cache_dir=cache_dir, use_cache=use_cache)
                if tpex:
                    upsert_bars(conn, day, tpex, fetched_at=dt.datetime.now(dt.timezone.utc))

            counts_after = day_counts(day)
            if counts_after is not None and counts_after[0] > 0 and counts_after[1] > 0:
                collected.append(day)
                log(f"寫入補缺：TWSE={counts_after[0]}、TPEX={counts_after[1]}")

                if len(collected) <= 5:
                    if not has_inst_trades(day, market="TWSE") and not inst_attempted(day, market="TWSE"):
                        log(f"補抓法人：{day.isoformat()}（TWSE T86）")
                        inst_trades = fetch_twse_institution_trades(day, cache_dir=cache_dir, use_cache=use_cache)
                        if inst_trades:
                            upsert_institution_trades(conn, day, inst_trades)
                        mark_inst_attempt(day, market="TWSE", count=len(inst_trades))

                    if not has_inst_trades(day, market="TPEX") and not inst_attempted(day, market="TPEX"):
                        log(f"補抓法人：{day.isoformat()}（TPEX 3inst）")
                        tpex_trades = fetch_tpex_institution_trades(day, cache_dir=cache_dir, use_cache=use_cache)
                        if tpex_trades:
                            upsert_institution_trades(conn, day, tpex_trades)
                        mark_inst_attempt(day, market="TPEX", count=len(tpex_trades))

            continue

        # 否則抓一次該日資料，補齊缺漏
        log(f"抓取：{day.isoformat()}（{len(collected) + 1}/{keep_trading_days} 交易日）")
        request_delay = 0.4
        twse, tpex = fetch_daily_bars(
            day,
            cache_dir=cache_dir,
            use_cache=use_cache,
            per_request_delay=request_delay,
        )

        if not twse and not tpex:
            mark_non_trading(day, reason="no_data")
            continue

        now_utc = dt.datetime.now(dt.timezone.utc)
        if twse:
            upsert_bars(conn, day, twse, fetched_at=now_utc)
        if tpex:
            upsert_bars(conn, day, tpex, fetched_at=now_utc)

        # 法人資料（TWSE T86 / TPEX 3inst）：若該日非交易日通常會回空
        time.sleep(request_delay + random.random() * 0.2)
        inst_twse = []
        inst_tpex = []
        if twse and not inst_attempted(day, market="TWSE"):
            inst_twse = fetch_twse_institution_trades(day, cache_dir=cache_dir, use_cache=use_cache)
            if inst_twse:
                upsert_institution_trades(conn, day, inst_twse)
            mark_inst_attempt(day, market="TWSE", count=len(inst_twse))

        if tpex and not inst_attempted(day, market="TPEX"):
            inst_tpex = fetch_tpex_institution_trades(day, cache_dir=cache_dir, use_cache=use_cache)
            if inst_tpex:
                upsert_institution_trades(conn, day, inst_tpex)
            mark_inst_attempt(day, market="TPEX", count=len(inst_tpex))

        counts_after = day_counts(day)
        if counts_after is None or (counts_after[0] <= 0 and counts_after[1] <= 0):
            continue

        # 額外保守：若出現 TPEX-only（TWSE=0, TPEX>0），視為誤寫，刪除並跳過
        if counts_after[0] <= 0 and counts_after[1] > 0:
            log(f"跳過：{day.isoformat()}（疑似休市日誤寫：TWSE=0, TPEX={counts_after[1]}）")
            with conn:
                conn.execute("DELETE FROM bars WHERE date=?", (day.isoformat(),))
                conn.execute("DELETE FROM inst_trades WHERE date=?", (day.isoformat(),))
                conn.execute("DELETE FROM day_meta WHERE date=?", (day.isoformat(),))
            mark_non_trading(day, reason="tpex_only")
            continue

        collected.append(day)
        log(f"寫入：TWSE={counts_after[0]}、TPEX={counts_after[1]}、T86={len(inst_twse)}、TPEX3I={len(inst_tpex)}")
        time.sleep(per_day_delay + random.random() * 0.5)

    # 只保留最後 keep_trading_days 個（asc）
    collected = sorted(set(collected))[-keep_trading_days:]
    if not collected:
        raise RuntimeError("無法取得交易日資料，請確認網路/資料源")

    start_date = collected[0]
    latest_date = collected[-1]
    log(f"回補完成：{start_date.isoformat()} ~ {latest_date.isoformat()}（{len(collected)} 交易日）")

    # 若上櫃歷史資料仍明顯不足，使用 yfinance 對最近區間做一次批次回補。
    # 這是主要的歷史補齊來源；TPEx 的日線端點對過去日期並不穩定。
    tpex_counts = []
    for day in collected:
        counts = day_counts(day)
        tpex_counts.append(0 if counts is None else int(counts[1]))

    if tpex_counts and min(tpex_counts) <= 1:
        log("偵測到上櫃歷史資料偏少，啟動 yfinance 批次回補")
        try:
            from otc_backfill import backfill_otc_ohlcv
            from tw_market_data import fetch_tpex_latest_snapshot

            snapshot_date, snapshot_bars = fetch_tpex_latest_snapshot(cache_dir=cache_dir, use_cache=use_cache)
            codes = sorted({bar.code for bar in snapshot_bars})
            name_by_code = {bar.code: bar.name for bar in snapshot_bars}
            if codes:
                log(f"TPEX 批次回補：{snapshot_date.isoformat()}，共 {len(codes)} 檔")
                grouped = backfill_otc_ohlcv(
                    codes,
                    start_date=start_date,
                    end_date=latest_date,
                    name_by_code=name_by_code,
                    chunk_size=80,
                    chunk_delay=0.8,
                )
                if grouped:
                    now_utc = dt.datetime.now(dt.timezone.utc)
                    for day, day_bars in grouped.items():
                        if day_bars:
                            upsert_bars(conn, day, day_bars, fetched_at=now_utc)
                    log(f"TPEX 批次回補完成：{len(grouped)} 個交易日")
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            log(f"TPEX 批次回補失敗：{exc}")

    # 2) prune 舊資料（以交易日數為準）
    log(f"Prune：只保留最後 {keep_trading_days} 個交易日")
    prune_to_last_n_days(conn, keep_trading_days)
    log("同步流程結束")
    return latest_date
