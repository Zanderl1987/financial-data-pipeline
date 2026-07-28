#!/usr/bin/env python3
"""
Dark Pool (ATS) Volume Pipeline — FINRA OTC Transparency.

Fetches aggregate dark pool / ATS trading volume data from FINRA's OTC
Transparency API. No API key required.

Source: https://otctransparency.finra.org

CLI:
  python dark_pool_pipeline.py             # incremental (last 30 days)
  python dark_pool_pipeline.py --backfill  # full year of history

Output:
  storage/raw/dark_pool/year=YYYY/month=MM/dark_pool_{mode}_{YYYYMMDD}.parquet
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

OUTPUT_DIR = os.path.join("storage", "raw", "dark_pool")
BASE_URL = "https://otctransparency.finra.org/otc/transparency/api/daily/aggregate"
REQUEST_INTERVAL = 1.0
MAX_RETRIES = 3
BACKOFF_SECONDS = 60


def _get_with_backoff(url: str, params) -> requests.Response | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from FINRA -- backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code}: {r.text[:200]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts.")
    return None


def fetch_dark_pool_data(trade_date=None):
    """Fetch dark pool volume data for a given date from the FINRA OTC Transparency API."""
    params = {}
    if trade_date:
        params["startDate"] = trade_date
        params["endDate"] = trade_date
    return _get_with_backoff(BASE_URL, params)


def parse_response(raw_resp, trade_date):
    """Parse raw response into a list of row dicts."""
    if raw_resp is None:
        return []
    try:
        body = raw_resp.json()
    except Exception:
        return []

    records = []
    data = body if isinstance(body, list) else body.get("data", [])
    for record in data:
        if isinstance(record, dict):
            record["trade_date"] = trade_date
            records.append(record)
    return records


def main(backfill=False):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode = "backfill" if backfill else "incremental"

    end_date = now.date()
    start_date = end_date - datetime.timedelta(days=365 if backfill else 30)

    print(f"Dark Pool Pipeline  mode={mode}")
    print(f"Date range: {start_date} to {end_date}")

    all_records = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            raw = fetch_dark_pool_data(current.isoformat())
            records = parse_response(raw, current.isoformat())
            all_records.extend(records)
        current += datetime.timedelta(days=1)
        time.sleep(REQUEST_INTERVAL)

    if not all_records:
        print("[!] No dark pool data returned.")
        return

    df = pd.DataFrame(all_records)
    df["fetched_at"] = fetched_at

    path = write_partitioned(
        df, OUTPUT_DIR,
        f"dark_pool_{mode}_{today_str}.parquet",
    )
    print(f"[+] {path} ({len(df):,} rows)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FINRA dark pool (ATS) volume data pipeline")
    parser.add_argument("--backfill", action="store_true", help="Full year of history")
    args = parser.parse_args()
    main(backfill=args.backfill)
