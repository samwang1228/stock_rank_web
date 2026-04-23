from __future__ import annotations

import datetime as dt
import random
import time
from typing import Callable

import pandas as pd
import yfinance as yf

from tw_market_data import DailyBar


def _to_yf_ticker(code: str) -> str:
    return f"{code}.TWO"


def backfill_otc_ohlcv(
    codes: list[str],
    *,
    start_date: dt.date,
    end_date: dt.date,
    name_by_code: dict[str, str],
    chunk_size: int = 80,
    chunk_delay: float = 1.2,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[dt.date, list[DailyBar]]:
    """用 yfinance 抓取上櫃 OTC 的歷史 OHLCV，回傳按 date 分組的 DailyBar。

    - start_date/end_date 都是「包含」；內部會用 end_date+1 作為 yfinance end（exclusive）。
    - 會分 chunk 抓，並在 chunk 間 sleep，避免被限流。
    """

    if not codes:
        return {}

    end_exclusive = end_date + dt.timedelta(days=1)

    out: dict[dt.date, list[DailyBar]] = {}
    total = (len(codes) + chunk_size - 1) // chunk_size

    for idx in range(total):
        chunk = codes[idx * chunk_size : (idx + 1) * chunk_size]
        tickers = [_to_yf_ticker(c) for c in chunk]

        if progress_cb:
            progress_cb(idx + 1, total)

        df = yf.download(
            tickers=tickers,
            start=start_date.isoformat(),
            end=end_exclusive.isoformat(),
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=False,
        )

        if df is None or df.empty:
            time.sleep(chunk_delay + random.random() * 0.4)
            continue

        # Multi-ticker returns MultiIndex columns; single-ticker returns flat columns
        if isinstance(df.columns, pd.MultiIndex):
            # Expect level0 tickers
            for t in tickers:
                if t not in df.columns.get_level_values(0):
                    continue
                sub = df[t].dropna(how="all")
                _ingest_subframe(out, sub, code=t.split(".")[0], name_by_code=name_by_code)
        else:
            # Single ticker
            code = chunk[0]
            _ingest_subframe(out, df.dropna(how="all"), code=code, name_by_code=name_by_code)

        time.sleep(chunk_delay + random.random() * 0.4)

    return out


def _ingest_subframe(out: dict[dt.date, list[DailyBar]], sub: pd.DataFrame, *, code: str, name_by_code: dict[str, str]) -> None:
    # columns usually: Open High Low Close Adj Close Volume
    for ts, row in sub.iterrows():
        if pd.isna(row.get("Close")):
            continue
        date = ts.date() if hasattr(ts, "date") else dt.date.fromisoformat(str(ts)[:10])

        try:
            o = float(row["Open"])
            h = float(row["High"])
            l = float(row["Low"])
            c = float(row["Close"])
            v = int(row.get("Volume") or 0)
        except Exception:
            continue

        out.setdefault(date, []).append(
            DailyBar(
                date=date,
                code=code,
                name=name_by_code.get(code, ""),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=v,
                market="TPEX",
            )
        )
