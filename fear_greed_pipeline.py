#!/usr/bin/env python3
"""
Fear & Greed Index Pipeline.

Fetches the Crypto Fear & Greed Index from Alternative.me (free, no key):
  - Daily composite sentiment score 0–100 (0=Extreme Fear, 100=Extreme Greed)
  - Classification label: Extreme Fear / Fear / Neutral / Greed / Extreme Greed
  - Full history available in a single API call

API: https://api.alternative.me/fng/?limit=0

CLI:
  python fear_greed_pipeline.py             # incremental (last 5 years)
  python fear_greed_pipeline.py --backfill  # full history

Output:
  storage/raw/fear_greed/year=YYYY/month=MM/fear_greed_{mode}_{date}.parquet
"""

import argparse
import datetime
import os

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR = "storage/raw/fear_greed"
API_URL  = "https://api.alternative.me/fng/?limit=0&format=json"


def fetch() -> pd.DataFrame:
    print(f"  GET {API_URL}")
    resp = requests.get(API_URL, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data", [])
    if not data:
        return pd.DataFrame()

    rows = []
    for item in data:
        try:
            ts = int(item.get("timestamp", 0))
            rows.append({
                "date":           datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                "value":          int(item.get("value", 0)),
                "classification": item.get("value_classification", ""),
                "source":         "crypto",
            })
        except Exception:
            continue

    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Fear & Greed Index (Alternative.me)")
    parser.add_argument("--backfill", action="store_true", help="Full history")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"
    cutoff     = None if args.backfill else str(now.year - 5)

    os.makedirs(BASE_DIR, exist_ok=True)

    print(f"Fear & Greed Pipeline  mode={mode}\n")
    print("[fear_greed]")

    try:
        df = fetch()
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return

    if df.empty:
        print("  No data returned")
        return

    if cutoff:
        df = df[df["date"] >= cutoff]

    df["fetched_at"] = fetched_at
    print(f"  {len(df):,} rows  ({df['date'].min()} -> {df['date'].max()})")
    path = write_partitioned(df, BASE_DIR, f"fear_greed_{mode}_{today_str}.parquet")
    print(f"  -> {path}")

    print("\n--- FEAR & GREED PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
