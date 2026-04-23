#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable


CODE_RE = re.compile(r"^\d{4,6}$")
DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    close: float


def _parse_date(s: str) -> dt.date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError("日期格式需為 YYYY-MM-DD 或 YYYYMMDD")


def _to_roc_date(d: dt.date) -> str:
    return f"{d.year - 1911:03d}/{d.month:02d}/{d.day:02d}"


def _parse_price(value: str) -> float | None:
    v = str(value).strip()
    if not v or v in {"--", "---", "-", "N/A"}:
        return None
    v = v.replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _is_derivative_like(name: str) -> bool:
    # 避免「股票篩選」輸出被權證/牛熊證等衍生性商品洗版。
    # 這是保守的啟發式：只排除非常典型的關鍵字。
    n = name.strip()
    keywords = ("權證", "牛", "熊", "購", "售")
    return any(k in n for k in keywords)


def _fetch_json(url: str, *, timeout: int = 30, retries: int = 3, user_agent: str = DEFAULT_USER_AGENT) -> dict:
    last_err: Exception | None = None
    context = _ssl_context()
    insecure_allowed = os.environ.get("STOCK_SCREENER_INSECURE") == "1"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="replace"))
        except ssl.SSLCertVerificationError as e:
            last_err = e
            if insecure_allowed:
                # 明確由使用者允許才會關閉驗證
                context = ssl._create_unverified_context()  # noqa: S501
                continue
            break
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
            last_err = e
            if attempt == retries - 1:
                break
            time.sleep(1.2 * (2**attempt))
    if isinstance(last_err, ssl.SSLCertVerificationError):
        raise RuntimeError(
            "HTTPS 憑證驗證失敗。建議用 conda 環境的 Python 執行，例如："
            " /Users/wangshaocheng/anaconda3/envs/stock/bin/python stock_screener.py ... "
            "（或在你了解風險的前提下，暫時設定環境變數 STOCK_SCREENER_INSECURE=1）"
        )
    raise RuntimeError(f"抓取失敗: {url} ({last_err})")


def _load_or_fetch_json(cache_dir: str, cache_key: str, url: str, *, use_cache: bool) -> dict:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{cache_key}.json")
    if use_cache and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    payload = _fetch_json(url)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)
    return payload


def _twse_daily_url(d: dt.date) -> str:
    return f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={d:%Y%m%d}&type=ALL"


def _tpex_daily_url(d: dt.date) -> str:
    roc = urllib.parse.quote(_to_roc_date(d))
    return f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&o=json&d={roc}&s=0,asc,0"


def _parse_twse_daily(payload: dict) -> dict[str, Quote]:
    if payload.get("stat") != "OK":
        return {}

    table = None
    for t in payload.get("tables", []):
        title = t.get("title") or ""
        if "每日收盤行情(全部)" in title:
            table = t
            break
    if not table:
        return {}

    fields = [str(x).strip() for x in table.get("fields", [])]
    try:
        i_code = fields.index("證券代號")
        i_name = fields.index("證券名稱")
        i_close = fields.index("收盤價")
    except ValueError:
        return {}

    out: dict[str, Quote] = {}
    for row in table.get("data", []):
        if not row or len(row) <= max(i_code, i_name, i_close):
            continue
        code = str(row[i_code]).strip()
        if not CODE_RE.match(code):
            continue
        name = str(row[i_name]).strip()
        if _is_derivative_like(name):
            continue
        close = _parse_price(row[i_close])
        if close is None:
            continue
        out[code] = Quote(code=code, name=name, close=close)
    return out


def _parse_tpex_daily(payload: dict) -> dict[str, Quote]:
    if str(payload.get("stat")).lower() != "ok":
        return {}

    tables = payload.get("tables") or []
    if not tables:
        return {}

    table = tables[0]
    fields = [str(x).strip().replace(" ", "") for x in table.get("fields", [])]
    # TPEx 欄位名稱有時會包含空白 (例如 成交股 數)，我們先移除空白後再索引
    try:
        i_code = fields.index("代號")
        i_name = fields.index("名稱")
        i_close = fields.index("收盤")
    except ValueError:
        return {}

    out: dict[str, Quote] = {}
    for row in table.get("data", []):
        if not row or len(row) <= max(i_code, i_name, i_close):
            continue
        code = str(row[i_code]).strip()
        if not CODE_RE.match(code):
            continue
        name = str(row[i_name]).strip()
        if _is_derivative_like(name):
            continue
        close = _parse_price(row[i_close])
        if close is None:
            continue
        out[code] = Quote(code=code, name=name, close=close)
    return out


