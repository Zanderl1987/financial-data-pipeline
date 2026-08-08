#!/usr/bin/env python3
"""
Redfin Market Tracker Pipeline.

Downloads Redfin's Housing Market Tracker files from their public S3 bucket
(no API key). Data is published weekly (Thursday) as full-history gzipped
TSV files — one row per region/period/property-type, wide format with MOM
(month-over-month) and YOY (year-over-year) columns.

Default scope is national + metro + state (~121 MB gz total). Pass
--granularity all to additionally pull county, city, and zip_code levels
(combined ~2.9 GB gz). County/city/zip rows are partitioned from the same
schema as metro.

CLI:
  python redfin_pipeline.py                 # national + metro + state
  python redfin_pipeline.py --granularity all
  python redfin_pipeline.py --only national

Outputs:
  storage/raw/redfin/market_tracker/redfin_market_tracker_{level}_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import io
import os
import time

import pandas as pd
import requests

from storage_utils import write_partitioned

OUT_DIR = os.path.join("storage", "raw", "redfin", "market_tracker")

BASE_URL = "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker"

REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3

LEVEL_FILES = {
    "national": "us_national_market_tracker.tsv000.gz",
    "metro":    "redfin_metro_market_tracker.tsv000.gz",
    "state":    "state_market_tracker.tsv000.gz",
    "county":   "county_market_tracker.tsv000.gz",
    "city":     "city_market_tracker.tsv000.gz",
    "zip_code": "zip_code_market_tracker.tsv000.gz",
}

DEFAULT_LEVELS = ["national", "metro", "state"]

# Columns that are purely identifiers / dates — everything else is numeric.
TEXT_COLS = {
    "period_begin", "period_end", "region_type", "region", "city", "state",
    "state_code", "property_type", "parent_metro_region", "last_updated",
}
DATE_COLS = {"period_begin", "period_end"}


def _get_with_retry(url: str) -> bytes | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, timeout=600)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
            print(f"  HTTP {r.status_code} or short response (attempt {attempt}).")
        except requests.RequestException as e:
            print(f"  Error (attempt {attempt}): {e}")
        time.sleep(REQUEST_INTERVAL)
    return None


def fetch_level(level: str, url: str) -> pd.DataFrame | None:
    print(f"[redfin] Downloading {level} ({url.split('/')[-1]})...")
    content = _get_with_retry(url)
    if content is None:
        print(f"  Failed to download {level}.")
        return None

    df = pd.read_csv(io.BytesIO(content), sep="\t", compression="gzip", low_memory=False)
    df.columns = [c.lower().strip() for c in df.columns]

    for col in DATE_COLS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Cast every non-text column to numeric (levels, codes, metrics, MOM/YOY).
    for col in df.columns:
        if col not in TEXT_COLS and col not in DATE_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["period_begin", "region"])
    df["region_level"] = level
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    print(f"  Parsed {len(df):,} rows ({df['region'].nunique()} regions).")
    return df


def main():
    parser = argparse.ArgumentParser(description="Redfin Housing Market Tracker pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Same as default — full history is always included")
    parser.add_argument("--only", choices=sorted(LEVEL_FILES.keys()),
                        help="Run only one region level")
    parser.add_argument("--granularity", choices=["default", "all"], default="default",
                        help="'default' = national+metro+state; 'all' adds county/city/zip_code")
    args = parser.parse_args()

    levels = list(LEVEL_FILES.keys()) if args.granularity == "all" else list(DEFAULT_LEVELS)
    if args.only:
        levels = [args.only]

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"

    os.makedirs(OUT_DIR, exist_ok=True)

    for level in levels:
        frame = fetch_level(level, f"{BASE_URL}/{LEVEL_FILES[level]}")
        if frame is not None and not frame.empty:
            path = write_partitioned(frame, OUT_DIR, f"redfin_market_tracker_{level}_{mode}_{today_str}.parquet")
            print(f"  -> {path}")

    print("\n--- REDFIN PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
