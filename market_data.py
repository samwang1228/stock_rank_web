from __future__ import annotations

import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from io import StringIO
import time
from typing import Any, Callable

import pandas as pd

from stock_db import upsert_market_overview
from tw_market_data import _load_or_fetch_json_any, fetch_twse_daily_bars


TWSE_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&date={date}&type=IND"
TWSE_INDEX_INTRADAY_URL = "https://www.twse.com.tw/exchangeReport/MI_5MINS_INDEX?response=json&date={date}"
TWSE_MARKET_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&date={date}&type=ALL"
TWSE_MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={date}&selectType=ALL"
TWSE_INST_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date}&selectType=ALLBUT0999"
YAHOO_INDEX_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?period1={start}&period2={end}&interval=1d&events=history"
TAIFEX_FUTURES_URL = "https://www.taifex.com.tw/cht/3/futContractsDate"
YAHOO_INSTITUTIONAL_URL = "https://tw.stock.yahoo.com/institutional-trading"


def _number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "--", "---", "-", "N/A"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    value_f = _number(value)
    return None if value_f is None else int(round(value_f))


def _fetch_taifex_foreign_net_short(cache_dir: str, day: dt.date) -> int | None:
    """Return foreign TX futures short OI minus long OI for a trading date."""
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"market_taifex_futures_{day:%Y%m%d}.html")
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as handle:
            html = handle.read()
    else:
        body = urllib.parse.urlencode(
            {"queryType": "1", "queryDate": day.strftime("%Y/%m/%d"), "doQuery": "1"}
        ).encode()
        request = urllib.request.Request(
            TAIFEX_FUTURES_URL,
            data=body,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
        with open(cache_path, "w", encoding="utf-8") as handle:
            handle.write(html)

    tables = pd.read_html(StringIO(html))
    for table in tables:
        if not isinstance(table.columns, pd.MultiIndex) or len(table.columns) < 14:
            continue
        for _, row in table.iterrows():
            values = [str(value).strip() for value in row.tolist()]
            if len(values) < 14 or values[1] != "臺股期貨" or values[2] not in {"外資", "外資及陸資"}:
                continue
            long_open_interest = _integer(values[9])
            short_open_interest = _integer(values[11])
            if long_open_interest is None or short_open_interest is None:
                return None
            return short_open_interest - long_open_interest
    return None


def _fetch_yahoo_institutional(cache_dir: str) -> dict[str, dict[str, int]]:
    """Fetch Yahoo's concentrated-market institutional net amounts.

    Yahoo's embedded fields are in million NTD (M); persist them as NTD.
    """
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "market_yahoo_institutional.html")
    # 法人頁面會在收盤後更新；舊快取不能沿用，否則當日資料會一直缺少。
    request = urllib.request.Request(YAHOO_INSTITUTIONAL_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")
    with open(cache_path, "w", encoding="utf-8") as handle:
        handle.write(html)

    marker = '"StockInstitutionalTradingStore":'
    start = html.find(marker)
    if start < 0:
        return {}
    decoder = json.JSONDecoder()
    store, _ = decoder.raw_decode(html[start + len(marker):])
    trades = store.get("TAI", {}).get("data", {}).get("trades", [])
    result: dict[str, dict[str, int]] = {}
    for trade in trades:
        date_s = str(trade.get("date") or "")[:10]
        if not date_s:
            continue
        result[date_s] = {
            "foreign_net": int(trade["foreignDiffM"]) * 1_000_000,
            "trust_net": int(trade["investmentTrustDiffM"]) * 1_000_000,
            "dealer_net": int(trade["dealerDiffM"]) * 1_000_000,
        }
    return result


def _tables(payload: Any) -> list[tuple[list[str], list[list[Any]]]]:
    result: list[tuple[list[str], list[list[Any]]]] = []
    if not isinstance(payload, dict):
        return result
    for item in payload.get("data", []), payload.get("tables", []):
        if not isinstance(item, list):
            continue
        for table in item:
            if not isinstance(table, dict):
                continue
            fields = table.get("fields") or table.get("columns") or []
            rows = table.get("data") or []
            if isinstance(fields, list) and isinstance(rows, list):
                result.append(([str(x) for x in fields], rows))
    return result


def _find_index(payload: Any) -> dict[str, float | int | None]:
    result: dict[str, float | int | None] = {}
    for fields, rows in _tables(payload):
        joined = " ".join(fields)
        if "收盤指數" not in joined:
            continue
        for raw in rows:
            if not isinstance(raw, list) or len(raw) < len(fields):
                continue
            name = str(raw[0])
            if "發行量加權" not in name and "加權指數" not in name and "TAIEX" not in name.upper():
                continue
            values = {field: raw[index] for index, field in enumerate(fields)}
            result["index_close"] = _number(values.get("收盤指數"))
            return result
    return result


def _find_index_ohlc(payload: Any) -> dict[str, float | int | None]:
    if not isinstance(payload, dict):
        return {}
    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    try:
        index = [str(field) for field in fields].index("發行量加權股價指數")
        values = [_number(row[index]) for row in rows if isinstance(row, list) and len(row) > index]
        values = [value for value in values if value is not None]
    except (ValueError, TypeError):
        return {}
    if not values:
        return {}
    return {
        "index_open": values[0],
        "index_high": max(values),
        "index_low": min(values),
        "index_close": values[-1],
    }


def _find_margin(payload: Any) -> dict[str, float | int | None]:
    result: dict[str, float | int | None] = {}
    for fields, rows in _tables(payload):
        if "代號" in fields and "名稱" in fields:
            code_index = fields.index("代號")
            financing_previous = [index for index, field in enumerate(fields) if field == "前日餘額"]
            financing_today = [index for index, field in enumerate(fields) if field == "今日餘額"]
            if len(financing_previous) >= 2 and len(financing_today) >= 2:
                for raw in rows:
                    if not isinstance(raw, list) or len(raw) <= max(code_index, financing_today[1]):
                        continue
                    code = str(raw[code_index]).strip()
                    if code not in {"00631L", "0050"}:
                        continue
                    financing_prev = _integer(raw[financing_previous[0]])
                    financing_now = _integer(raw[financing_today[0]])
                    securities_prev = _integer(raw[financing_previous[1]])
                    securities_now = _integer(raw[financing_today[1]])
                    if code == "00631L":
                        result.update(
                            financing_change_00631l=(financing_now - financing_prev if financing_now is not None and financing_prev is not None else None),
                            securities_change_00631l=(securities_now - securities_prev if securities_now is not None and securities_prev is not None else None),
                        )
                    else:
                        result.update(
                            financing_change_0050=(financing_now - financing_prev if financing_now is not None and financing_prev is not None else None),
                            securities_change_0050=(securities_now - securities_prev if securities_now is not None and securities_prev is not None else None),
                        )
                if all(key in result for key in ("financing_change_00631l", "securities_change_00631l", "financing_change_0050", "securities_change_0050")):
                    return result
        for raw in rows:
            if not isinstance(raw, list):
                continue
            label = " ".join(str(value) for value in raw[:2])
            if "融資" not in label or "融券" in label:
                continue
            values = {field: raw[index] for index, field in enumerate(fields) if index < len(raw)}
            if "融資金額" in label:
                balance = _integer(values.get("今日餘額"))
                previous = _integer(values.get("前日餘額"))
                if balance is not None:
                    result.update(financing_balance=balance, financing_change=(balance - previous if previous is not None else None))
            if "融資維持率" in label:
                result["margin_maintenance"] = _number(raw[-1])
            if "融資餘額" in values:
                result.update(
                    financing_balance=_integer(values.get("融資餘額")),
                )
    return result


def _calculate_margin_maintenance(
    conn: Any,
    payload: Any,
    day: dt.date,
    financing_balance_thousand: int | None,
    cache_dir: str,
) -> dict[str, float | int | None]:
    """Estimate the market ratio from stock-level margin shares and closing prices.

    TWSE reports financing amount in thousand NTD and financing balance in shares.
    """
    if financing_balance_thousand is None or financing_balance_thousand <= 0:
        return {}
    prices = {
        str(row["code"]): float(row["close"])
        for row in conn.execute(
            "SELECT code, close FROM bars WHERE date=? AND market='TWSE'",
            (day.isoformat(),),
        ).fetchall()
    }
    if not prices:
        prices = {
            bar.code: float(bar.close)
            for bar in fetch_twse_daily_bars(day, cache_dir=cache_dir, use_cache=True)
        }
    if not prices:
        return {}
    numerator = 0.0
    found = 0
    for fields, rows in _tables(payload):
        if "代號" not in fields or "今日餘額" not in fields:
            continue
        code_index = fields.index("代號")
        balance_indices = [index for index, field in enumerate(fields) if field == "今日餘額"]
        if not balance_indices:
            continue
        balance_index = balance_indices[0]
        for raw in rows:
            if not isinstance(raw, list) or len(raw) <= max(code_index, balance_index):
                continue
            code = str(raw[code_index]).strip()
            balance = _integer(raw[balance_index])
            close = prices.get(code)
            if balance is not None and close is not None:
                numerator += balance * close
                found += 1
        break
    if found == 0:
        return {}
    # 融資交易單位為千股；乘 1,000 後才是股票市值（元）。
    numerator *= 1000
    denominator = financing_balance_thousand * 1000
    return {
        "margin_market_value": int(round(numerator)),
        "margin_maintenance": numerator / denominator * 100.0,
    }


def _find_market_volume(payload: Any) -> int | None:
    for fields, rows in _tables(payload):
        if "成交股數(股)" not in fields:
            continue
        volume_index = fields.index("成交股數(股)")
        for raw in rows:
            if isinstance(raw, list) and raw and "合計" in str(raw[0]):
                return _integer(raw[volume_index]) if volume_index < len(raw) else None
    return None


def _find_institution(payload: Any) -> dict[str, int | None]:
    if isinstance(payload, dict) and isinstance(payload.get("fields"), list) and isinstance(payload.get("data"), list):
        fields = [str(field) for field in payload["fields"]]
        indexes = {
            "foreign_net": fields.index("外陸資買賣超股數(不含外資自營商)"),
            "trust_net": fields.index("投信買賣超股數"),
            "dealer_net": fields.index("自營商買賣超股數"),
        }
        totals = {key: 0 for key in indexes}
        for raw in payload["data"]:
            if isinstance(raw, list):
                for key, index in indexes.items():
                    value = _integer(raw[index]) if index < len(raw) else None
                    if value is not None:
                        totals[key] += value
        return totals
    return {}


def _fetch(cache_dir: str, prefix: str, template: str, day: dt.date) -> Any:
    return _load_or_fetch_json_any(cache_dir, f"market_{prefix}_{day:%Y%m%d}", template.format(date=day.strftime("%Y%m%d")), use_cache=True)


def sync_market_overview(
    conn,
    *,
    end_date: dt.date,
    calendar_days: int = 60,
    recent_days: int = 14,
    cache_dir: str,
    on_log: Callable[[str], None] | None = None,
) -> tuple[dt.date | None, list[str]]:
    """Fetch missing calendar dates; endpoints returning no index data are treated as holidays."""
    errors: list[str] = []
    start = end_date - dt.timedelta(days=max(calendar_days * 3, recent_days + 10))
    yahoo_institutional: dict[str, dict[str, int]] = {}
    try:
        yahoo_institutional = _fetch_yahoo_institutional(cache_dir)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        errors.append(f"Yahoo 法人金額 {exc}")
    yahoo_rows: dict[str, dict[str, float | int | None]] = {}
    try:
        payload = _load_or_fetch_json_any(
            cache_dir,
            "market_yahoo_index_history",
            YAHOO_INDEX_URL.format(start=int(start.strftime("%s")), end=int((end_date + dt.timedelta(days=1)).strftime("%s"))),
            use_cache=True,
        )
        result = payload.get("chart", {}).get("result", [{}])[0] if isinstance(payload, dict) else {}
        timestamps = result.get("timestamp", [])
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        for index, timestamp in enumerate(timestamps):
            date_s = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).date().isoformat()
            yahoo_rows[date_s] = {"index_open": quote.get("open", [])[index], "index_high": quote.get("high", [])[index], "index_low": quote.get("low", [])[index], "index_close": quote.get("close", [])[index], "index_volume": quote.get("volume", [])[index]}
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, IndexError):
        pass
    dates: list[dt.date] = []
    day = end_date
    while day >= start and len(dates) < calendar_days * 2:
        if day.weekday() < 5:
            dates.append(day)
        day -= dt.timedelta(days=1)

    saved_count = 0
    for index, current in enumerate(dates):
        row: dict[str, object] = {"date": current.isoformat(), "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "source_status": ""}
        row.update(yahoo_rows.get(current.isoformat(), {}))
        try:
            if any(row.get(field) is None for field in ("index_open", "index_high", "index_low", "index_close")):
                row.update(_find_index_ohlc(_fetch(cache_dir, "index_intraday", TWSE_INDEX_INTRADAY_URL, current)))
            if row.get("index_close") is None:
                row.update(_find_index(_fetch(cache_dir, "index", TWSE_INDEX_URL, current)))
            if row.get("index_volume") is None:
                row["index_volume"] = _find_market_volume(_fetch(cache_dir, "market", TWSE_MARKET_URL, current))
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            errors.append(f"{current}: 加權指數 {exc}")
        if row.get("index_close") is None:
            continue

        if saved_count < recent_days:
            for key, prefix, url, parser in (("融資", "margin", TWSE_MARGIN_URL, _find_margin),):
                try:
                    row.update(parser(_fetch(cache_dir, prefix, url, current)))
                except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
                    errors.append(f"{current}: {key} {exc}")

            yahoo_values = yahoo_institutional.get(current.isoformat())
            if yahoo_values:
                row.update(yahoo_values)
            else:
                errors.append(f"{current}: Yahoo 法人金額資料缺少")

            try:
                margin_payload = _fetch(cache_dir, "margin", TWSE_MARGIN_URL, current)
                row.update(_calculate_margin_maintenance(conn, margin_payload, current, row.get("financing_balance"), cache_dir))
            except (OSError, RuntimeError, TypeError, ValueError, KeyError, IndexError) as exc:
                errors.append(f"{current}: 融資維持率 {exc}")

            try:
                row["futures_short_total"] = _fetch_taifex_foreign_net_short(cache_dir, current)
            except (OSError, RuntimeError, ValueError, IndexError) as exc:
                errors.append(f"{current}: 外資台指期淨空單 {exc}")

        # TAIFEX 的 HTML/下載端點格式不穩定，先保留欄位並明確記錄未提供。
        missing = [label for label, field in (
            ("外資台指期淨空單", "futures_short_total"),
            ("00631L融資融券", "financing_change_00631l"),
            ("00631L融券", "securities_change_00631l"),
            ("0050融資融券", "financing_change_0050"),
            ("0050融券", "securities_change_0050"),
        ) if row.get(field) is None]
        row["source_status"] = "；".join(missing)
        upsert_market_overview(conn, row)
        saved_count += 1
        if saved_count >= calendar_days:
            break
        if on_log:
            on_log(f"大盤 {index + 1}/{len(dates)}：{current}，{row['source_status'] or '完成'}")
        time.sleep(0.15)

    # Once the requested dates are present, compute current minus prior-day changes.
    futures_rows = conn.execute(
        "SELECT date, futures_short_total FROM market_overview WHERE futures_short_total IS NOT NULL ORDER BY date ASC"
    ).fetchall()
    previous = None
    with conn:
        for futures_row in futures_rows:
            current_value = int(futures_row["futures_short_total"])
            change = None if previous is None else current_value - previous
            conn.execute("UPDATE market_overview SET futures_short_change=? WHERE date=?", (change, futures_row["date"]))
            previous = current_value

    latest = None
    rows = conn.execute("SELECT date FROM market_overview WHERE index_close IS NOT NULL ORDER BY date DESC LIMIT 1").fetchone()
    if rows:
        latest = dt.date.fromisoformat(str(rows["date"]))
    return latest, errors
