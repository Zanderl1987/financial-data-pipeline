#!/usr/bin/env python3
"""
TSA Checkpoint Travel Volumes Pipeline.

Daily count of travelers screened at TSA checkpoints nationwide — a
high-frequency leading indicator of air travel demand / transportation
activity. Published as an HTML table with no API; the current-year page
carries the latest data and past years are archived under /{YYYY}.

No API key required.

CLI:
  python tsa_pipeline.py             # current year only
  python tsa_pipeline.py --backfill  # full history (2019+)

Output:
  storage/raw/tsa/year=YYYY/month=MM/tsa_checkpoint_{mode}_{date}.parquet
"""

import argparse
import datetime
import io
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR    = "storage/raw/tsa"
BASE_URL    = "https://www.tsa.gov/travel/passenger-volumes"
HEADERS     = {"User-Agent": "Mozilla/5.0 financial-data-pipeline/1.0"}
REQUEST_GAP = 1.0
FIRST_YEAR  = 2019  # earliest year TSA publishes an archive page for


def fetch_year(year: int | None) -> pd.DataFrame:
    """year=None fetches the current-year page (has the latest data)."""
    url = BASE_URL if year is None else f"{BASE_URL}/{year}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    tables = pd.read_html(io.StringIO(resp.text))
    df = tables[0]
    df.columns = [c.strip().lower() for c in df.columns]
    df = df.rename(columns={"numbers": "travelers"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["travelers"] = pd.to_numeric(df["travelers"], errors="coerce")
    df = df.dropna(subset=["date", "travelers"])
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    df["travelers"] = df["travelers"].astype("int64")
    return df[["date", "travelers"]]


def main() -> None:
    parser = argparse.ArgumentParser(description="TSA checkpoint travel volumes")
    parser.add_argument("--backfill", action="store_true", help=f"Full history ({FIRST_YEAR}+)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"

    import os
    os.makedirs(BASE_DIR, exist_ok=True)

    print(f"TSA Checkpoint Pipeline  mode={mode}\n")
    print("[tsa_checkpoint]")

    frames = []
    try:
        current = fetch_year(None)
        print(f"  current year: {len(current):,} rows")
        frames.append(current)
    except Exception as exc:
        print(f"  current year: ERROR — {exc}")

    if args.backfill:
        current_year = now.year
        for year in range(FIRST_YEAR, current_year):
            time.sleep(REQUEST_GAP)
            try:
                df = fetch_year(year)
                print(f"  {year}: {len(df):,} rows")
                frames.append(df)
            except Exception as exc:
                print(f"  {year}: ERROR — {exc}")

    if not frames:
        print("  No data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    combined["fetched_at"] = fetched_at
    path = write_partitioned(combined, BASE_DIR, f"tsa_checkpoint_{mode}_{today_str}.parquet")
    print(f"  -> {path}  ({len(combined):,} rows)")

    print("\n--- TSA PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
