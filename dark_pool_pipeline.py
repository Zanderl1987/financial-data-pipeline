#!/usr/bin/env python3
"""
Dark Pool (ATS) Volume Pipeline — FINRA weekly ATS/OTC summary.

Fetches aggregate weekly dark pool / ATS trading volume per market
participant (firm) from FINRA's public data-group API. No API key required.

Rewritten 2026-08-01: the old otctransparency.finra.org REST path now serves
the site's Angular SPA shell (HTTP 200, text/html) instead of JSON for every
request -- FINRA retired that endpoint and moved this dataset behind
api.finra.org's data-group gateway (dataset "weeklySummary" in group
"otcMarket"), which is POST-based, keyless, and paginated (max 5,000 rows
per request via limit/offset).

Source: https://api.finra.org/data/group/otcMarket/name/weeklySummary

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
BASE_URL = "https://api.finra.org/data/group/otcMarket/name/weeklySummary"
PAGE_LIMIT = 5000
REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3
BACKOFF_SECONDS = 60


def _post_with_backoff(url, body):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, json=body, headers={"Accept": "application/json"}, timeout=30)
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


def fetch_weekly_summary(start_date, end_date):
    """Paginate through weeklySummary for [start_date, end_date], both YYYY-MM-DD."""
    records = []
    offset = 0
    while True:
        body = {
            "limit": PAGE_LIMIT,
            "offset": offset,
            "dateRangeFilters": [
                {"fieldName": "weekStartDate", "startDate": start_date, "endDate": end_date}
            ],
        }
        resp = _post_with_backoff(BASE_URL, body)
        if resp is None:
            break
        try:
            page = resp.json()
        except Exception:
            break
        if not page:
            break
        records.extend(page)
        total = int(resp.headers.get("record-total", len(records)))
        print(f"    {len(records):,}/{total:,} rows...")
        if len(page) < PAGE_LIMIT or len(records) >= total:
            break
        offset += PAGE_LIMIT
        time.sleep(REQUEST_INTERVAL)
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

    records = fetch_weekly_summary(start_date.isoformat(), end_date.isoformat())
    if not records:
        print("[!] No dark pool data returned.")
        return

    df = pd.DataFrame(records)
    df = df.rename(columns={"weekStartDate": "trade_date"})
    df["fetched_at"] = fetched_at

    path = write_partitioned(
        df, OUTPUT_DIR,
        f"dark_pool_{mode}_{today_str}.parquet",
    )
    print(f"[+] {path} ({len(df):,} rows)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FINRA weekly ATS/dark-pool volume pipeline")
    parser.add_argument("--backfill", action="store_true", help="Full year of history")
    args = parser.parse_args()
    main(backfill=args.backfill)
