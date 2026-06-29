#!/usr/bin/env python3
"""
Open-Meteo Weather Pipeline -- daily weather history for 25 economically significant locations.

Uses the Open-Meteo Archive API (open-source, no API key required, 10k calls/day free).

Batching strategy: the API accepts multiple lat/lon values in one request and returns a
JSON array. We group the 25 locations into batches of 5, making 5 API calls total instead
of 25. This keeps usage well under the hourly quota regardless of date range.

Covers five economic clusters:
  - Agricultural regions  (temperature, precipitation, frost, growing-degree days)
  - Energy production hubs (wind, solar radiation, heating/cooling degree days)
  - Major retail metros   (population-weighted demand signals)
  - Industrial corridors
  - Port / trade nodes    (storm/freeze disruption signals)

Variables captured (daily):
  temperature_2m_max/min/mean, precipitation_sum, rain_sum, snowfall_sum,
  wind_speed_10m_max, shortwave_radiation_sum, et0_fao_evapotranspiration,
  weather_code, daylight_duration

Outputs:
  storage/raw/open_meteo/year=YYYY/month=MM/open_meteo_{mode}_{YYYYMMDD}.parquet
  CATALOG table: open_meteo_weather

Usage:
  python open_meteo_pipeline.py             # incremental (last 2 years)
  python open_meteo_pipeline.py --backfill  # full history from 1990
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests

from storage_utils import write_partitioned

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OUTPUT_DIR  = os.path.join("storage", "raw", "open_meteo")

BACKFILL_START    = "1990-01-01"
INCREMENTAL_YEARS = 2
BATCH_SIZE        = 5    # locations per API call; keeps individual requests small
BATCH_PAUSE       = 45   # seconds between batches; avoids burst limits
MAX_RETRIES       = 3

DAILY_VARS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
    "rain_sum",
    "snowfall_sum",
    "wind_speed_10m_max",
    "shortwave_radiation_sum",        # proxy for solar generation potential
    "et0_fao_evapotranspiration",     # crop water demand (ag signal)
    "weather_code",                   # WMO code -- storm/clear/fog
    "daylight_duration",              # seconds; seasonal demand signal
]

# 25 locations across five economic clusters
LOCATIONS: list[dict] = [
    # -- Agricultural ----------------------------------------------------------------
    {"name": "Des Moines IA",     "lat": 41.60,  "lon": -93.61,  "cluster": "agriculture"},
    {"name": "Wichita KS",        "lat": 37.69,  "lon": -97.34,  "cluster": "agriculture"},
    {"name": "Fresno CA",         "lat": 36.75,  "lon": -119.77, "cluster": "agriculture"},
    {"name": "Minneapolis MN",    "lat": 44.98,  "lon": -93.27,  "cluster": "agriculture"},
    {"name": "Omaha NE",          "lat": 41.26,  "lon": -95.94,  "cluster": "agriculture"},
    {"name": "Memphis TN",        "lat": 35.15,  "lon": -90.05,  "cluster": "agriculture"},
    # -- Energy ----------------------------------------------------------------------
    {"name": "Houston TX",        "lat": 29.76,  "lon": -95.37,  "cluster": "energy"},
    {"name": "Midland TX",        "lat": 31.99,  "lon": -102.08, "cluster": "energy"},
    {"name": "Williston ND",      "lat": 48.15,  "lon": -103.62, "cluster": "energy"},
    {"name": "Pittsburgh PA",     "lat": 40.44,  "lon": -79.99,  "cluster": "energy"},
    {"name": "Great Falls MT",    "lat": 47.50,  "lon": -111.30, "cluster": "energy"},
    # -- Retail / Population ---------------------------------------------------------
    {"name": "New York NY",       "lat": 40.71,  "lon": -74.01,  "cluster": "retail"},
    {"name": "Los Angeles CA",    "lat": 34.05,  "lon": -118.24, "cluster": "retail"},
    {"name": "Chicago IL",        "lat": 41.88,  "lon": -87.63,  "cluster": "retail"},
    {"name": "Atlanta GA",        "lat": 33.75,  "lon": -84.39,  "cluster": "retail"},
    {"name": "Dallas TX",         "lat": 32.78,  "lon": -96.80,  "cluster": "retail"},
    {"name": "Phoenix AZ",        "lat": 33.45,  "lon": -112.07, "cluster": "retail"},
    # -- Industrial ------------------------------------------------------------------
    {"name": "Detroit MI",        "lat": 42.33,  "lon": -83.05,  "cluster": "industrial"},
    {"name": "Louisville KY",     "lat": 38.25,  "lon": -85.76,  "cluster": "industrial"},
    {"name": "Columbus OH",       "lat": 39.96,  "lon": -82.99,  "cluster": "industrial"},
    # -- Ports / Trade ---------------------------------------------------------------
    {"name": "Los Angeles Port",  "lat": 33.74,  "lon": -118.27, "cluster": "port"},
    {"name": "New Orleans LA",    "lat": 29.95,  "lon": -90.07,  "cluster": "port"},
    {"name": "Seattle WA",        "lat": 47.61,  "lon": -122.33, "cluster": "port"},
    {"name": "Miami FL",          "lat": 25.77,  "lon": -80.19,  "cluster": "port"},
    {"name": "Baltimore MD",      "lat": 39.29,  "lon": -76.61,  "cluster": "port"},
]


def _fetch_batch(batch: list[dict], start_date: str, end_date: str) -> list[dict] | None:
    """
    Fetch one batch of locations in a single API call.

    The archive API accepts comma-separated lat/lon and returns a JSON array
    (one element per location) when multiple coordinates are supplied. This
    keeps total API calls to ceil(25/BATCH_SIZE) = 5 instead of 25.
    """
    params = {
        "latitude":   ",".join(str(loc["lat"]) for loc in batch),
        "longitude":  ",".join(str(loc["lon"]) for loc in batch),
        "start_date": start_date,
        "end_date":   end_date,
        "daily":      ",".join(DAILY_VARS),
        "timezone":   "UTC",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(ARCHIVE_URL, params=params, timeout=120)
            if r.status_code == 200:
                data = r.json()
                # Single location returns a dict; multiple returns a list
                return data if isinstance(data, list) else [data]
            if r.status_code == 429:
                wait = 60 * attempt
                print(f"    429 rate limit -- waiting {wait}s (attempt {attempt})")
                time.sleep(wait)
            else:
                print(f"    HTTP {r.status_code}: {r.text[:200]}")
                return None
        except requests.RequestException as exc:
            print(f"    Request error (attempt {attempt}): {exc}")
            time.sleep(30 * attempt)

    return None


def _parse_location_data(loc_data: dict, loc_meta: dict) -> pd.DataFrame:
    """Convert one element of the API response array into a tidy DataFrame."""
    daily = loc_data.get("daily", {})
    dates = daily.get("time", [])
    if not dates:
        return pd.DataFrame()

    rows = []
    for i, d in enumerate(dates):
        row = {
            "location":  loc_meta["name"],
            "cluster":   loc_meta["cluster"],
            "latitude":  loc_meta["lat"],
            "longitude": loc_meta["lon"],
            "date":      d,
        }
        for var in DAILY_VARS:
            vals = daily.get(var, [])
            row[var] = vals[i] if i < len(vals) else None
        rows.append(row)

    return pd.DataFrame(rows)


def main(backfill: bool = False) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    now       = datetime.datetime.utcnow()
    today     = now.strftime("%Y%m%d")
    end_date  = now.strftime("%Y-%m-%d")
    mode      = "backfill" if backfill else "incremental"

    if backfill:
        start_date = BACKFILL_START
    else:
        start_date = (now - datetime.timedelta(days=365 * INCREMENTAL_YEARS)).strftime("%Y-%m-%d")

    batches = [LOCATIONS[i:i + BATCH_SIZE] for i in range(0, len(LOCATIONS), BATCH_SIZE)]

    print(f"Open-Meteo Weather Pipeline  mode={mode}")
    print(f"Date range: {start_date} -> {end_date}")
    print(f"Locations:  {len(LOCATIONS)} in {len(batches)} batches of {BATCH_SIZE}")
    print(f"Variables:  {len(DAILY_VARS)}")
    print()

    frames = []
    for b_idx, batch in enumerate(batches):
        names = ", ".join(loc["name"] for loc in batch)
        print(f"  batch {b_idx + 1}/{len(batches)}: {names}")
        results = _fetch_batch(batch, start_date, end_date)

        if results is None:
            print(f"    no data (all {len(batch)} locations)")
        else:
            for loc_meta, loc_data in zip(batch, results):
                df_loc = _parse_location_data(loc_data, loc_meta)
                if not df_loc.empty:
                    frames.append(df_loc)
                    print(f"    {loc_meta['name']}: {len(df_loc):,} rows")
                else:
                    print(f"    {loc_meta['name']}: no data")

        if b_idx < len(batches) - 1:
            print(f"    (pausing {BATCH_PAUSE}s before next batch)")
            time.sleep(BATCH_PAUSE)

    if not frames:
        print("\nNo data returned.")
        return

    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["fetched_at"] = now.isoformat()

    path = write_partitioned(df, OUTPUT_DIR, f"open_meteo_{mode}_{today}.parquet")

    date_min = df["date"].min().strftime("%Y-%m-%d")
    date_max = df["date"].max().strftime("%Y-%m-%d")
    print(f"\n[+] {path}")
    print(f"    {len(df):,} rows | {df['location'].nunique()} locations | {date_min} to {date_max}")
    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Open-Meteo daily weather pipeline for 25 US economic locations (keyless)"
    )
    parser.add_argument("--backfill", action="store_true",
                        help=f"Full history from {BACKFILL_START}. Default: last {INCREMENTAL_YEARS} years.")
    args = parser.parse_args()
    main(backfill=args.backfill)
