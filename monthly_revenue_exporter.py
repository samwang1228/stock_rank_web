#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from io import StringIO
from typing import Literal

import pandas as pd
import requests

Market = Literal["tse", "otc", "rotc"]


@dataclass(frozen=True)
class MonthlyRevenueExporter:
    """下載指定年月的月營收（上市/上櫃/興櫃）並輸出 CSV。

    - 資料來源：公開資訊觀測站（MOPS）
    - 市場代碼：
      - tse  -> 上市（sii）
      - otc  -> 上櫃（otc）
      - rotc -> 興櫃（rotc）
    """

    out_dir: str = "web"
    timeout_seconds: int = 30
    user_agent: str = "Mozilla/5.0"

    def fetch(self, year: int, month: int, market: Market) -> pd.DataFrame:
        roc_year, ad_year = self._normalize_year(year)
        month = self._normalize_month(month)
        market_dir = self._market_dir(market)

        urls = [
            f"https://mopsov.twse.com.tw/nas/t21/{market_dir}/t21sc03_{roc_year}_{month}_0.html",
            f"https://mopsov.twse.com.tw/nas/t21/{market_dir}/t21sc03_{roc_year}_{month}.html",
        ]

        last_error: Exception | None = None
        for url in urls:
            try:
                df = self._download_and_parse(url)
                df = self._clean_dataframe(df)
                df.attrs["ad_year"] = ad_year
                df.attrs["roc_year"] = roc_year
                df.attrs["month"] = month
                df.attrs["market"] = market
                df.attrs["source_url"] = url
                return df
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue

        raise RuntimeError(
            f"無法取得 {ad_year}-{month:02d} {market} 月營收；"
            f"已嘗試 {len(urls)} 個 URL。最後錯誤：{last_error}"
        )

    def save_csv(self, df: pd.DataFrame, year: int, month: int, market: Market) -> str:
        _, ad_year = self._normalize_year(year)
        month = self._normalize_month(month)
        os.makedirs(self.out_dir, exist_ok=True)

        filename = f"{market}_{ad_year}_{month:02d}.csv"
        path = os.path.join(self.out_dir, filename)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path

    def fetch_and_save(self, year: int, month: int, market: Market) -> str:
        df = self.fetch(year=year, month=month, market=market)
        return self.save_csv(df=df, year=year, month=month, market=market)

    def _download_and_parse(self, url: str) -> pd.DataFrame:
        headers = {"User-Agent": self.user_agent}
        r = requests.get(url, headers=headers, timeout=self.timeout_seconds)
        r.raise_for_status()
        r.encoding = "big5"

        dfs = pd.read_html(StringIO(r.text), encoding="big-5")
        candidates = [d for d in dfs if d.shape[1] <= 11 and d.shape[1] > 5]
        if not candidates:
            raise ValueError("找不到符合格式的營收表格")
        return pd.concat(candidates)

    def _clean_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(1)
            df.columns = [str(col).replace(" ", "") for col in df.columns]
        else:
            df = df[list(range(0, 10))]
            header_row_idx = df.index[(df[0] == "公司代號")][0]
            df.columns = df.iloc[header_row_idx]

        if "當月營收" in df.columns:
            df["當月營收"] = pd.to_numeric(df["當月營收"], errors="coerce")
            df = df[~df["當月營收"].isnull()]

        if "公司代號" in df.columns:
            df = df[df["公司代號"] != "合計"]

        percentage_columns = ["上月比較增減(%)", "去年同月增減(%)", "前期比較增減(%)"]
        for col in percentage_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        revenue_columns = ["上月營收", "去年當月營收", "當月累計營收", "去年累計營收"]
        for col in revenue_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    @staticmethod
    def _normalize_month(month: int) -> int:
        if not 1 <= int(month) <= 12:
            raise ValueError("month 必須介於 1~12")
        return int(month)

    @staticmethod
    def _normalize_year(year: int) -> tuple[int, int]:
        """回傳 (民國年, 西元年)。

        - 若 year > 1990 視為西元年
        - 否則視為民國年
        """
        year = int(year)
        if year > 1990:
            return year - 1911, year
        return year, year + 1911

    @staticmethod
    def _market_dir(market: Market) -> str:
        if market == "tse":
            return "sii"
        if market == "otc":
            return "otc"
        if market == "rotc":
            return "rotc"
        raise ValueError(f"不支援的 market: {market}")


def _parse_market(value: str) -> Market:
    v = value.strip().lower()
    aliases = {
        "tse": "tse",
        "sii": "tse",
        "上市": "tse",
        "otc": "otc",
        "上櫃": "otc",
        "rotc": "rotc",
        "興櫃": "rotc",
    }
    if v not in aliases:
        raise argparse.ArgumentTypeError("market 只能是 tse/otc/rotc（或 中文：上市/上櫃/興櫃）")
    return aliases[v]  # type: ignore[return-value]


def main() -> int:
    parser = argparse.ArgumentParser(description="下載指定年月月營收並存成 CSV")
    parser.add_argument("--year", type=int, required=True, help="西元年(如 2024) 或民國年(如 113)")
    parser.add_argument("--month", type=int, required=True, help="月份 1~12")
    parser.add_argument("--market", type=_parse_market, required=True, help="tse/otc/rotc 或 上市/上櫃/興櫃")
    parser.add_argument("--out-dir", type=str, default="revenue", help="輸出資料夾（預設 revenue）")
    args = parser.parse_args()

    exporter = MonthlyRevenueExporter(out_dir=args.out_dir)
    path = exporter.fetch_and_save(year=args.year, month=args.month, market=args.market)
    print(f"已輸出: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
