from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable

CODE_RE = re.compile(r"^\d{4,6}$")
DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"


@dataclass(frozen=True)
class DailyBar:
    date: dt.date
    code: str
    name: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    market: str  # TWSE | TPEX


@dataclass(frozen=True)
class InstitutionTrade:
    date: dt.date
    code: str
    name: str
    foreign_net: int
    trust_net: int
    dealer_net: int
    total_net: int
    market: str  # TWSE | TPEX


def _to_roc_date(d: dt.date) -> str:
    return f"{d.year - 1911:03d}/{d.month:02d}/{d.day:02d}"


def _to_roc_compact(d: dt.date) -> str:
    return f"{d.year - 1911:03d}{d.month:02d}{d.day:02d}"


def _roc_compact_to_date(s: str) -> dt.date:
    s = str(s).strip()
    if not re.match(r"^\d{7}$", s):
        raise ValueError(f"ROC date format unexpected: {s}")
    y = 1911 + int(s[:3])
    m = int(s[3:5])
    d = int(s[5:7])
    return dt.date(y, m, d)


def _parse_price(value: str) -> float | None:
    v = str(value).strip()
    if not v or v in {"--", "---", "-", "N/A"}:
        return None
    v = v.replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    v = str(value).strip()
    if not v or v in {"--", "---", "-", "N/A"}:
        return None
    v = v.replace(",", "")
    try:
        return int(float(v))
    except ValueError:
        return None


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()

    # Some endpoints (notably certain TW/TPEx hosts) may present certificate chains
    # that fail OpenSSL's strict X.509 checks (e.g., Missing Subject Key Identifier)
    # on some macOS/Python builds. Relax strict mode while keeping CA verification.
    try:
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag:
            ctx.verify_flags &= ~strict_flag
    except Exception:
        pass

    return ctx


def _fetch_json_any(url: str, *, timeout: int = 30, retries: int = 3, user_agent: str = DEFAULT_USER_AGENT) -> object:
    """Like _fetch_json, but allows non-dict JSON payloads (e.g. list from OpenAPI)."""

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
            "HTTPS 憑證驗證失敗。建議使用 conda/venv 的 Python 執行；"
            "或在你了解風險下暫時設定 STOCK_SCREENER_INSECURE=1"
        )
    raise RuntimeError(f"抓取失敗: {url} ({last_err})")


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
            "HTTPS 憑證驗證失敗。建議使用 conda/venv 的 Python 執行；"
            "或在你了解風險下暫時設定 STOCK_SCREENER_INSECURE=1"
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


def _load_or_fetch_json_any(cache_dir: str, cache_key: str, url: str, *, use_cache: bool) -> object:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{cache_key}.json")
    if use_cache and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    payload = _fetch_json_any(url)
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


def _tpex_openapi_daily_url() -> str:
    return "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


def _twse_openapi_company_basic_url() -> str:
    return "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"


@dataclass(frozen=True)
class CompanyShareInfo:
    code: str
    name: str
    issued_shares: int


def fetch_twse_company_share_info(
    *,
    cache_dir: str,
    use_cache: bool = True,
) -> tuple[str | None, list[CompanyShareInfo]]:
    """抓取上市公司基本資料（含已發行普通股數）。

    回傳 (出表日期, rows)。出表日期為 ROC compact (例如 1150427)。
    """

    payload = _load_or_fetch_json_any(cache_dir, "twse_company_basic", _twse_openapi_company_basic_url(), use_cache=use_cache)
    if not isinstance(payload, list) or not payload:
        return None, []

    asof_roc = str(payload[0].get("出表日期") or "").strip() or None
    out: list[CompanyShareInfo] = []
    for row in payload:
        try:
            code = str(row.get("公司代號") or "").strip()
            if not CODE_RE.match(code):
                continue
            name = str(row.get("公司簡稱") or row.get("公司名稱") or "").strip()
            if not name:
                continue
            if _is_derivative_like(name):
                continue
            issued_raw = row.get("已發行普通股數或TDR原股發行股數")
            issued = _parse_int(issued_raw)
            if issued is None or issued <= 0:
                continue
            out.append(CompanyShareInfo(code=code, name=name, issued_shares=int(issued)))
        except Exception:
            continue

    return asof_roc, out


def _twse_t86_url(d: dt.date) -> str:
    # 三大法人買賣超日報（上市）
    return f"https://www.twse.com.tw/fund/T86?response=json&date={d:%Y%m%d}&selectType=ALL"


