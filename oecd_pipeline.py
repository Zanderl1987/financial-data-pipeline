#!/usr/bin/env python3
"""
OECD Macro Indicators Pipeline.

Fetches key economic indicators from the OECD SDMX-JSON API for 14 major economies.
Completely keyless — no API key required.

Indicators (from MEI — Main Economic Indicators dataset):
  LRHUTTTT  Unemployment rate
  CP01000   CPI inflation (YoY)
  PRMNTO01  Industrial production index
  IRLT      Long-term interest rate (10Y govt bond)
  IR3TBB01  Short-term interest rate (3M interbank)
  NAEXKP01  GDP growth rate (quarterly volume)
  CCRETT01  Consumer confidence index
  BPBLTD02  Current account balance (% of GDP, annual)

Countries: USA GBR DEU FRA JPN CAN ITA ESP NLD CHE SWE AUS KOR CHN

CLI:
  python oecd_pipeline.py             # last 5 years
  python oecd_pipeline.py --backfill  # full available history

Outputs:
  storage/raw/oecd/oecd_macro_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_URL        = "https://stats.oecd.org/SDMX-JSON/data"
OECD_DIR        = os.path.join("storage", "raw", "oecd")
REQUEST_INTERVAL = 2.0
MAX_RETRIES     = 3

COUNTRIES = "USA+GBR+DEU+FRA+JPN+CAN+ITA+ESP+NLD+CHE+SWE+AUS+KOR+CHN"

# (subject, description, frequency, unit)
INDICATORS = [
    ("LRHUTTTT", "Unemployment Rate",                        "Q", "Percent"),
    ("CP01000",  "CPI Total YoY",                           "Q", "Percent change"),
    ("PRMNTO01", "Industrial Production Index",              "Q", "Index 2015=100"),
    ("IRLT",     "Long-Term Interest Rate 10Y Govt Bond",   "Q", "Percent per annum"),
    ("IR3TBB01", "Short-Term Interest Rate 3M Interbank",  "Q", "Percent per annum"),
    ("NAEXKP01", "GDP Growth Rate Quarterly Volume",        "Q", "Percent change"),
    ("CCRETT01", "Consumer Confidence Index",               "Q", "Normal=100"),
    ("BPBLTD02", "Current Account Balance Pct GDP",        "A", "Percent of GDP"),
]


def _get(url, params=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=60)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                time.sleep(60 * attempt)
            elif resp.status_code == 404:
                print(f"  404 Not Found: {url}")
                return None
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:200]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(20 * attempt)
    return None


def _parse_sdmx_json(data, subject, description, unit):
    """Parse OECD SDMX-JSON 1.0 format into row dicts."""
    try:
        structure = data["structure"]
        series_dims = structure["dimensions"]["series"]
        obs_dims    = structure["dimensions"]["observation"]

        # Build index: dim_id -> list of value IDs in order
        series_dim_values = [
            [v["id"] for v in d.get("values", [])]
            for d in series_dims
        ]
        obs_time_values = [v["id"] for v in obs_dims[0].get("values", [])]

        # Find which series dimension is LOCATION / REF_AREA
        loc_dim_idx = next(
            (i for i, d in enumerate(series_dims) if d["id"] in ("LOCATION", "REF_AREA")),
            None
        )

        rows = []
        dataset = data.get("dataSets", [{}])[0]
        for series_key, series_obj in dataset.get("series", {}).items():
            key_parts = [int(x) for x in series_key.split(":")]
            country = None
            if loc_dim_idx is not None and loc_dim_idx < len(key_parts):
                idx = key_parts[loc_dim_idx]
                vals = series_dim_values[loc_dim_idx]
                if idx < len(vals):
                    country = vals[idx]

            for obs_key, obs_val in series_obj.get("observations", {}).items():
                value = obs_val[0] if obs_val else None
                if value is None:
                    continue
                obs_idx = int(obs_key)
                if obs_idx >= len(obs_time_values):
                    continue
                period = obs_time_values[obs_idx]
                # Normalize period to YYYY-MM-DD
                if "-Q" in period:
                    yr, q = period.split("-Q")
                    date_str = f"{yr}-{(int(q) - 1) * 3 + 1:02d}-01"
                elif len(period) == 4:
                    date_str = f"{period}-01-01"
                elif len(period) == 7 and "-" in period:
                    date_str = f"{period}-01"
                else:
                    date_str = period

                rows.append({
                    "country_code": country,
                    "indicator":    subject,
                    "description":  description,
                    "date":         date_str,
                    "value":        float(value),
                    "unit":         unit,
                })
        return rows
    except Exception as exc:
        print(f"  Parse error: {exc}")
        return []


def fetch_indicator(subject, description, frequency, unit, start_period):
    key  = f"{subject}.{COUNTRIES}.{frequency}"
    url  = f"{BASE_URL}/MEI/{key}/all"
    data = _get(url, params={"startTime": start_period, "contentType": "json"})
    time.sleep(REQUEST_INTERVAL)
    if not data:
        return pd.DataFrame()
    rows = _parse_sdmx_json(data, subject, description, unit)
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="OECD macro indicators pipeline (keyless)")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full available history")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"
    start      = "1960-Q1" if args.backfill else f"{now.year - 5}-Q1"

    print(f"OECD Pipeline  mode={mode}  start={start}")
    print(f"Countries: {COUNTRIES.replace('+', ', ')}\n")
    os.makedirs(OECD_DIR, exist_ok=True)

    all_frames = []
    for subject, description, freq, unit in INDICATORS:
        print(f"  [{subject}] {description}...")
        df = fetch_indicator(subject, description, freq, unit, start)
        if not df.empty:
            all_frames.append(df)
            n_countries = df["country_code"].nunique()
            print(f"    {len(df):,} rows, {n_countries} countries")
        else:
            print(f"    No data returned")

    if not all_frames:
        print("\nNo data returned.")
        return

    combined = (
        pd.concat(all_frames, ignore_index=True)
        .drop_duplicates(subset=["country_code", "indicator", "date"])
        .sort_values(["indicator", "country_code", "date"])
        .reset_index(drop=True)
    )
    combined["fetched_at"] = fetched_at

    path = write_partitioned(combined, OECD_DIR,
                             f"oecd_macro_{mode}_{today_str}.parquet")
    print(f"\n-> {path}  ({len(combined):,} rows, {combined['indicator'].nunique()} indicators, "
          f"{combined['country_code'].nunique()} countries)")
    print("\n--- OECD PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
