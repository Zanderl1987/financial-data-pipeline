#!/usr/bin/env python3
"""
Nasdaq Data Link (formerly Quandl) Pipeline.

Pulls two sets of free datasets using NASDAQ_DATA_LINK_API_KEY:

  market_valuation    (MULTPL series — long-run S&P 500 valuation metrics):
    MULTPL/SHILLER_PE_RATIO_MONTH  — Shiller CAPE ratio, monthly
    MULTPL/SP500_DIV_YIELD_MONTH   — S&P 500 dividend yield, monthly
    MULTPL/SP500_EARNINGS_YIELD_MONTH — earnings yield, monthly
    MULTPL/SP500_PE_RATIO_MONTH    — trailing P/E ratio, monthly
    MULTPL/SP500_REAL_PRICE_MONTH  — CPI-adjusted S&P 500 price, monthly

  treasury_yield_curve (USTREASURY/YIELD — daily full yield curve):
    1mo, 3mo, 6mo, 1yr, 2yr, 3yr, 5yr, 7yr, 10yr, 20yr, 30yr

Requires: NASDAQ_DATA_LINK_API_KEY in .env

CLI:
  python nasdaq_data_link_pipeline.py             # last 5 years
  python nasdaq_data_link_pipeline.py --backfill  # full history

Outputs:
  storage/raw/nasdaq_data_link/valuation/year=YYYY/month=MM/market_valuation_{mode}_{date}.parquet
  storage/raw/nasdaq_data_link/yield_curve/year=YYYY/month=MM/treasury_yield_curve_{mode}_{date}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from storage_utils import write_partitioned

load_dotenv()

API_KEY  = os.environ.get("NASDAQ_DATA_LINK_API_KEY", "")
BASE_URL = "https://data.nasdaq.com/api/v3/datasets"
BASE_DIR = "storage/raw/nasdaq_data_link"
REQUEST_GAP = 0.5

MULTPL_SERIES = [
    ("MULTPL/SHILLER_PE_RATIO_MONTH",      "shiller_pe",     "Shiller CAPE ratio"),
    ("MULTPL/SP500_DIV_YIELD_MONTH",       "sp500_div_yield","S&P 500 dividend yield (%)"),
    ("MULTPL/SP500_EARNINGS_YIELD_MONTH",  "sp500_ey",       "S&P 500 earnings yield (%)"),
    ("MULTPL/SP500_PE_RATIO_MONTH",        "sp500_pe",       "S&P 500 trailing P/E"),
    ("MULTPL/SP500_REAL_PRICE_MONTH",      "sp500_real",     "S&P 500 CPI-adjusted price"),
]

YIELD_CURVE_CODE = "USTREASURY/YIELD"
YIELD_CURVE_COLS = {
    "1 Mo":  "1mo",  "3 Mo":  "3mo",  "6 Mo":  "6mo",
    "1 Yr":  "1yr",  "2 Yr":  "2yr",  "3 Yr":  "3yr",
    "5 Yr":  "5yr",  "7 Yr":  "7yr",  "10 Yr": "10yr",
    "20 Yr": "20yr", "30 Yr": "30yr",
}


def _fetch_dataset(code: str, start_date: str | None = None) -> pd.DataFrame:
    params = {"api_key": API_KEY, "order": "asc"}
    if start_date:
        params["start_date"] = start_date
    url = f"{BASE_URL}/{code}.json"
    resp = requests.get(url, params=params, timeout=60)
    if resp.status_code == 404:
        print(f"    {code}: 404 not found")
        return pd.DataFrame()
    resp.raise_for_status()
    body = resp.json().get("dataset", {})
    cols = body.get("column_names", [])
    data = body.get("data", [])
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data, columns=cols)


def run_valuation(mode: str, start_date: str | None, today_str: str, fetched_at: str) -> None:
    os.makedirs(f"{BASE_DIR}/valuation", exist_ok=True)
    frames = []
    for code, series_id, desc in MULTPL_SERIES:
        try:
            df = _fetch_dataset(code, start_date)
            if df.empty:
                print(f"  {code}: no data")
                continue
            df.columns = [c.strip() for c in df.columns]
            date_col  = df.columns[0]
            value_col = df.columns[1]
            df = df.rename(columns={date_col: "date", value_col: "value"})
            df["date"]      = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            df["value"]     = pd.to_numeric(df["value"], errors="coerce")
            df["series"]    = series_id
            df["series_desc"] = desc
            df = df.dropna(subset=["date", "value"])
            print(f"  {code}: {len(df):,} rows")
            frames.append(df[["date", "series", "series_desc", "value"]])
        except Exception as exc:
            print(f"  {code}: ERROR — {exc}")
        time.sleep(REQUEST_GAP)

    if not frames:
        print("  No valuation data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["fetched_at"] = fetched_at
    path = write_partitioned(combined, f"{BASE_DIR}/valuation",
                             f"market_valuation_{mode}_{today_str}.parquet")
    print(f"  -> {path}  ({len(combined):,} rows)\n")


def run_yield_curve(mode: str, start_date: str | None, today_str: str, fetched_at: str) -> None:
    os.makedirs(f"{BASE_DIR}/yield_curve", exist_ok=True)
    try:
        df = _fetch_dataset(YIELD_CURVE_CODE, start_date)
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return

    if df.empty:
        print("  No yield curve data")
        return

    df.columns = [c.strip() for c in df.columns]
    date_col = df.columns[0]
    df = df.rename(columns={date_col: "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["date"])

    # Rename tenor columns
    df = df.rename(columns={k: v for k, v in YIELD_CURVE_COLS.items() if k in df.columns})
    for col in YIELD_CURVE_COLS.values():
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["fetched_at"] = fetched_at
    print(f"  USTREASURY/YIELD: {len(df):,} rows")
    path = write_partitioned(df, f"{BASE_DIR}/yield_curve",
                             f"treasury_yield_curve_{mode}_{today_str}.parquet")
    print(f"  -> {path}  ({len(df):,} rows)\n")


def main() -> None:
    if not API_KEY:
        print("NASDAQ_DATA_LINK_API_KEY not set in .env")
        return

    parser = argparse.ArgumentParser(description="Nasdaq Data Link: market valuation + yield curve")
    parser.add_argument("--backfill", action="store_true", help="Full history")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"
    start_date = None if args.backfill else f"{now.year - 5}-01-01"

    print(f"Nasdaq Data Link Pipeline  mode={mode}\n")

    print("[market_valuation]")
    run_valuation(mode, start_date, today_str, fetched_at)

    print("[treasury_yield_curve]")
    run_yield_curve(mode, start_date, today_str, fetched_at)

    print("--- NASDAQ DATA LINK PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
