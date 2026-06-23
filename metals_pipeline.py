#!/usr/bin/env python3
"""
Metals Spot Price Pipeline.

Two data sources:
  1. api.metals.live — real-time precious metals spot prices (no key required)
  2. FRED API — monthly base metals historical prices (IMF PCPS series)

Precious metals from api.metals.live are stored as of today's date.
Base metals history from FRED goes back to the 1990s–2000s depending on series.

CLI:
  python metals_pipeline.py             # incremental (last 90 days)
  python metals_pipeline.py --backfill  # full FRED history + today's spot

Output:
  storage/raw/metals/metals_spot_{mode}_{YYYYMMDD}.parquet
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

FRED_API_KEY  = os.environ.get("FRED_API_KEY", "")
FRED_BASE     = "https://api.stlouisfed.org/fred/series/observations"
METALS_LIVE   = "https://api.metals.live/v1/spot"
BASE_DIR      = os.path.join("storage", "raw", "metals")
REQUEST_INTERVAL = 0.5
MAX_RETRIES      = 3
BACKOFF_SECONDS  = 60

# Base metals via FRED (IMF PCPS, monthly)
# Precious metals (gold, silver, platinum, palladium) already in commodity_macro_pipeline
FRED_METALS: dict[str, tuple] = {
    "PCOPPUSDM":        ("Copper",               "monthly", "USD/MT"),
    "PALUMUSDM":        ("Aluminum",             "monthly", "USD/MT"),
    "PNICKUSDM":        ("Nickel",               "monthly", "USD/MT"),
    "PZINCUSDM":        ("Zinc",                 "monthly", "USD/MT"),
    "PLEADUSDM":        ("Lead",                 "monthly", "USD/MT"),
    "PIORECRUSDM":      ("Iron Ore",             "monthly", "USD/DMT"),
    "PTINUSDM":         ("Tin",                  "monthly", "USD/MT"),
}

# Metals returned by api.metals.live and their display names
METALS_LIVE_MAP = {
    "gold":      "Gold",
    "silver":    "Silver",
    "platinum":  "Platinum",
    "palladium": "Palladium",
    "lbma_gold": "Gold LBMA",
}


def get_with_backoff(url, params):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 rate limit. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code}: {r.text[:120]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    return None


def fetch_fred_series(series_id, observation_start=None):
    params = {
        "series_id":  series_id,
        "api_key":    FRED_API_KEY,
        "file_type":  "json",
        "sort_order": "asc",
        "limit":      100000,
    }
    if observation_start:
        params["observation_start"] = observation_start

    r = get_with_backoff(FRED_BASE, params)
    if not r:
        return None

    observations = r.json().get("observations", [])
    if not observations:
        return None

    df = pd.DataFrame(observations)[["date", "value"]]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    df["date"] = pd.to_datetime(df["date"])
    return df if not df.empty else None


def fetch_metals_live(today_str):
    """Fetch real-time spot from api.metals.live. Returns a DataFrame or None."""
    try:
        r = requests.get(METALS_LIVE, timeout=15)
        if r.status_code != 200:
            print(f"  api.metals.live HTTP {r.status_code}")
            return None
        data = r.json()
        # Response may be a list of dicts or a single dict
        if isinstance(data, list):
            data = data[0] if data else {}
        rows = []
        for key, display_name in METALS_LIVE_MAP.items():
            if key in data:
                rows.append({
                    "series_id": f"metals_live_{key}",
                    "name":      display_name,
                    "frequency": "realtime",
                    "unit":      "USD/troy oz",
                    "source":    "api.metals.live",
                    "date":      pd.Timestamp(today_str),
                    "value":     float(data[key]),
                })
        # Also handle flat numeric keys like "XAU", "XAG"
        xau_map = {"XAU": "Gold", "XAG": "Silver", "XPT": "Platinum", "XPD": "Palladium"}
        for xcode, display_name in xau_map.items():
            if xcode in data and xcode not in [r["series_id"] for r in rows]:
                rows.append({
                    "series_id": f"metals_live_{xcode.lower()}",
                    "name":      display_name,
                    "frequency": "realtime",
                    "unit":      "USD/troy oz",
                    "source":    "api.metals.live",
                    "date":      pd.Timestamp(today_str),
                    "value":     float(data[xcode]),
                })
        return pd.DataFrame(rows) if rows else None
    except Exception as e:
        print(f"  api.metals.live error: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Metals spot price pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full FRED history")
    args = parser.parse_args()

    if not FRED_API_KEY:
        print("ERROR: FRED_API_KEY not set in .env")
        return

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"
    observation_start = None if args.backfill else (
        (now - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    )

    print(f"Metals Pipeline  mode={mode}\n")
    os.makedirs(BASE_DIR, exist_ok=True)
    frames = []

    # Part 1: real-time spot from api.metals.live
    print("[metals_live] Fetching real-time spot prices...")
    spot_df = fetch_metals_live(today_str)
    if spot_df is not None and not spot_df.empty:
        spot_df["fetched_at"] = now.isoformat()
        frames.append(spot_df)
        print(f"  {len(spot_df)} metals fetched from api.metals.live")
    else:
        print("  No data from api.metals.live (may be down or format changed)")

    # Part 2: FRED historical base metals
    print(f"\n[fred_metals] Fetching {len(FRED_METALS)} series from FRED...")
    failed = []
    for i, (series_id, (name, frequency, unit)) in enumerate(FRED_METALS.items(), 1):
        print(f"  [{i}/{len(FRED_METALS)}] {series_id} — {name}...", end=" ")
        df = fetch_fred_series(series_id, observation_start)
        if df is None or df.empty:
            print("no data")
            failed.append(series_id)
            time.sleep(REQUEST_INTERVAL)
            continue

        df["series_id"] = series_id
        df["name"]      = name
        df["frequency"] = frequency
        df["unit"]      = unit
        df["source"]    = "FRED/IMF"
        df["fetched_at"] = now.isoformat()
        frames.append(df)
        print(f"{len(df):,} rows")
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        print("\nNo data fetched.")
        return

    combined = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["series_id", "date"])
        .sort_values(["series_id", "date"])
        .reset_index(drop=True)
    )

    path = write_partitioned(
        combined, BASE_DIR,
        f"metals_spot_{mode}_{today_str}.parquet",
    )
    print(f"\n-> {path}")
    print(f"   {len(combined):,} rows | {combined['series_id'].nunique()} series")
    if failed:
        print(f"   No data: {', '.join(failed)}")

    print("\n--- METALS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
