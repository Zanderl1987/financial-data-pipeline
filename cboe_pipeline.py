#!/usr/bin/env python3
"""
CBOE Volatility Index Pipeline.

Downloads daily OHLC history for CBOE volatility indices:
  VIX   — 30-day implied volatility of S&P 500 options (since 1990)
  VIX9D — 9-day VIX
  VIX3M — 3-month VIX
  VIX6M — 6-month VIX
  VVIX  — volatility of VIX (since 2006)
  SKEW  — tail-risk / skewness index (since 1990)

No API key required.

CLI:
  python cboe_pipeline.py             # last 5 years
  python cboe_pipeline.py --backfill  # full history

Output:
  storage/raw/cboe/year=YYYY/month=MM/cboe_volatility_{mode}_{date}.parquet
"""

import argparse
import datetime
import io
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR   = "storage/raw/cboe"
BASE_URL   = "https://cdn.cboe.com/api/global/us_indices/daily_prices"
REQUEST_GAP = 1.0  # seconds between requests

INDICES = [
    ("VIX",   "VIX_History.csv"),
    ("VIX9D", "VIX9D_History.csv"),
    ("VIX3M", "VIX3M_History.csv"),
    ("VIX6M", "VIX6M_History.csv"),
    ("VVIX",  "VVIX_History.csv"),
    ("SKEW",  "SKEW_History.csv"),
]


def fetch_index(name: str, filename: str) -> pd.DataFrame:
    url = f"{BASE_URL}/{filename}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    text = resp.text
    # Some files have a header row before the column names
    lines = text.splitlines()
    # Find the row that starts with "DATE"
    start = 0
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("DATE"):
            start = i
            break

    csv_text = "\n".join(lines[start:])
    df = pd.read_csv(io.StringIO(csv_text))
    df.columns = [c.strip().upper() for c in df.columns]

    # Rename columns
    col_map = {"DATE": "date", "OPEN": "open", "HIGH": "high", "LOW": "low", "CLOSE": "close"}
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df["index_name"] = name

    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return df[["date", "index_name", "open", "high", "low", "close"]]


def main() -> None:
    parser = argparse.ArgumentParser(description="CBOE volatility indices daily OHLC")
    parser.add_argument("--backfill", action="store_true", help="Full history (always full from CBOE)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"
    cutoff     = None if args.backfill else str(now.year - 5)

    import os
    os.makedirs(BASE_DIR, exist_ok=True)

    print(f"CBOE Volatility Pipeline  mode={mode}\n")
    print("[cboe_volatility]")

    frames = []
    for name, filename in INDICES:
        try:
            df = fetch_index(name, filename)
            if cutoff:
                df = df[df["date"] >= cutoff]
            print(f"  {name}: {len(df):,} rows")
            frames.append(df)
        except Exception as exc:
            print(f"  {name}: ERROR — {exc}")
        time.sleep(REQUEST_GAP)

    if not frames:
        print("  No data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["fetched_at"] = fetched_at
    path = write_partitioned(combined, BASE_DIR, f"cboe_volatility_{mode}_{today_str}.parquet")
    print(f"  -> {path}  ({len(combined):,} rows)")

    print("\n--- CBOE PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
