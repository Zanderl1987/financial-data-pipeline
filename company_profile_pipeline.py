#!/usr/bin/env python3
"""
Company Profile Pipeline:
  Snapshot of company descriptive/reference data -- business description,
  sector, industry, website, employee count, HQ address -- for the broad
  S&P 500 universe (via symbol_universe.get_broad_universe()).

  Source: yfinance Ticker.info (free, keyless). Complements the existing
  `securities` reference table (name/sector/industry/market-cap/index
  membership) with the free-text business description and HQ details that
  no other pipeline in this repo captures. Finnhub's /stock/profile2
  (finnhub_profile table) does NOT include a description field.

  Snapshot-only, like schwab_quotes -- no history dimension, run this
  periodically to refresh. curated.py dedups to one row per symbol.

CLI:
  python company_profile_pipeline.py                    # broad universe (S&P 500)
  python company_profile_pipeline.py --symbols AAPL MSFT # override universe

Output:
  storage/raw/company_profile/year=YYYY/month=MM/company_profile_{YYYYMMDD}.parquet
  CATALOG table: company_profile
"""

import argparse
import datetime
import os
import time

import pandas as pd
import yfinance as yf

from storage_utils import write_partitioned

OUTPUT_DIR = os.path.join("storage", "raw", "company_profile")

REQUEST_GAP = 0.3  # seconds between symbols -- be polite, avoid throttling
MAX_RETRIES = 3
BACKOFF_SECONDS = 15


def fetch_profile(symbol: str) -> dict | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            info = yf.Ticker(symbol).info
            if not info or not info.get("longName"):
                return None
            return {
                "symbol":       symbol,
                "company_name": info.get("longName") or info.get("shortName"),
                "description":  info.get("longBusinessSummary"),
                "sector":       info.get("sector"),
                "industry":     info.get("industry"),
                "website":      info.get("website"),
                "employees":    info.get("fullTimeEmployees"),
                "address":      info.get("address1"),
                "city":         info.get("city"),
                "state":        info.get("state"),
                "country":      info.get("country"),
                "exchange":     info.get("exchange"),
                "currency":     info.get("currency"),
                "fetched_at":   datetime.datetime.utcnow().isoformat(),
            }
        except Exception as e:
            if attempt == MAX_RETRIES:
                print(f"  {symbol}: ERROR — {e}")
                return None
            time.sleep(BACKOFF_SECONDS)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Company profile / description pipeline (yfinance, no API key)")
    parser.add_argument("--symbols", nargs="+", default=None,
                         help="Specific symbols to fetch (default: broad S&P 500 universe).")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols]
    else:
        from symbol_universe import get_broad_universe
        symbols = get_broad_universe()

    print(f"Company Profile Pipeline  symbols={len(symbols)}\n")

    rows = []
    for i, symbol in enumerate(symbols, 1):
        row = fetch_profile(symbol)
        if row:
            rows.append(row)
        else:
            print(f"  {symbol}: no data")
        if i % 50 == 0:
            print(f"  Progress: {i}/{len(symbols)}")
        time.sleep(REQUEST_GAP)

    if not rows:
        print("  No data retrieved")
        return

    df = pd.DataFrame(rows)
    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    out_path = write_partitioned(df, OUTPUT_DIR, f"company_profile_{today_str}.parquet")

    print(f"\n--- COMPANY PROFILE PIPELINE COMPLETE ---")
    print(f"Saved {len(df)} rows -> {out_path}")
    with_desc = df["description"].notna().sum()
    print(f"  {with_desc}/{len(df)} rows have a business description")


if __name__ == "__main__":
    main()
