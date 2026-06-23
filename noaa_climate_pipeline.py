"""
NOAA NCEI Climate Pipeline — Monthly weather summaries for US agricultural regions.

Uses the keyless NCEI Access Services API v1 (Global Summary of the Month dataset).
No API key required.

Outputs:
  storage/raw/climate/year=YYYY/month=MM/noaa_climate_{mode}_{YYYYMMDD}.parquet
  CATALOG table: noaa_climate

Usage:
  python noaa_climate_pipeline.py             # incremental (last 2 years)
  python noaa_climate_pipeline.py --backfill  # full history from 1990
"""

import argparse
import datetime
import io
import os
import time

import pandas as pd
import requests

from storage_utils import write_partitioned

NCEI_BASE = "https://www.ncei.noaa.gov/access/services/data/v1"
CLIMATE_DIR = os.path.join("storage", "raw", "climate")

REQUEST_INTERVAL = 0.25   # stay well under the 5 req/sec limit
MAX_RETRIES = 3
BACKOFF_SECONDS = 30
BACKFILL_START_YEAR = 1990
INCREMENTAL_YEARS = 2

DATASET = "global-summary-of-the-month"

# Monthly data types — temperature in Celsius, precipitation in mm
DATA_TYPES = [
    "TMAX",   # Monthly maximum temperature (C)
    "TMIN",   # Monthly minimum temperature (C)
    "TAVG",   # Monthly average temperature (C)
    "PRCP",   # Total monthly precipitation (mm)
    "SNOW",   # Total monthly snowfall (mm)
    "HDD",    # Heating degree days (base 65F)
    "CDD",    # Cooling degree days (base 65F)
    "DP01",   # Days with >= 0.01 inch precipitation
    "DP10",   # Days with >= 0.10 inch precipitation
    "EMXT",   # Extreme maximum temperature for month (C)
    "EMNT",   # Extreme minimum temperature for month (C)
]

