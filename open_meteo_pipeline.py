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

BACKFILL_START     = "1990-01-01"
INCREMENTAL_YEARS  = 2
BATCH_SIZE         = 5    # locations per API call; keeps individual requests small
BATCH_PAUSE        = 45   # seconds between batches; avoids burst limits (recurring incremental runs)
BACKFILL_BATCH_PAUSE = 30 # shorter pause for the one-time backfill than the recurring 45s, but
                          # NOT as short as 10s -- confirmed live 2026-08-03 that 10s escalates
                          # into a sustained run of 429s that even a 180s in-batch backoff can't
                          # clear (Open-Meteo's keyless tier tracks a rolling window, not just a
                          # per-request burst limit). 30s traded some of the speed win for staying
                          # under that sustained threshold.
MAX_RETRIES       = 3
CHUNK_YEARS       = 3    # backfill date-range chunk size; 35yr x 5 locations x 11 vars
                          # in one call is too much for the archive API (times out /
                          # returns truncated JSON) -- chunk backfill requests by date too

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


def _date_chunks(start_date: str, end_date: str, years_per_chunk: int = CHUNK_YEARS) -> list[tuple[str, str]]:
    """Split [start_date, end_date] into consecutive ~years_per_chunk-year windows."""
    start = datetime.date.fromisoformat(start_date)
    end   = datetime.date.fromisoformat(end_date)

    chunks = []
    chunk_start = start
    while chunk_start <= end:
        try:
            chunk_end = chunk_start.replace(year=chunk_start.year + years_per_chunk) - datetime.timedelta(days=1)
        except ValueError:
            # Feb 29 landing on a non-leap year
            chunk_end = chunk_start.replace(year=chunk_start.year + years_per_chunk, day=28) - datetime.timedelta(days=1)
        chunk_end = min(chunk_end, end)
        chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
        chunk_start = chunk_end + datetime.timedelta(days=1)

    return chunks


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
    date_ranges = _date_chunks(start_date, end_date) if backfill else [(start_date, end_date)]
    batch_pause = BACKFILL_BATCH_PAUSE if backfill else BATCH_PAUSE

    print(f"Open-Meteo Weather Pipeline  mode={mode}", flush=True)
    print(f"Date range: {start_date} -> {end_date}"
          + (f" ({len(date_ranges)} chunks of ~{CHUNK_YEARS}yr)" if len(date_ranges) > 1 else ""), flush=True)
    print(f"Locations:  {len(LOCATIONS)} in {len(batches)} batches of {BATCH_SIZE}", flush=True)
    print(f"Variables:  {len(DAILY_VARS)}", flush=True)
    print(flush=True)

    total_requests = len(date_ranges) * len(batches)
    req_idx = 0
    total_rows = 0
    written_paths = []
    skipped_chunks = 0
    for d_idx, (chunk_start, chunk_end) in enumerate(date_ranges):
        if len(date_ranges) > 1:
            print(f"date chunk {d_idx + 1}/{len(date_ranges)}: {chunk_start} -> {chunk_end}", flush=True)

        # Resume support: a killed/interrupted backfill shouldn't have to
        # re-fetch chunks it already checkpointed successfully. Skip (and
        # don't burn the pause) if this chunk's file is already on disk.
        suffix = f"{chunk_start}_{chunk_end}" if len(date_ranges) > 1 else today
        expected_path = os.path.join(
            OUTPUT_DIR, f"year={now.year}", f"month={now.month:02d}", f"open_meteo_{mode}_{suffix}.parquet"
        )
        if os.path.exists(expected_path):
            print(f"  already written -> {expected_path} (skipping)", flush=True)
            req_idx += len(batches)
            skipped_chunks += 1
            continue

        chunk_frames = []
        for b_idx, batch in enumerate(batches):
            req_idx += 1
            names = ", ".join(loc["name"] for loc in batch)
            print(f"  batch {b_idx + 1}/{len(batches)}: {names}", flush=True)
            results = _fetch_batch(batch, chunk_start, chunk_end)

            if results is None:
                print(f"    no data (all {len(batch)} locations)", flush=True)
            else:
                for loc_meta, loc_data in zip(batch, results):
                    df_loc = _parse_location_data(loc_data, loc_meta)
                    if not df_loc.empty:
                        chunk_frames.append(df_loc)
                        print(f"    {loc_meta['name']}: {len(df_loc):,} rows", flush=True)
                    else:
                        print(f"    {loc_meta['name']}: no data", flush=True)

            if req_idx < total_requests:
                print(f"    (pausing {batch_pause}s before next batch)", flush=True)
                time.sleep(batch_pause)

        # Write each date chunk as soon as it's done, not just once at the very
        # end -- a backfill runs many minutes across dozens of requests, and a
        # single end-of-run write means an interrupted run loses everything
        # fetched so far (confirmed 2026-08-03: a killed background run produced
        # zero output despite running 90+ minutes).
        if chunk_frames:
            chunk_df = pd.concat(chunk_frames, ignore_index=True)
            chunk_df["date"] = pd.to_datetime(chunk_df["date"], errors="coerce")
            chunk_df["fetched_at"] = now.isoformat()
            path = write_partitioned(chunk_df, OUTPUT_DIR, f"open_meteo_{mode}_{suffix}.parquet")
            written_paths.append(path)
            total_rows += len(chunk_df)
            print(f"    [+] wrote {len(chunk_df):,} rows -> {path}", flush=True)

    if not written_paths and not skipped_chunks:
        print("\nNo data returned.")
        return

    print(f"\n{total_rows:,} total rows written across {len(written_paths)} file(s) this run"
          + (f" ({skipped_chunks} chunk(s) already done, skipped)" if skipped_chunks else ""))
    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Open-Meteo daily weather pipeline for 25 US economic locations (keyless)"
    )
    parser.add_argument("--backfill", action="store_true",
                        help=f"Full history from {BACKFILL_START}. Default: last {INCREMENTAL_YEARS} years.")
    args = parser.parse_args()
    main(backfill=args.backfill)
