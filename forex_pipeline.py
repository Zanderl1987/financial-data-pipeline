#!/usr/bin/env python3
"""
Frankfurter Forex Pipeline.

Fetches historical foreign exchange rates from Frankfurter (api.frankfurter.app),
sourced from the European Central Bank (201 currencies, 84 central banks).
Completely keyless — no API key required.

Tracks 19 currencies vs USD (base):
  EUR GBP JPY CAD AUD CHF CNY INR MXN BRL KRW SGD HKD NOK SEK DKK NZD ZAR TRY

CLI:
  python forex_pipeline.py             # last 90 days
  python forex_pipeline.py --backfill  # full history since 1999 (ECB launch)

Outputs:
  storage/raw/forex/forex_rates_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_URL        = "https://api.frankfurter.app"
FOREX_DIR       = os.path.join("storage", "raw", "forex")
REQUEST_INTERVAL = 0.5
MAX_RETRIES     = 3
BASE_CURRENCY   = "USD"

TARGET_CURRENCIES = [
    "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "INR",
    "MXN", "BRL", "KRW", "SGD", "HKD", "NOK", "SEK", "DKK",
    "NZD", "ZAR", "TRY",
]


def _get(url, params=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  429 rate limit. Backing off {wait}s.")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(15 * attempt)
    return None


def fetch_range(start_date, end_date):
    """Fetch daily rates for a date range. Returns {date: {currency: rate}}."""
    url  = f"{BASE_URL}/{start_date}..{end_date}"
    data = _get(url, params={
        "from": BASE_CURRENCY,
        "to":   ",".join(TARGET_CURRENCIES),
    })
    time.sleep(REQUEST_INTERVAL)
    if not data:
        return {}
    return data.get("rates", {})


def rates_to_df(rates_by_date, fetched_at):
    rows = []
    for date_str, currencies in rates_by_date.items():
        for currency, rate in currencies.items():
            rows.append({
                "base":       BASE_CURRENCY,
                "currency":   currency,
                "pair":       f"{BASE_CURRENCY}/{currency}",
                "date":       date_str,
                "rate":       rate,
                "fetched_at": fetched_at,
            })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="Frankfurter forex rates pipeline (keyless ECB data)")
    parser.add_argument("--backfill", action="store_true",
                        help="Full history since 1999-01-04 (ECB data launch)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today      = now.date()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"

    os.makedirs(FOREX_DIR, exist_ok=True)

    if args.backfill:
        # Chunk by year — Frankfurter handles multi-year ranges but year-chunks keep responses lean
        chunks = []
        year = 1999
        while year <= today.year:
            chunk_end = f"{year}-12-31" if year < today.year else today.strftime("%Y-%m-%d")
            chunks.append((f"{year}-01-01" if year > 1999 else "1999-01-04", chunk_end))
            year += 1
    else:
        start = today - datetime.timedelta(days=90)
        chunks = [(start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))]

    print(f"Frankfurter Forex Pipeline  mode={mode}  chunks={len(chunks)}")
    print(f"Currencies: {', '.join(TARGET_CURRENCIES)}\n")

    all_frames = []
    for i, (start_d, end_d) in enumerate(chunks, 1):
        print(f"  [{i}/{len(chunks)}] {start_d} .. {end_d}")
        rates = fetch_range(start_d, end_d)
        if rates:
            all_frames.append(rates_to_df(rates, fetched_at))

    if not all_frames:
        print("No data returned.")
        return

    combined = (
        pd.concat(all_frames, ignore_index=True)
        .drop_duplicates(subset=["pair", "date"])
        .sort_values(["pair", "date"])
        .reset_index(drop=True)
    )

    path = write_partitioned(combined, FOREX_DIR,
                             f"forex_rates_{mode}_{today_str}.parquet")
    print(f"\n-> {path}  ({len(combined):,} rows, {combined['currency'].nunique()} currencies)")
    print("\n--- FOREX PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
