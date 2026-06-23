#!/usr/bin/env python3
"""
IMF Primary Commodity Prices (PCPS) Pipeline.

Fetches commodity price series from the IMF PCPS dataset, accessed via the
St. Louis Fed FRED API (which mirrors IMF PCPS data with stable series IDs).

Covers base metals, coal, LNG, silver, fertilizers, and agricultural
commodities not already in commodity_macro_pipeline.py.

Requires: FRED_API_KEY in .env

CLI:
  python imf_commodities_pipeline.py             # incremental (last 90 days)
  python imf_commodities_pipeline.py --backfill  # full history

Output:
  storage/raw/imf/imf_commodities_{mode}_{YYYYMMDD}.parquet
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

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE    = "https://api.stlouisfed.org/fred/series/observations"
BASE_DIR     = os.path.join("storage", "raw", "imf")
REQUEST_INTERVAL = 0.5
MAX_RETRIES      = 3
BACKOFF_SECONDS  = 60

# ---------------------------------------------------------------------------
# IMF PCPS series available via FRED — supplements commodity_macro_pipeline
# (gold, copper, WTI, Brent, natural gas, corn, wheat, soybeans, cotton,
#  sugar, coffee, platinum, palladium already live in commodity_macro)
# ---------------------------------------------------------------------------

SERIES: dict[str, tuple] = {
    # Energy — coal and LNG (not in commodity_macro)
    "PCOALAUUSDM":    ("Coal Australian",              "monthly", "USD/MT",          "energy"),
    "PNGASJPUSDM":    ("Natural Gas Japan LNG",        "monthly", "USD/MMBtu",       "energy"),
    "PNGASEUUSDM":    ("Natural Gas European",         "monthly", "USD/MMBtu",       "energy"),

    # Base metals (not in commodity_macro: no aluminum, nickel, zinc, lead, iron ore, tin)
    "PALUMUSDM":      ("Aluminum",                     "monthly", "USD/MT",          "metals"),
    "PNICKUSDM":      ("Nickel",                       "monthly", "USD/MT",          "metals"),
    "PZINCUSDM":      ("Zinc",                         "monthly", "USD/MT",          "metals"),
    "PLEADUSDM":      ("Lead",                         "monthly", "USD/MT",          "metals"),
    "PIORECRUSDM":    ("Iron Ore",                     "monthly", "USD/DMT",         "metals"),
    "PTINUSDM":       ("Tin",                          "monthly", "USD/MT",          "metals"),
    # Silver already in commodity_macro_pipeline; SLVPRUSD ID returns 400 on FRED

    # Agricultural (rice, palm oil, tea not in commodity_macro)
    "PRICENPQUSDM":   ("Rice Thailand",                "monthly", "USD/MT",          "agriculture"),
    "PPOILUSDM":      ("Palm Oil",                     "monthly", "USD/MT",          "agriculture"),
    "PTEAUSDM":       ("Tea",                          "monthly", "USD/kg",          "agriculture"),
    # Cocoa and rubber series codes differ across FRED — covered by WB Pink Sheet
    # Fertilizers (urea, DAP, potash) not available in FRED's IMF PCPS mirror —
    # covered by World Bank Pink Sheet pipeline (worldbank_pink_sheet.py)

    # Commodity indices
    "PALLFNFINDEXM":  ("Non-Fuel Commodity Index",     "monthly", "Index 2016=100",  "indices"),
    "PFOODINDEXM":    ("Food Commodity Index",         "monthly", "Index 2016=100",  "indices"),
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
                print(f"  HTTP {r.status_code} for {params.get('series_id')}: {r.text[:120]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts: {params.get('series_id')}")
    return None


def fetch_series(series_id, observation_start=None):
    params = {
        "series_id":   series_id,
        "api_key":     FRED_API_KEY,
        "file_type":   "json",
        "sort_order":  "asc",
        "limit":       100000,
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


def main():
    parser = argparse.ArgumentParser(description="IMF Primary Commodity Prices pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full history (default: last 90 days)")
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

    print(f"IMF Commodities Pipeline  mode={mode}")
    if observation_start:
        print(f"  Fetching from {observation_start}\n")

    os.makedirs(BASE_DIR, exist_ok=True)
    frames = []
    failed = []

    total = len(SERIES)
    for i, (series_id, (name, frequency, unit, category)) in enumerate(SERIES.items(), 1):
        print(f"  [{i}/{total}] {series_id} — {name}...", end=" ")
        df = fetch_series(series_id, observation_start)
        if df is None or df.empty:
            print("no data")
            failed.append(series_id)
            time.sleep(REQUEST_INTERVAL)
            continue

        df["series_id"] = series_id
        df["name"]      = name
        df["frequency"] = frequency
        df["unit"]      = unit
        df["category"]  = category
        df["fetched_at"] = now.isoformat()
        frames.append(df)
        print(f"{len(df):,} rows")
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        print("\nNo data fetched. Check FRED_API_KEY and series IDs.")
        return

    combined = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["series_id", "date"])
        .sort_values(["category", "series_id", "date"])
        .reset_index(drop=True)
    )

    path = write_partitioned(
        combined, BASE_DIR,
        f"imf_commodities_{mode}_{today_str}.parquet",
    )
    print(f"\n-> {path}")
    print(f"   {len(combined):,} rows | {combined['series_id'].nunique()} series | "
          f"{combined['category'].nunique()} categories")
    if failed:
        print(f"   Series with no data: {', '.join(failed)}")

    print("\n--- IMF COMMODITIES PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