def _fetch_day_quotes(d: dt.date, *, cache_dir: str, use_cache: bool) -> dict[str, Quote]:
    twse = _load_or_fetch_json(cache_dir, f"twse_{d:%Y%m%d}", _twse_daily_url(d), use_cache=use_cache)
    tpex = _load_or_fetch_json(cache_dir, f"tpex_{d:%Y%m%d}", _tpex_daily_url(d), use_cache=use_cache)

    merged: dict[str, Quote] = {}
    merged.update(_parse_twse_daily(twse))
    merged.update(_parse_tpex_daily(tpex))
    return merged


def _collect_recent_trading_days(end_date: dt.date, needed_days: int, *, cache_dir: str, use_cache: bool) -> list[tuple[dt.date, dict[str, Quote]]]:
    collected: list[tuple[dt.date, dict[str, Quote]]] = []  # newest -> oldest
    cursor = end_date
    max_scan_days = max(needed_days * 4, 260)  # avoid infinite loop; covers long holiday windows

    for _ in range(max_scan_days):
        quotes = _fetch_day_quotes(cursor, cache_dir=cache_dir, use_cache=use_cache)
        if quotes:
            collected.append((cursor, quotes))
            if len(collected) >= needed_days:
                break
        cursor -= dt.timedelta(days=1)

    if not collected:
        raise RuntimeError("找不到任何可用交易日資料（可能是網路或資料源問題）")

    return list(reversed(collected))  # oldest -> newest


def _build_close_series(day_quotes: Iterable[tuple[dt.date, dict[str, Quote]]]) -> tuple[dict[str, str], dict[str, list[float]]]:
    names: dict[str, str] = {}
    series: dict[str, list[float]] = {}
    for _, quotes in day_quotes:
        for code, q in quotes.items():
            names[code] = q.name
            series.setdefault(code, []).append(q.close)
    return names, series


@dataclass(frozen=True)
class NearMAResult:
    code: str
    name: str
    close: float
    ma: float
    dist_pct: float


def _near_ma(names: dict[str, str], series: dict[str, list[float]], window: int, threshold_pct: float) -> list[NearMAResult]:
    out: list[NearMAResult] = []
    for code, closes in series.items():
        if len(closes) < window:
            continue
        ma = sum(closes[-window:]) / window
        close = closes[-1]
        if ma == 0:
            continue
        dist_pct = abs(close - ma) / ma * 100.0
        if dist_pct <= threshold_pct:
            out.append(
                NearMAResult(
                    code=code,
                    name=names.get(code, ""),
                    close=close,
                    ma=ma,
                    dist_pct=dist_pct,
                )
            )
    out.sort(key=lambda r: (r.dist_pct, r.code))
    return out


@dataclass(frozen=True)
class GainerResult:
    code: str
    name: str
    close: float
    past_close: float
    pct: float


def _top_gainers(names: dict[str, str], series: dict[str, list[float]], lookback_days: int) -> list[GainerResult]:
    if lookback_days <= 0:
        raise ValueError("lookback_days 必須 >= 1")

    out: list[GainerResult] = []
    for code, closes in series.items():
        # 「N 個交易日前（只看交易日）」：比較 today vs closes[-1-N]
        if len(closes) < lookback_days + 1:
            continue
        close = closes[-1]
        past = closes[-1 - lookback_days]
        if past == 0:
            continue
        pct = (close / past - 1.0) * 100.0
        out.append(GainerResult(code=code, name=names.get(code, ""), close=close, past_close=past, pct=pct))

    out.sort(key=lambda r: (r.pct, r.code), reverse=True)
    return out


def _print_table(rows: list[list[str]]) -> None:
    if not rows:
        print("(無)")
        return

    widths = [0] * len(rows[0])
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))

    for idx, r in enumerate(rows):
        line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(r))
        print(line)
        if idx == 0:
            print("  ".join("-" * w for w in widths))


