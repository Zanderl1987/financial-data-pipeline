#!/usr/bin/env python3
"""
OECD Macro Indicators Pipeline (revised 2026-07-29).

Uses the OECD SDMX 2.1 REST API via CSV format (the old stats.oecd.org/SDMX-JSON
endpoint was decommissioned Jan 2025).

Strategy: 2 wildcard queries (KEI + LFS), filter locally.
Keyless — no API key required.

Indicators (mapped from old MEI codes to new KEI/LFS MEASURE codes):
  LRHUTTTT -> UNEMP (LFS dataflow)
  CP01000  -> CP    (KEI dataflow)
  PRMNTO01 -> MANM  (KEI)
  IRLT     -> IRLT  (KEI)
  IR3TBB01 -> IR3TIB (KEI)
  NAEXKP01 -> B1GQ_Q (KEI)
  CCRETT01 -> CC    (KEI)
  BPBLTD02 -> CA_GDP (KEI)

Countries: USA GBR DEU FRA JPN CAN ITA ESP NLD CHE SWE AUS KOR CHN

CLI:
  python oecd_pipeline.py             # last 5 years
  python oecd_pipeline.py --backfill  # full available history

Outputs:
  storage/raw/oecd/oecd_macro_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import io
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

KEI_WILDCARD  = "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_KEI@DF_KEI,4.0/."
LFS_WILDCARD  = "https://sdmx.oecd.org/public/rest/data/OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0/........."
OECD_DIR      = os.path.join("storage", "raw", "oecd")
MAX_RETRIES   = 3
RETRY_DELAY   = 60

COUNTRIES = {"USA", "GBR", "DEU", "FRA", "JPN", "CAN", "ITA", "ESP", "NLD", "CHE", "SWE", "AUS", "KOR", "CHN"}

# Maps old indicator id -> (kei_measure, unit, unit_measure, adjustment, transformation, activity)
KEI_FILTERS = {
    "CP01000":  ("CP",     "Percent change",     "GR", "_Z", "GY", "_Z"),
    "PRMNTO01": ("MANM",   "Index 2015=100",     "IX", "Y",  "_Z", "_Z"),
    "IRLT":     ("IRLT",   "Percent per annum",   "PA", "_Z", "_Z", "_Z"),
    "IR3TBB01": ("IR3TIB", "Percent per annum",   "PA", "_Z", "_Z", "_Z"),
    "NAEXKP01": ("B1GQ_Q", "Percent change",      "GR", "Y",  "GY", "_T"),
    "CCRETT01": ("CC",     "Normal=100",          "XDC_USD", "_Z", "_Z", "_Z"),
    "BPBLTD02": ("CA_GDP", "Percent of GDP",      "PT_B1GQ", "Y",  "_Z", "_T"),
}

# Old -> new indicator descriptions for KEI measures
KEI_DESCRIPTIONS = {
    "CP01000": "CPI Total YoY",
    "PRMNTO01": "Industrial Production Index",
    "IRLT": "Long-Term Interest Rate 10Y Govt Bond",
    "IR3TBB01": "Short-Term Interest Rate 3M Interbank",
    "NAEXKP01": "GDP Growth Rate Quarterly Volume",
    "CCRETT01": "Consumer Confidence Index",
    "BPBLTD02": "Current Account Balance Pct GDP",
}

LFS_FILTER = {
    "id": "LRHUTTTT",
    "description": "Unemployment Rate",
    "unit": "Percent",
    "measure": "UNE_LF_M",
    "unit_measure": "PT_LF_SUB",
    "transformation": "_Z",
    "adjustment": "N",
    "sex": "_T",
    "age": "Y_GE15",
    "activity": "_Z",
    "freq": "M",
}


def _fetch_csv(url, start_period):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            params = {"startPeriod": start_period, "format": "csvfilewithlabels"}
            resp = requests.get(url, params=params, timeout=120)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 429:
                wait = RETRY_DELAY * attempt
                print(f"    Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            print(f"    HTTP {resp.status_code}: {resp.text[:120]}")
            return None
        except requests.RequestException as exc:
            print(f"    Request error (attempt {attempt}): {exc}")
            time.sleep(RETRY_DELAY)
    return None


def _normalize_date(period):
    if "-Q" in str(period):
        yr, q = str(period).split("-Q")
        return f"{yr}-{(int(q) - 1) * 3 + 1:02d}-01"
    if len(str(period)) == 4:
        return f"{period}-01-01"
    if len(str(period)) == 7 and "-" in str(period):
        return f"{period}-01"
    return str(period)


def _extract_kei(csv_text, start_period):
    """Parse KEI wildcard CSV and filter to target indicators/countries."""
    if not csv_text:
        return pd.DataFrame()
    df = pd.read_csv(io.StringIO(csv_text))
    if "OBS_VALUE" not in df.columns:
        return pd.DataFrame()
    obs = df[df["OBS_VALUE"].notna()].copy()

    # Filter to our target countries
    obs = obs[obs["REF_AREA"].isin(COUNTRIES)]
    if obs.empty:
        return pd.DataFrame()

    rows = []
    for old_id, (measure, unit, unit_m, adj, transf, activity) in KEI_FILTERS.items():
        mask = (
            (obs["MEASURE"] == measure)
            & (obs["UNIT_MEASURE"] == unit_m)
            & (obs["ADJUSTMENT"] == adj)
            & (obs["TRANSFORMATION"] == transf)
            & (obs["ACTIVITY"] == activity)
        )
        subset = obs[mask].copy()
        if subset.empty:
            continue
        # Only keep M/Q frequency depending on indicator
        freq = "M" if old_id in ("CP01000", "PRMNTO01", "IRLT", "IR3TBB01", "CCRETT01") else "Q"
        subset = subset[subset["FREQ"] == freq]
        if subset.empty:
            continue
        subset["country_code"] = subset["REF_AREA"]
        subset["indicator"] = old_id
        subset["description"] = KEI_DESCRIPTIONS[old_id]
        subset["date"] = subset["TIME_PERIOD"].apply(_normalize_date)
        subset["value"] = pd.to_numeric(subset["OBS_VALUE"], errors="coerce")
        subset["unit"] = unit
        rows.append(subset)

    if not rows:
        return pd.DataFrame()
    result = pd.concat(rows, ignore_index=True)
    result = result[["country_code", "indicator", "description", "date", "value", "unit"]]
    result = result.dropna(subset=["value"])
    result["value"] = result["value"].astype(float)
    return result.reset_index(drop=True)


def _extract_lfs(csv_text):
    """Parse LFS wildcard CSV and filter to unemployment rate data."""
    if not csv_text:
        return pd.DataFrame()
    df = pd.read_csv(io.StringIO(csv_text))
    if "OBS_VALUE" not in df.columns:
        return pd.DataFrame()
    obs = df[df["OBS_VALUE"].notna()].copy()

    # Filter to our target countries and the right dimension combo
    obs = obs[obs["REF_AREA"].isin(COUNTRIES)]
    if obs.empty:
        return pd.DataFrame()

    f = LFS_FILTER
    mask = (
        (obs["MEASURE"] == f["measure"])
        & (obs["UNIT_MEASURE"] == f["unit_measure"])
        & (obs["TRANSFORMATION"] == f["transformation"])
        & (obs["ADJUSTMENT"] == f["adjustment"])
        & (obs["SEX"] == f["sex"])
        & (obs["AGE"] == f["age"])
        & (obs["ACTIVITY"] == f["activity"])
        & (obs["FREQ"] == f["freq"])
    )
    subset = obs[mask].copy()
    if subset.empty:
        return pd.DataFrame()

    subset["country_code"] = subset["REF_AREA"]
    subset["indicator"] = f["id"]
    subset["description"] = f["description"]
    subset["date"] = subset["TIME_PERIOD"].apply(_normalize_date)
    subset["value"] = pd.to_numeric(subset["OBS_VALUE"], errors="coerce")
    subset["unit"] = f["unit"]

    result = subset[["country_code", "indicator", "description", "date", "value", "unit"]]
    result = result.dropna(subset=["value"])
    result["value"] = result["value"].astype(float)
    return result.reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description="OECD macro indicators pipeline (keyless)")
    parser.add_argument("--backfill", action="store_true", help="Fetch full available history")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode = "backfill" if args.backfill else "incremental"
    start = "1960" if args.backfill else f"{now.year - 5}"

    print(f"OECD Pipeline  mode={mode}  start={start}")
    print(f"Countries: {', '.join(sorted(COUNTRIES))}\n")
    os.makedirs(OECD_DIR, exist_ok=True)

    all_frames = []

    # 1. KEI wildcard -> 7 indicators
    print("  [KEI] Fetching Key Economic Indicators...")
    kei_csv = _fetch_csv(KEI_WILDCARD, start)
    if kei_csv:
        kei_df = _extract_kei(kei_csv, start)
        if not kei_df.empty:
            all_frames.append(kei_df)
            n_inds = kei_df["indicator"].nunique()
            n_ctry = kei_df["country_code"].nunique()
            print(f"    {len(kei_df):,} rows, {n_inds} indicators, {n_ctry} countries")
        else:
            print("    No data after filtering")
    else:
        print("    Failed to fetch KEI data")

    # 2. LFS wildcard -> unemployment
    print("  [LFS] Fetching Labour Force Survey (unemployment)...")
    lfs_csv = _fetch_csv(LFS_WILDCARD, start)
    if lfs_csv:
        lfs_df = _extract_lfs(lfs_csv)
        if not lfs_df.empty:
            all_frames.append(lfs_df)
            n_ctry = lfs_df["country_code"].nunique()
            print(f"    {len(lfs_df):,} rows, {n_ctry} countries")
        else:
            print("    No data after filtering")
    else:
        print("    Failed to fetch LFS data")

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

    path = write_partitioned(combined, OECD_DIR, f"oecd_macro_{mode}_{today_str}.parquet")
    print(f"\n-> {path}  ({len(combined):,} rows, {combined['indicator'].nunique()} indicators, "
          f"{combined['country_code'].nunique()} countries)")
    print("\n--- OECD PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
