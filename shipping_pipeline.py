#!/usr/bin/env python3
"""
Shipping / Logistics Pipeline — NY Fed GSCPI + FRED freight PPI series.

Two keyless-ish sources tracking global shipping/supply-chain pressure:

  GSCPI (NY Fed Global Supply Chain Pressure Index) — single composite
  monthly index (z-score) built from shipping cost + PMI delivery-time
  components across 7 economies. Keyless Excel download, back to 1998.
    https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx

  FRED freight PPI series (uses existing FRED_API_KEY) — deep-sea freight
  transportation and marine cargo handling producer price indexes, plus
  the BTS Freight Transportation Services Index and rail/truck/air freight
  PPI and diesel fuel PPI for broader multi-modal coverage. These substitute
  for the Baltic Dry Index / Freightos FBX, which require paid licenses or
  ToS-restricted attribution for time-series use.

CLI:
  python shipping_pipeline.py             # incremental (last 90 days)
  python shipping_pipeline.py --backfill  # full available history

Output:
  storage/raw/shipping/gscpi/shipping_gscpi_{mode}_{YYYYMMDD}.parquet
  storage/raw/shipping/freight_ppi/shipping_freight_ppi_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import io
import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv

from storage_utils import write_partitioned

load_dotenv()

FRED_API_KEY = os.environ.get("FRED_API_KEY")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

GSCPI_DIR       = os.path.join("storage", "raw", "shipping", "gscpi")
FREIGHT_PPI_DIR = os.path.join("storage", "raw", "shipping", "freight_ppi")

GSCPI_URL = ("https://www.newyorkfed.org/medialibrary/research/interactives/"
             "gscpi/downloads/gscpi_data.xlsx")

REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3
BACKOFF_SECONDS = 60

# FRED series substituting for Baltic Dry Index / Freightos (paid/ToS-restricted)
FREIGHT_SERIES = {
    "PCU483111483111": ("Deep Sea Freight Transportation PPI", "monthly", "Index"),
    "WPU301301":        ("Deep Sea Water Transportation of Freight PPI", "monthly", "Index"),
    "WPU3113":           ("Marine Cargo Handling PPI", "monthly", "Index"),
    # Broader modal freight coverage (rail/truck/air) + composite freight volume index
    "TSIFRGHT":          ("BTS Freight Transportation Services Index", "monthly", "Index"),
    "WPU3011":           ("Rail Transportation of Freight and Mail PPI", "monthly", "Index"),
    "WPU3012":           ("Truck Transportation of Freight PPI", "monthly", "Index"),
    "WPU3014":           ("Air Transportation of Freight PPI", "monthly", "Index"),
    "WPU057303":         ("No. 2 Diesel Fuel PPI", "monthly", "Index"),
}


# ---------------------------------------------------------------------------
# GSCPI
# ---------------------------------------------------------------------------

def fetch_gscpi(now: datetime.datetime) -> pd.DataFrame | None:
    print("[gscpi] Downloading NY Fed GSCPI...")
    headers = {"User-Agent": "Mozilla/5.0 financial-data-pipeline/1.0"}
    try:
        r = requests.get(GSCPI_URL, headers=headers, timeout=60)
        if r.status_code != 200 or len(r.content) < 1000:
            print(f"  HTTP {r.status_code} or short response.")
            return None
    except requests.RequestException as e:
        print(f"  Error: {e}")
        return None

    try:
        xl = pd.ExcelFile(io.BytesIO(r.content))
        sheet_name = next((n for n in xl.sheet_names if "monthly" in n.lower()), xl.sheet_names[-1])
        df = xl.parse(sheet_name)
        df = df[["Date", "GSCPI"]].dropna()
        df = df.rename(columns={"Date": "date", "GSCPI": "gscpi"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["gscpi"] = pd.to_numeric(df["gscpi"], errors="coerce")
        df = df.dropna(subset=["date", "gscpi"])
        df["source"] = "NY Fed GSCPI"
        df["fetched_at"] = now.isoformat()
        print(f"  Parsed {len(df):,} rows, {df['date'].min().date()} to {df['date'].max().date()}")
        return df
    except Exception as e:
        print(f"  Excel parse error: {e}")
        return None


# ---------------------------------------------------------------------------
# FRED freight series
# ---------------------------------------------------------------------------

def _get_with_backoff(url, params):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from FRED. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code} for {params.get('series_id')}: {r.text[:120]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    return None


def fetch_fred_series(series_id: str, observation_start: str | None) -> pd.DataFrame | None:
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "asc",
        "limit": 100000,
    }
    if observation_start:
        params["observation_start"] = observation_start

    r = _get_with_backoff(FRED_BASE, params)
    if not r:
        return None
    observations = r.json().get("observations", [])
    if not observations:
        return None
    df = pd.DataFrame(observations)[["date", "value"]]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def fetch_freight_ppi(now: datetime.datetime, backfill: bool) -> pd.DataFrame | None:
    if not FRED_API_KEY:
        print("[freight_ppi] FRED_API_KEY not set — skipping.")
        return None

    observation_start = None if backfill else (now - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    print(f"[freight_ppi] Fetching {len(FREIGHT_SERIES)} FRED series "
          f"({'full history' if backfill else f'from {observation_start}'})...")

    frames = []
    for series_id, (name, frequency, unit) in FREIGHT_SERIES.items():
        df = fetch_fred_series(series_id, observation_start)
        if df is None or df.empty:
            print(f"  {series_id}: no data returned.")
            time.sleep(REQUEST_INTERVAL)
            continue
        df["series_id"] = series_id
        df["name"] = name
        df["frequency"] = frequency
        df["unit"] = unit
        df["fetched_at"] = now.isoformat()
        frames.append(df)
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    print(f"  Parsed {len(out):,} rows across {out['series_id'].nunique()} series.")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="NY Fed GSCPI + FRED freight PPI shipping pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full available history for FRED series (GSCPI is always full history).")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"

    os.makedirs(GSCPI_DIR, exist_ok=True)
    os.makedirs(FREIGHT_PPI_DIR, exist_ok=True)

    gscpi_df = fetch_gscpi(now)
    if gscpi_df is not None and not gscpi_df.empty:
        path = write_partitioned(gscpi_df, GSCPI_DIR, f"shipping_gscpi_{mode}_{today_str}.parquet")
        print(f"  -> {path}\n")

    freight_df = fetch_freight_ppi(now, args.backfill)
    if freight_df is not None and not freight_df.empty:
        path = write_partitioned(freight_df, FREIGHT_PPI_DIR, f"shipping_freight_ppi_{mode}_{today_str}.parquet")
        print(f"  -> {path}")

    print("\n--- SHIPPING PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
