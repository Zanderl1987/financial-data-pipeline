#!/usr/bin/env python3
"""
Indeed Hiring Lab Pipeline — US Job Postings Index.

Downloads Indeed's Hiring Lab job-postings tracker (national, sector, and
state level) directly from its public GitHub repo. No API key, no auth.

Source: https://github.com/hiring-lab/job_postings_tracker

CLI:
  python indeed_hiringlab_pipeline.py             # full history (always included)
  python indeed_hiringlab_pipeline.py --backfill  # same -- no separate backfill mode

Output:
  storage/raw/indeed_hiringlab/national/indeed_hiringlab_national_{mode}_{YYYYMMDD}.parquet
  storage/raw/indeed_hiringlab/sector/indeed_hiringlab_sector_{mode}_{YYYYMMDD}.parquet
  storage/raw/indeed_hiringlab/state/indeed_hiringlab_state_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import io
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR = os.path.join("storage", "raw", "indeed_hiringlab")
MAX_RETRIES = 3
BACKOFF_SECONDS = 30
REQUEST_INTERVAL = 0.5

RAW_BASE = "https://raw.githubusercontent.com/hiring-lab/job_postings_tracker/master/US"

SOURCES = {
    "national": f"{RAW_BASE}/aggregate_job_postings_US.csv",
    "sector":   f"{RAW_BASE}/job_postings_by_sector_US.csv",
    "state":    f"{RAW_BASE}/state_job_postings_us.csv",
}


def _get_with_backoff(url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from GitHub -- backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code} for {url}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts: {url}")
    return None


def fetch_csv(url):
    """Download and parse one Hiring Lab CSV. Returns a DataFrame or None."""
    r = _get_with_backoff(url)
    if r is None:
        return None
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip().lower() for c in df.columns]
    return df


def main(backfill=False):
    os.makedirs(BASE_DIR, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode = "backfill" if backfill else "incremental"

    print(f"Indeed Hiring Lab Pipeline  mode={mode}")
    print("(source CSVs contain full history since 2020-02-01 on every pull)")

    total_rows = 0
    for name, url in SOURCES.items():
        print(f"\n[{name}] {url}")
        df = fetch_csv(url)
        if df is None or df.empty:
            print(f"  No data returned for {name}.")
            continue

        if name == "sector":
            df = df.rename(columns={"display_name": "sector"})

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df["fetched_at"] = fetched_at

        out_dir = os.path.join(BASE_DIR, name)
        path = write_partitioned(
            df, out_dir,
            f"indeed_hiringlab_{name}_{mode}_{today_str}.parquet",
        )
        total_rows += len(df)
        print(f"  -> {path} ({len(df):,} rows, {df['date'].min().date()} to {df['date'].max().date()})")
        time.sleep(REQUEST_INTERVAL)

    print(f"\nTotal: {total_rows:,} rows across {len(SOURCES)} datasets")
    print("\n--- INDEED HIRING LAB PIPELINE COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Indeed Hiring Lab job-postings index pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Same as default -- full history is always included")
    args = parser.parse_args()
    main(backfill=args.backfill)