def _tpex_3inst_url(d: dt.date) -> str:
    # 三大法人買賣明細資訊（上櫃）
    roc = urllib.parse.quote(_to_roc_date(d))
    return f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&d={roc}"


def _parse_tpex_3inst(payload: dict, d: dt.date) -> list[InstitutionTrade]:
    if str(payload.get("stat") or "").lower() != "ok":
        return []

    tables = payload.get("tables") or []
    if not tables or not isinstance(tables, list):
        return []

    table = tables[0] if isinstance(tables[0], dict) else None
    if not table:
        return []

    # TPEx 回傳的是 ROC 日期字串（例如 115/04/29）。非交易日有時會回前一交易日；若不一致視為無資料。
    try:
        table_date = str(table.get("date") or "").strip()
        if table_date and table_date != _to_roc_date(d):
            return []
    except Exception:
        return []

    fields = [str(x).strip() for x in (table.get("fields") or [])]
    if len(fields) < 24:
        return []

    # 欄位順序（fields_len=24, row_len=24）：
    # 0 code, 1 name,
    # 2..22: 7 組 (buy,sell,net) => net index = 4,7,10,13,16,19,22
    # 23: 三大法人買賣超股數合計
    # 我們採用：
    # - 外資(含外資自營商) net => index 10
    # - 投信 net => index 13
    # - 自營商合計 net => index 22
    # - 三大法人合計 net => index 23
    i_code, i_name = 0, 1
    i_foreign_net, i_trust_net, i_dealer_net, i_total_net = 10, 13, 22, 23
    max_i = max(i_total_net, i_dealer_net, i_trust_net, i_foreign_net)

    out: list[InstitutionTrade] = []
    for row in (table.get("data") or []) or []:
        if not isinstance(row, list) or len(row) <= max_i:
            continue

        code = str(row[i_code]).strip()
        if not CODE_RE.match(code):
            continue
        name = str(row[i_name]).strip()
        if _is_derivative_like(name):
            continue

        foreign_net = _parse_int(row[i_foreign_net])
        trust_net = _parse_int(row[i_trust_net])
        dealer_net = _parse_int(row[i_dealer_net])
        total_net = _parse_int(row[i_total_net])
        if None in {foreign_net, trust_net, dealer_net, total_net}:
            continue

        out.append(
            InstitutionTrade(
                date=d,
                code=code,
                name=name,
                foreign_net=int(foreign_net),
                trust_net=int(trust_net),
                dealer_net=int(dealer_net),
                total_net=int(total_net),
                market="TPEX",
            )
        )

    return out


def _parse_twse_t86(payload: dict, d: dt.date) -> list[InstitutionTrade]:
    if payload.get("stat") != "OK":
        return []

    fields = [str(x).strip() for x in payload.get("fields", [])]

    def idx(name: str) -> int | None:
        try:
            return fields.index(name)
        except ValueError:
            return None

    i_code = idx("證券代號")
    i_name = idx("證券名稱")
    i_foreign = idx("外陸資買賣超股數(不含外資自營商)")
    i_trust = idx("投信買賣超股數")
    i_dealer = idx("自營商買賣超股數")
    i_total = idx("三大法人買賣超股數")

    if None in {i_code, i_name, i_foreign, i_trust, i_dealer, i_total}:
        return []

    max_i = max(int(i_code), int(i_name), int(i_foreign), int(i_trust), int(i_dealer), int(i_total))

    out: list[InstitutionTrade] = []
    for row in payload.get("data", []) or []:
        if not row:
            continue
        # 有些列會缺少尾端欄位（例如部分證券），避免 IndexError
        if not isinstance(row, list) or len(row) <= max_i:
            continue
        code = str(row[i_code]).strip()
        if not CODE_RE.match(code):
            continue
        name = str(row[i_name]).strip()
        if _is_derivative_like(name):
            continue

        foreign_net = _parse_int(row[i_foreign])
        trust_net = _parse_int(row[i_trust])
        dealer_net = _parse_int(row[i_dealer])
        total_net = _parse_int(row[i_total])
        if None in {foreign_net, trust_net, dealer_net, total_net}:
            continue

        out.append(
            InstitutionTrade(
                date=d,
                code=code,
                name=name,
                foreign_net=int(foreign_net),
                trust_net=int(trust_net),
                dealer_net=int(dealer_net),
                total_net=int(total_net),
                market="TWSE",
            )
        )

    return out


def _is_derivative_like(name: str) -> bool:
    # 保守排除典型權證/牛熊證關鍵字
    n = name.strip()
    keywords = ("權證", "牛", "熊", "購", "售")
    return any(k in n for k in keywords)


