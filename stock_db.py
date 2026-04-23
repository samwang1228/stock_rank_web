from __future__ import annotations

import contextlib
import datetime as dt
import os
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from tw_market_data import DailyBar


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS day_meta (
  date TEXT PRIMARY KEY,
  twse_count INTEGER NOT NULL,
  tpex_count INTEGER NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bars (
  date TEXT NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  market TEXT NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume INTEGER NOT NULL,
  PRIMARY KEY (date, code)
);

CREATE INDEX IF NOT EXISTS idx_bars_code_date ON bars(code, date);
"""


@dataclass(frozen=True)
class DayMeta:
    date: dt.date
    twse_count: int
    tpex_count: int
    fetched_at: dt.datetime


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _date_to_str(d: dt.date) -> str:
    return d.isoformat()


def _str_to_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def upsert_day(conn: sqlite3.Connection, d: dt.date, twse: list[DailyBar], tpex: list[DailyBar], fetched_at: dt.datetime) -> None:
    upsert_bars(conn, d, twse + tpex, fetched_at=fetched_at)


def upsert_bars(conn: sqlite3.Connection, d: dt.date, bars: list[DailyBar], *, fetched_at: dt.datetime) -> None:
    """Upsert bars for a date, then recompute day_meta counts from DB.

    This allows syncing TWSE/TPEX independently without accidentally resetting the other market's counts.
    """

    date_s = _date_to_str(d)
    with conn:
        if bars:
            rows = [(
                date_s,
                b.code,
                b.name,
                b.market,
                float(b.open),
                float(b.high),
                float(b.low),
                float(b.close),
                int(b.volume),
            ) for b in bars]

            conn.executemany(
                "INSERT INTO bars(date, code, name, market, open, high, low, close, volume) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(date, code) DO UPDATE SET "
                "name=excluded.name, market=excluded.market, open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume",
                rows,
            )

        counts = {"TWSE": 0, "TPEX": 0}
        cur = conn.execute("SELECT market, COUNT(*) AS cnt FROM bars WHERE date=? GROUP BY market", (date_s,))
        for r in cur.fetchall():
            counts[str(r["market"])] = int(r["cnt"])

        conn.execute(
            "INSERT INTO day_meta(date, twse_count, tpex_count, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET twse_count=excluded.twse_count, tpex_count=excluded.tpex_count, fetched_at=excluded.fetched_at",
            (date_s, int(counts.get("TWSE", 0)), int(counts.get("TPEX", 0)), fetched_at.isoformat(timespec="seconds")),
        )


def has_day(conn: sqlite3.Connection, d: dt.date) -> bool:
    cur = conn.execute("SELECT 1 FROM day_meta WHERE date=?", (_date_to_str(d),))
    return cur.fetchone() is not None


def list_trading_dates(conn: sqlite3.Connection, limit: int | None = None, *, desc: bool = False) -> list[dt.date]:
    sql = "SELECT date FROM day_meta ORDER BY date " + ("DESC" if desc else "ASC")
    if limit is not None:
        sql += " LIMIT ?"
        cur = conn.execute(sql, (int(limit),))
    else:
        cur = conn.execute(sql)
    return [_str_to_date(r["date"]) for r in cur.fetchall()]


def latest_trading_date(conn: sqlite3.Connection) -> dt.date | None:
    cur = conn.execute("SELECT date FROM day_meta ORDER BY date DESC LIMIT 1")
    row = cur.fetchone()
    return _str_to_date(row["date"]) if row else None


def prune_to_last_n_days(conn: sqlite3.Connection, keep_trading_days: int) -> None:
    dates = list_trading_dates(conn, desc=True)
    if len(dates) <= keep_trading_days:
        return
    keep_set = {_date_to_str(d) for d in dates[:keep_trading_days]}
    with conn:
        conn.execute(
            "DELETE FROM bars WHERE date NOT IN (SELECT date FROM day_meta ORDER BY date DESC LIMIT ?)",
            (int(keep_trading_days),),
        )
        conn.execute(
            "DELETE FROM day_meta WHERE date NOT IN (SELECT date FROM day_meta ORDER BY date DESC LIMIT ?)",
            (int(keep_trading_days),),
        )


@dataclass(frozen=True)
class BarRow:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: int


def get_bars_for_code(conn: sqlite3.Connection, code: str, dates: Iterable[dt.date] | None = None) -> list[BarRow]:
    if dates is None:
        cur = conn.execute(
            "SELECT date, open, high, low, close, volume FROM bars WHERE code=? ORDER BY date ASC",
            (code,),
        )
        rows = cur.fetchall()
    else:
        ds = [_date_to_str(d) for d in dates]
        if not ds:
            return []
        placeholders = ",".join(["?"] * len(ds))
        cur = conn.execute(
            f"SELECT date, open, high, low, close, volume FROM bars WHERE code=? AND date IN ({placeholders}) ORDER BY date ASC",
            (code, *ds),
        )
        rows = cur.fetchall()

    return [
        BarRow(
            date=_str_to_date(r["date"]),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=int(r["volume"]),
        )
        for r in rows
    ]


def get_code_name(conn: sqlite3.Connection, code: str) -> str | None:
    cur = conn.execute("SELECT name FROM bars WHERE code=? ORDER BY date DESC LIMIT 1", (code,))
    row = cur.fetchone()
    return str(row["name"]) if row else None


def select_closes_for_dates(conn: sqlite3.Connection, dates: list[dt.date]) -> list[sqlite3.Row]:
    if not dates:
        return []
    ds = [_date_to_str(d) for d in dates]
    placeholders = ",".join(["?"] * len(ds))
    cur = conn.execute(
        f"SELECT code, name, date, close FROM bars WHERE date IN ({placeholders}) ORDER BY code ASC, date ASC",
        ds,
    )
    return cur.fetchall()


def select_closes_for_last_n_trading_days(conn: sqlite3.Connection, n: int) -> list[sqlite3.Row]:
    cur_dates = conn.execute("SELECT date FROM day_meta ORDER BY date DESC LIMIT ?", (int(n),)).fetchall()
    dates = [_str_to_date(r["date"]) for r in cur_dates]
    dates.sort()
    return select_closes_for_dates(conn, dates)


@contextlib.contextmanager
def db_session(db_path: str):
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