def cmd_near_ma(args: argparse.Namespace) -> int:
    end_date = args.date or dt.date.today()
    needed_days = max(args.ma_60, args.ma_20)
    day_quotes = _collect_recent_trading_days(end_date, needed_days, cache_dir=args.cache_dir, use_cache=not args.no_cache)
    asof = day_quotes[-1][0]

    names, series = _build_close_series(day_quotes)

    print(f"資料截至交易日: {asof:%Y-%m-%d}  (共 {len(day_quotes)} 個交易日, 標的數: {len(series)})")
    print()

    if args.ma_20:
        res20 = _near_ma(names, series, args.ma_20, args.threshold_pct)
        print(f"接近 {args.ma_20}MA (距離 <= {args.threshold_pct:.2f}%) — Top {args.top}")
        rows = [["代號", "名稱", "收盤", f"MA{args.ma_20}", "距離%"]]
        for r in res20[: args.top]:
            rows.append([r.code, r.name, f"{r.close:.2f}", f"{r.ma:.2f}", f"{r.dist_pct:.2f}"])
        _print_table(rows)
        print()

    if args.ma_60:
        res60 = _near_ma(names, series, args.ma_60, args.threshold_pct)
        print(f"接近 {args.ma_60}MA (距離 <= {args.threshold_pct:.2f}%) — Top {args.top}")
        rows = [["代號", "名稱", "收盤", f"MA{args.ma_60}", "距離%"]]
        for r in res60[: args.top]:
            rows.append([r.code, r.name, f"{r.close:.2f}", f"{r.ma:.2f}", f"{r.dist_pct:.2f}"])
        _print_table(rows)

    return 0


def cmd_gainers(args: argparse.Namespace) -> int:
    end_date = args.date or dt.date.today()
    needed_days = max(args.lookback_days + 1, 60)
    day_quotes = _collect_recent_trading_days(end_date, needed_days, cache_dir=args.cache_dir, use_cache=not args.no_cache)
    asof = day_quotes[-1][0]

    names, series = _build_close_series(day_quotes)
    res = _top_gainers(names, series, args.lookback_days)

    print(f"資料截至交易日: {asof:%Y-%m-%d}  (回看 {args.lookback_days} 交易日, 標的數: {len(series)})")
    print(f"近 {args.lookback_days} 日漲幅排行 — Top {args.top}")
    rows = [["代號", "名稱", "收盤", f"{args.lookback_days}日前收盤", "漲幅%"]]
    for r in res[: args.top]:
        rows.append([r.code, r.name, f"{r.close:.2f}", f"{r.past_close:.2f}", f"{r.pct:.2f}"])
    _print_table(rows)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="台股篩選：接近 20/60MA、以及近 N 日漲幅排行（資料源：TWSE + TPEx 每日收盤行情）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ma = sub.add_parser("near-ma", help="找接近 20MA/60MA 的標的")
    p_ma.add_argument("--date", type=_parse_date, default=None, help="結束日期（預設今天）；格式 YYYY-MM-DD 或 YYYYMMDD")
    p_ma.add_argument("--cache-dir", default=os.path.join(os.path.dirname(__file__), ".cache"), help="快取資料夾")
    p_ma.add_argument("--no-cache", action="store_true", help="不使用快取，每次都重新抓")
    p_ma.add_argument("--ma-20", type=int, default=20, help="MA20 週期（0 代表不顯示）")
    p_ma.add_argument("--ma-60", type=int, default=60, help="MA60 週期（0 代表不顯示）")
    p_ma.add_argument("--threshold-pct", type=float, default=1.0, help="距離門檻（百分比），預設 1.0")
    p_ma.add_argument("--top", type=int, default=50, help="顯示筆數")
    p_ma.set_defaults(func=cmd_near_ma)

    p_g = sub.add_parser("gainers", help="近 N 交易日漲幅排行")
    p_g.add_argument("--date", type=_parse_date, default=None, help="結束日期（預設今天）；格式 YYYY-MM-DD 或 YYYYMMDD")
    p_g.add_argument("--cache-dir", default=os.path.join(os.path.dirname(__file__), ".cache"), help="快取資料夾")
    p_g.add_argument("--no-cache", action="store_true", help="不使用快取，每次都重新抓")
    p_g.add_argument("--lookback-days", type=int, default=5, help="回看 N 個交易日，預設 5")
    p_g.add_argument("--top", type=int, default=50, help="顯示筆數")
    p_g.set_defaults(func=cmd_gainers)

    return p


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n中斷", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