def _parse_twse_daily(payload: dict, d: dt.date) -> list[DailyBar]:
    if payload.get("stat") != "OK":
        return []

    table = None
    for t in payload.get("tables", []):
        title = t.get("title") or ""
        if "每日收盤行情(全部)" in title:
            table = t
            break
    if not table:
        return []

    fields = [str(x).strip() for x in table.get("fields", [])]

    def idx(name: str) -> int | None:
        try:
            return fields.index(name)
        except ValueError:
            return None

    i_code = idx("證券代號")
    i_name = idx("證券名稱")
    i_vol = idx("成交股數")
    i_open = idx("開盤價")
    # TWSE 有時欄位帶前置空白
    i_high = idx("最高價")
    if i_high is None:
        i_high = idx(" 最高價")
    i_low = idx("最低價")
    i_close = idx("收盤價")

    if None in {i_code, i_name, i_vol, i_open, i_high, i_low, i_close}:
        return []

    bars: list[DailyBar] = []
    for row in table.get("data", []):
        if not row:
            continue
        code = str(row[i_code]).strip()
        if not CODE_RE.match(code):
            continue
        name = str(row[i_name]).strip()
        if _is_derivative_like(name):
            continue

        vol = _parse_int(row[i_vol])
        o = _parse_price(row[i_open])
        h = _parse_price(row[i_high])
        l = _parse_price(row[i_low])
        c = _parse_price(row[i_close])
        if None in {vol, o, h, l, c}:
            continue

        bars.append(
            DailyBar(
                date=d,
                code=code,
                name=name,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=int(vol),
                market="TWSE",
            )
        )
    return bars


def _parse_tpex_daily(payload: dict, d: dt.date) -> list[DailyBar]:
    if str(payload.get("stat")).lower() != "ok":
        return []

    # TPEx 在休市/非交易日可能回傳「前一交易日」資料；若日期不一致則視為該日無資料
    try:
        payload_date = str(payload.get("date") or "").strip()
        if payload_date and payload_date.isdigit():
            if payload_date != d.strftime("%Y%m%d"):
                return []
    except Exception:
        return []

    tables = payload.get("tables") or []
    if not tables:
        return []

    table = tables[0]
    # TPEx 欄位名稱可能含空白
    raw_fields = [str(x) for x in table.get("fields", [])]
    fields = [f.strip().replace(" ", "") for f in raw_fields]

    def idx(name: str) -> int | None:
        try:
            return fields.index(name)
        except ValueError:
            return None

    i_code = idx("代號")
    i_name = idx("名稱")
    i_close = idx("收盤")
    i_open = idx("開盤")
    i_high = idx("最高")
    i_low = idx("最低")
    # 成交股數在欄位中可能叫「成交股 數」
    i_vol = None
    for i, f in enumerate(fields):
        if f in {"成交股數", "成交股數量", "成交股數(股)", "成交股數(張)"}:
            i_vol = i
            break
        if "成交股" in f and "數" in f:
            i_vol = i
            break

    if None in {i_code, i_name, i_vol, i_open, i_high, i_low, i_close}:
        return []

    max_i = max(int(i_code), int(i_name), int(i_vol), int(i_open), int(i_high), int(i_low), int(i_close))

    bars: list[DailyBar] = []
    for row in table.get("data", []):
        if not row:
            continue
        if not isinstance(row, list) or len(row) <= max_i:
            continue
        code = str(row[i_code]).strip()
        if not CODE_RE.match(code):
            continue
        name = str(row[i_name]).strip()
        if _is_derivative_like(name):
            continue

        vol = _parse_int(row[i_vol])
        o = _parse_price(row[i_open])
        h = _parse_price(row[i_high])
        l = _parse_price(row[i_low])
        c = _parse_price(row[i_close])
        if None in {vol, o, h, l, c}:
            continue

        bars.append(
            DailyBar(
                date=d,
                code=code,
                name=name,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=int(vol),
                market="TPEX",
            )
        )

    return bars


def fetch_twse_daily_bars(
    d: dt.date,
    *,
    cache_dir: str,
    use_cache: bool = True,
) -> list[DailyBar]:
    # 台股不在週六/週日交易；避免週末端點回傳前一交易日而誤寫日期
    if d.weekday() >= 5:
        return []
    payload = _load_or_fetch_json(cache_dir, f"twse_{d:%Y%m%d}", _twse_daily_url(d), use_cache=use_cache)
    return _parse_twse_daily(payload, d)


