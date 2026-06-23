#!/usr/bin/env python3
"""
NY Fed SOMA (System Open Market Account) Holdings Pipeline.

Fetches the Federal Reserve's weekly balance sheet holdings from the
NY Fed public API (no key required):
  - Treasury securities: bills, notes, bonds, TIPS, FRNs
  - Agency MBS and Agency debt

The full available date list is fetched first, then each report date
is requested individually. In backfill mode this covers ~2002 to present
(~1,200+ weekly reports). Incremental mode fetches the last 10 weeks.

API: https://markets.newyorkfed.org/api/soma/

CLI:
  python fed_soma_pipeline.py             # last 10 weekly reports
  python fed_soma_pipeline.py --backfill  # full history (~2002+)

Output:
  storage/raw/fed_soma/year=YYYY/month=MM/fed_soma_{mode}_{date}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_URL    = "https://markets.newyorkfed.org/api/soma"
BASE_DIR    = "storage/raw/fed_soma"
REQUEST_GAP = 0.4   # seconds between date requests


def _get_dates(asset_type: str) -> list[str]:
    """Return sorted list of available report date strings (YYYY-MM-DD)."""
    resp = requests.get(f"{BASE_URL}/{asset_type}/get/dates.json", timeout=30)
    resp.raise_for_status()
    dates = resp.json().get("soma", {}).get("dates", [])
    return sorted(dates)


def _fetch_asof(asset_type: str, date: str) -> list[dict]:
    url = f"{BASE_URL}/{asset_type}/get/all/asof/{date}.json"
    resp = requests.get(url, timeout=30)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    holdings = resp.json().get("soma", {}).get("holdings", [])
    for h in holdings:
        h["as_of_date"] = date
        h["asset_type"]  = asset_type
    return holdings


def fetch_all(asset_type: str, dates: list[str], fetched_at: str) -> pd.DataFrame:
    all_rows = []
    for i, date in enumerate(dates):
        rows = _fetch_asof(asset_type, date)
        all_rows.extend(rows)
        if (i + 1) % 50 == 0:
            print(f"    {asset_type}: {i+1}/{len(dates)} dates processed ({len(all_rows):,} rows)")
        time.sleep(REQUEST_GAP)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df.columns = [c.lower() for c in df.columns]

    # Normalise numeric columns
    for col in ("facevalue", "parvalue", "inflationcompensation", "coupon"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Normalise date columns
    for col in ("maturitydate", "as_of_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")

    df["fetched_at"] = fetched_at
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="NY Fed SOMA balance sheet holdings")
    parser.add_argument("--backfill", action="store_true", help="Full history (~2002+)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"

    os.makedirs(BASE_DIR, exist_ok=True)

    print(f"NY Fed SOMA Pipeline  mode={mode}\n")
    print("[fed_soma]")

    frames = []
    for asset_type in ("tsy", "agency"):
        try:
            print(f"  Fetching available {asset_type} dates...")
            all_dates = _get_dates(asset_type)
            if not all_dates:
                print(f"  {asset_type}: no dates available")
                continue

            if args.backfill:
                dates = all_dates
            else:
                dates = all_dates[-10:]  # last 10 weekly reports

            print(f"  {asset_type}: {len(dates)} report dates to fetch")
            df = fetch_all(asset_type, dates, fetched_at)
            if not df.empty:
                print(f"  {asset_type}: {len(df):,} rows")
                frames.append(df)
        except Exception as exc:
            print(f"  {asset_type}: ERROR — {exc}")

    if not frames:
        print("  No data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    path = write_partitioned(combined, BASE_DIR, f"fed_soma_{mode}_{today_str}.parquet")
    print(f"  -> {path}  ({len(combined):,} rows)")

    print("\n--- FED SOMA PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