# 15 stations covering major US agricultural regions
STATIONS: dict[str, str] = {
    "USW00014933": "Des Moines IA (Corn Belt)",
    "USW00003928": "Wichita KS (Winter Wheat)",
    "USW00093193": "Fresno CA (Central Valley)",
    "USW00014922": "Minneapolis MN (Spring Wheat)",
    "USW00013874": "Atlanta GA (Southeast Ag)",
    "USW00023183": "Phoenix AZ (Cotton/Citrus)",
    "USW00094846": "Chicago IL (Midwest Hub)",
    "USW00003927": "Dallas TX (Cotton/Cattle)",
    "USW00012916": "New Orleans LA (Export Hub)",
    "USW00013748": "Memphis TN (Cotton/Soybeans)",
    "USW00094728": "New York NY (Northeast)",
    "USW00023234": "Los Angeles CA (Pacific Coast)",
    "USW00024155": "Great Falls MT (Northern Plains)",
    "USW00014942": "Omaha NE (Corn/Soybeans)",
    "USW00013957": "Jackson MS (Delta Ag)",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_with_backoff(params: dict) -> requests.Response | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(NCEI_BASE, params=params, timeout=60)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 rate limit — backing off {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
            elif r.status_code == 503:
                wait = BACKOFF_SECONDS * attempt
                print(f"  503 service unavailable — backing off {wait}s")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code}: {r.text[:300]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts.")
    return None


# ---------------------------------------------------------------------------
# Fetch one year of monthly climate data for all stations
# ---------------------------------------------------------------------------

def fetch_year(year: int) -> pd.DataFrame | None:
    start = f"{year}-01-01"
    end_year = min(year, datetime.datetime.utcnow().year)
    end_month = datetime.datetime.utcnow().month if end_year == year else 12
    end = f"{end_year}-{end_month:02d}-01"

    station_ids = list(STATIONS.keys())

    params = {
        "dataset": DATASET,
        "stations": ",".join(station_ids),
        "startDate": start,
        "endDate": end,
        "dataTypes": ",".join(DATA_TYPES),
        "units": "metric",
        "format": "csv",
        "includeAttributes": "false",
    }

    print(f"  Fetching {year}...", end=" ", flush=True)
    r = _get_with_backoff(params)
    if r is None:
        return None

    if not r.text.strip() or r.text.strip().startswith("No data"):
        print("no data")
        return None

    try:
        df = pd.read_csv(io.StringIO(r.text), low_memory=False)
    except Exception as e:
        print(f"parse error: {e}")
        return None

    if df.empty:
        print("empty")
        return None

    print(f"{len(df)} rows")
    return df


# ---------------------------------------------------------------------------
# Data cleaning
# ---------------------------------------------------------------------------

def clean(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.lower().strip() for c in df.columns]

    # Rename NCEI standard columns
    rename = {
        "station": "station_id",
        "date":    "date",
        "name":    "station_name",   # present in some response formats
    }
    df = df.rename(columns=rename)

    # Attach human-readable label from STATIONS dict (API rarely returns NAME)
    if "station_id" in df.columns:
        df["station_name"] = df.get("station_name", pd.Series(dtype=str))
        df["station_name"] = df["station_name"].fillna(df["station_id"].map(STATIONS))
        df["region"] = df["station_id"].map(STATIONS)

    # Parse date — GSOM returns YYYY-MM
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], format="%Y-%m", errors="coerce")

    # Coerce all weather measure columns to numeric; drop attribute columns
    attr_cols = [c for c in df.columns if c.endswith("_attributes")]
    df = df.drop(columns=attr_cols, errors="ignore")

    measure_cols = [c.lower() for c in DATA_TYPES]
    for col in measure_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop rows where date could not be parsed
    df = df.dropna(subset=["date"])

    # Standardise column order
    front = ["station_id", "station_name", "region", "date"]
    measures = [c for c in measure_cols if c in df.columns]
    geo = [c for c in ["latitude", "longitude", "elevation"] if c in df.columns]
    rest = [c for c in df.columns if c not in front + measures + geo]
    df = df[front + geo + measures + rest]

    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df.sort_values(["station_id", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill: bool = False) -> None:
    os.makedirs(CLIMATE_DIR, exist_ok=True)

    now = datetime.datetime.utcnow()
    today = now.strftime("%Y%m%d")

    if backfill:
        years = list(range(BACKFILL_START_YEAR, now.year + 1))
        mode_tag = "backfill"
        print(f"Mode: BACKFILL ({BACKFILL_START_YEAR}–{now.year}, {len(years)} years)")
    else:
        start_year = now.year - INCREMENTAL_YEARS
        years = list(range(start_year, now.year + 1))
        mode_tag = "incremental"
        print(f"Mode: INCREMENTAL ({start_year}–{now.year})")

    print(f"Stations: {len(STATIONS)}")
    print(f"Dataset:  {DATASET}")
    print(f"Measures: {', '.join(DATA_TYPES)}")
    print()

    frames = []
    for year in years:
        df_year = fetch_year(year)
        if df_year is not None:
            frames.append(df_year)
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        print("\nNo data returned. Check NCEI API availability.")
        return

    df = pd.concat(frames, ignore_index=True)
    df = clean(df)

    path = write_partitioned(df, CLIMATE_DIR, f"noaa_climate_{mode_tag}_{today}.parquet")

    stations_found = df["station_id"].nunique() if "station_id" in df.columns else "?"
    date_min = df["date"].min().strftime("%Y-%m") if "date" in df.columns else "?"
    date_max = df["date"].max().strftime("%Y-%m") if "date" in df.columns else "?"

    print(f"\n[+] {path}")
    print(f"    {len(df):,} rows | {stations_found} stations | {date_min} to {date_max}")
    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NOAA NCEI climate pipeline — monthly summaries for US agricultural regions (keyless)"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=f"Fetch full history from {BACKFILL_START_YEAR}. Default: last {INCREMENTAL_YEARS} years.",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