def fetch_twse_institution_trades(
    d: dt.date,
    *,
    cache_dir: str,
    use_cache: bool = True,
) -> list[InstitutionTrade]:
    # 台股不在週六/週日交易；避免週末端點回傳前一交易日而誤寫日期
    if d.weekday() >= 5:
        return []
    payload = _load_or_fetch_json(cache_dir, f"twse_t86_{d:%Y%m%d}", _twse_t86_url(d), use_cache=use_cache)
    return _parse_twse_t86(payload, d)


def fetch_tpex_institution_trades(
    d: dt.date,
    *,
    cache_dir: str,
    use_cache: bool = True,
) -> list[InstitutionTrade]:
    # 台股不在週六/週日交易；避免週末端點回前一交易日而誤寫日期
    if d.weekday() >= 5:
        return []
    payload = _load_or_fetch_json(cache_dir, f"tpex_3inst_{_to_roc_compact(d)}", _tpex_3inst_url(d), use_cache=use_cache)
    return _parse_tpex_3inst(payload, d)


def fetch_tpex_latest_snapshot(
    *,
    cache_dir: str,
    use_cache: bool = True,
) -> tuple[dt.date, list[DailyBar]]:
    """抓取 TPEx OpenAPI 的『上櫃股票行情』最新日快照。

    注意：此端點目前只回最新日，不支援歷史查詢。
    """

    url = _tpex_openapi_daily_url()
    payload = _load_or_fetch_json(cache_dir, "tpex_openapi_latest", url, use_cache=use_cache)
    if not isinstance(payload, list) or not payload:
        return (dt.date.today(), [])

    snapshot_date = _roc_compact_to_date(payload[0].get("Date"))
    bars: list[DailyBar] = []
    for row in payload:
        try:
            code = str(row.get("SecuritiesCompanyCode", "")).strip()
            if not CODE_RE.match(code):
                continue
            name = str(row.get("CompanyName", "")).strip()
            if _is_derivative_like(name):
                continue
            o = _parse_price(row.get("Open"))
            h = _parse_price(row.get("High"))
            l = _parse_price(row.get("Low"))
            c = _parse_price(row.get("Close"))
            v = _parse_int(row.get("TradingShares"))
            if None in {o, h, l, c, v}:
                continue
            bars.append(
                DailyBar(
                    date=snapshot_date,
                    code=code,
                    name=name,
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                    volume=int(v),
                    market="TPEX",
                )
            )
        except Exception:
            continue

    return snapshot_date, bars


def fetch_daily_bars(
    d: dt.date,
    *,
    cache_dir: str,
    use_cache: bool = True,
    per_request_delay: float = 0.4,
) -> tuple[list[DailyBar], list[DailyBar]]:
    """回傳 (twse_bars, tpex_bars)。若該日非交易日通常兩者都為空。"""

    # 台股不在週六/週日交易；直接視為非交易日
    if d.weekday() >= 5:
        return [], []

    twse_payload = _load_or_fetch_json(cache_dir, f"twse_{d:%Y%m%d}", _twse_daily_url(d), use_cache=use_cache)
    time.sleep(per_request_delay + random.random() * 0.2)
    tpex_cache_key = f"tpex_{d:%Y%m%d}"
    tpex_url = _tpex_daily_url(d)
    tpex_payload = _load_or_fetch_json(cache_dir, tpex_cache_key, tpex_url, use_cache=use_cache)

    # TPEx 在休市或端點異常時，可能回傳非查詢日的資料；若遇到日期不一致，嘗試繞過 cache 再抓一次
    # 以避免 cache 被「錯日資料」污染，導致後續回補永遠拿不到正確日期。
    try:
        payload_date = str(tpex_payload.get("date") or "").strip()
        if payload_date and payload_date.isdigit() and payload_date != d.strftime("%Y%m%d"):
            fresh = _fetch_json(tpex_url)
            fresh_date = str(fresh.get("date") or "").strip()
            if fresh_date and fresh_date.isdigit() and fresh_date == d.strftime("%Y%m%d"):
                # overwrite cache with the correct payload
                os.makedirs(cache_dir, exist_ok=True)
                path = os.path.join(cache_dir, f"{tpex_cache_key}.json")
                tmp = f"{path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(fresh, f, ensure_ascii=False)
                os.replace(tmp, path)
                tpex_payload = fresh
    except Exception:
        # 保守：出錯就交給 parser 判斷回空
        pass

    return _parse_twse_daily(twse_payload, d), _parse_tpex_daily(tpex_payload, d)


def iter_calendar_days_back(end_date: dt.date) -> Iterable[dt.date]:
    cursor = end_date
    while True:
        yield cursor
        cursor -= dt.timedelta(days=1)
