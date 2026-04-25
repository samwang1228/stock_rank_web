from __future__ import annotations

import datetime as dt
import random
import time
from typing import Callable

from stock_db import prune_to_last_n_days, upsert_bars, upsert_institution_trades
from tw_market_data import fetch_daily_bars, fetch_twse_institution_trades, iter_calendar_days_back


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

    def day_counts(d: dt.date) -> tuple[int, int] | None:
        row = conn.execute(
            "SELECT twse_count, tpex_count FROM day_meta WHERE date=?",
            (d.isoformat(),),
        ).fetchone()
        if not row:
            return None
        return int(row["twse_count"]), int(row["tpex_count"])

    def has_inst_trades(d: dt.date) -> bool:
        row = conn.execute(
            "SELECT 1 FROM inst_trades WHERE date=? AND market='TWSE' LIMIT 1",
            (d.isoformat(),),
        ).fetchone()
        return row is not None

    # 1) 往回掃描直到湊滿 keep_trading_days 個交易日
    collected: list[dt.date] = []
    for day in iter_calendar_days_back(end_date):
        if len(collected) >= keep_trading_days:
            break
        counts = day_counts(day)

        # 已完整存在（行情兩市場都非 0，且法人資料存在）就直接記一個交易日
        if counts is not None and counts[0] > 0 and counts[1] > 0 and has_inst_trades(day):
            collected.append(day)
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
            continue

        now_utc = dt.datetime.now(dt.timezone.utc)
        if twse:
            upsert_bars(conn, day, twse, fetched_at=now_utc)
        if tpex:
            upsert_bars(conn, day, tpex, fetched_at=now_utc)

        # 上市三大法人（T86）只需要 TWSE；若該日非交易日通常會回空
        time.sleep(request_delay + random.random() * 0.2)
        inst_trades = fetch_twse_institution_trades(day, cache_dir=cache_dir, use_cache=use_cache)
        if inst_trades:
            upsert_institution_trades(conn, day, inst_trades)

        collected.append(day)
        log(f"寫入：TWSE={len(twse)}、TPEX={len(tpex)}、T86={len(inst_trades)}")
        time.sleep(per_day_delay + random.random() * 0.5)

    # 只保留最後 keep_trading_days 個（asc）
    collected = sorted(set(collected))[-keep_trading_days:]
    if not collected:
        raise RuntimeError("無法取得交易日資料，請確認網路/資料源")

    start_date = collected[0]
    latest_date = collected[-1]
    log(f"回補完成：{start_date.isoformat()} ~ {latest_date.isoformat()}（{len(collected)} 交易日）")

    # 2) prune 舊資料（以交易日數為準）
    log(f"Prune：只保留最後 {keep_trading_days} 個交易日")
    prune_to_last_n_days(conn, keep_trading_days)
    log("同步流程結束")
    return latest_date
