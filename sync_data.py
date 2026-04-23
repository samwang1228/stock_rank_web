from __future__ import annotations

import datetime as dt
import os
import random
import time

from stock_db import prune_to_last_n_days, upsert_bars
from otc_backfill import backfill_otc_ohlcv
from tw_market_data import fetch_tpex_latest_snapshot, fetch_twse_daily_bars, iter_calendar_days_back


def sync_last_n_trading_days(
    conn,
    *,
    end_date: dt.date | None = None,
    keep_trading_days: int = 60,
    cache_dir: str,
    use_cache: bool = True,
    per_day_delay: float = 1.2,
) -> dt.date:
    """確保 DB 內有最近 keep_trading_days 個交易日資料；缺的會自動補齊。

    重要：
    - TWSE（上市）用官方「每日收盤行情(全部)」可回補歷史
    - TPEx（上櫃）官方整批行情端點目前僅回最新日，因此歷史回補改用 yfinance
    """

    if end_date is None:
        end_date = dt.date.today()

    # 先清空，避免之前誤存的 OTC 歷史污染結果
    with conn:
        conn.execute("DELETE FROM bars")
        conn.execute("DELETE FROM day_meta")

    # 1) 回補 TWSE 的近 keep_trading_days 交易日資料（用是否有 TWSE bars 判斷交易日）
    collected: list[dt.date] = []
    for day in iter_calendar_days_back(end_date):
        if len(collected) >= keep_trading_days:
            break
        twse = fetch_twse_daily_bars(day, cache_dir=cache_dir, use_cache=use_cache)
        if not twse:
            continue
        upsert_bars(conn, day, twse, fetched_at=dt.datetime.now(dt.timezone.utc))
        collected.append(day)
        time.sleep(per_day_delay + random.random() * 0.5)

    # 只保留最後 keep_trading_days 個（asc）
    collected = sorted(set(collected))[-keep_trading_days:]
    if not collected:
        raise RuntimeError("無法取得 TWSE 交易日資料，請確認網路/資料源")

    start_date = collected[0]
    latest_date = collected[-1]

    # 2) 取得上櫃最新快照（用於 code/name 清單 + 最新日補齊）
    snap_date, snap_bars = fetch_tpex_latest_snapshot(cache_dir=cache_dir, use_cache=use_cache)

    # 若快照日期跟 TWSE 最新交易日不一致，以快照日期為準（通常是同一天）
    if snap_date != latest_date:
        latest_date = snap_date
        if latest_date not in collected:
            collected.append(latest_date)
            collected = sorted(set(collected))[-keep_trading_days:]
            start_date = collected[0]

    name_by_code = {b.code: b.name for b in snap_bars}
    otc_codes = sorted(name_by_code.keys())

    # 3) 寫入最新日 OTC 快照（name/code 清單也來源於此）
    if snap_bars:
        upsert_bars(conn, snap_date, snap_bars, fetched_at=dt.datetime.now(dt.timezone.utc))

    # 4) 用 yfinance 回補 OTC 歷史（涵蓋 start_date~latest_date）
    if otc_codes:
        chunk_delay = float(os.environ.get("STOCK_YF_DELAY", "1.2"))
        chunk_size = int(os.environ.get("STOCK_YF_CHUNK", "80"))
        by_date = backfill_otc_ohlcv(
            otc_codes,
            start_date=start_date,
            end_date=latest_date,
            name_by_code=name_by_code,
            chunk_size=chunk_size,
            chunk_delay=chunk_delay,
        )
        for d, bars in by_date.items():
            if d < start_date or d > latest_date:
                continue
            # 只更新 OTC bar；day_meta 會由 upsert_bars 重新計算兩市場數量
            upsert_bars(conn, d, bars, fetched_at=dt.datetime.now(dt.timezone.utc))

    # prune 舊資料（以交易日數為準；理論上已經是重建，但保留此保險）
    prune_to_last_n_days(conn, keep_trading_days)
    return latest_date
