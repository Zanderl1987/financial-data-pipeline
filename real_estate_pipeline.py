#!/usr/bin/env python3
"""
Real Estate Pipeline — FHFA House Price Index + Zillow ZHVI/ZORI.

No API key required. Both sources publish full-history snapshot files, so
--backfill is equivalent to the default (every run re-downloads the current
full history).

FHFA HPI master file (all levels/flavors in one CSV — national, census
division, state, MSA, Puerto Rico; NSA + SA index back to the 1970s-90s
depending on level):
  https://www.fhfa.gov/hpi/download/monthly/hpi_master.csv

Zillow Research (state + metro level; wide date-column format, melted to
long here). URL/format occasionally drifts — same caveat as
worldbank_pink_sheet.py's Pink Sheet URL:
  ZHVI: home value index, USD
  ZORI: observed rent index, USD

CLI:
  python real_estate_pipeline.py             # download + parse all sources
  python real_estate_pipeline.py --backfill  # same (full history always included)
  python real_estate_pipeline.py --only fhfa
  python real_estate_pipeline.py --only zillow

Output:
  storage/raw/fhfa/hpi/fhfa_hpi_{mode}_{YYYYMMDD}.parquet
  storage/raw/zillow/zhvi/zillow_zhvi_{mode}_{YYYYMMDD}.parquet
  storage/raw/zillow/zori/zillow_zori_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import io
import os
import time

import pandas as pd
import requests

from storage_utils import write_partitioned

FHFA_DIR   = os.path.join("storage", "raw", "fhfa", "hpi")
ZHVI_DIR   = os.path.join("storage", "raw", "zillow", "zhvi")
ZORI_DIR   = os.path.join("storage", "raw", "zillow", "zori")

REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3

FHFA_URL = "https://www.fhfa.gov/hpi/download/monthly/hpi_master.csv"

ZILLOW_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
}
ZILLOW_URLS = {
    "zhvi_metro": "https://files.zillowstatic.com/research/public_csvs/zhvi/"
                  "Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "zhvi_state": "https://files.zillowstatic.com/research/public_csvs/zhvi/"
                  "State_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "zori_metro": "https://files.zillowstatic.com/research/public_csvs/zori/"
                  "Metro_zori_uc_sfrcondomfr_sm_month.csv",
}


def _get_with_retry(url: str, headers: dict) -> bytes | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
            print(f"  HTTP {r.status_code} or short response (attempt {attempt}).")
        except requests.RequestException as e:
            print(f"  Error (attempt {attempt}): {e}")
        time.sleep(REQUEST_INTERVAL)
    return None


# ---------------------------------------------------------------------------
# FHFA
# ---------------------------------------------------------------------------

def fetch_fhfa(now: datetime.datetime) -> pd.DataFrame | None:
    print("[fhfa] Downloading hpi_master.csv...")
    content = _get_with_retry(FHFA_URL, headers={"User-Agent": "Mozilla/5.0 financial-data-pipeline/1.0"})
    if content is None:
        print("  Failed to download FHFA HPI master file.")
        return None

    df = pd.read_csv(io.BytesIO(content), low_memory=False)
    df["date"] = pd.to_datetime(
        df["yr"].astype(str) + "-" + df["period"].astype(str) + "-01",
        errors="coerce",
    )
    df = df.dropna(subset=["date"])
    df["index_nsa"] = pd.to_numeric(df["index_nsa"], errors="coerce")
    df["index_sa"] = pd.to_numeric(df["index_sa"], errors="coerce")
    # Drop "yr"/"period" (fully represented by "date") rather than renaming
    # "yr" -> "year": DuckDB's hive_partitioning=True (used on the raw glob
    # view) treats "year"/"month" as reserved virtual columns derived from
    # the storage/raw/.../year=YYYY/month=MM directory, and silently
    # overwrites a same-named physical column with the partition's value.
    df = df.drop(columns=[c for c in ("H", "yr", "period") if c in df.columns])
    df["source"] = "FHFA HPI"
    df["fetched_at"] = now.isoformat()
    print(f"  Parsed {len(df):,} rows across {df['level'].nunique()} geography levels.")
    return df


# ---------------------------------------------------------------------------
# Zillow
# ---------------------------------------------------------------------------

def fetch_zillow_csv(key: str, url: str) -> pd.DataFrame | None:
    print(f"[zillow] Downloading {key}...")
    content = _get_with_retry(url, headers=ZILLOW_HEADERS)
    if content is None:
        print(f"  Failed to download {key}.")
        return None
    df = pd.read_csv(io.BytesIO(content))
    return df


def melt_zillow(df: pd.DataFrame, value_name: str) -> pd.DataFrame:
    id_cols = [c for c in ("RegionID", "SizeRank", "RegionName", "RegionType", "StateName") if c in df.columns]
    date_cols = [c for c in df.columns if c not in id_cols]
    long_df = df.melt(id_vars=id_cols, value_vars=date_cols, var_name="date", value_name=value_name)
    long_df["date"] = pd.to_datetime(long_df["date"], errors="coerce")
    long_df[value_name] = pd.to_numeric(long_df[value_name], errors="coerce")
    long_df = long_df.dropna(subset=["date", value_name])
    long_df = long_df.rename(columns={
        "RegionID": "region_id", "SizeRank": "size_rank", "RegionName": "region_name",
        "RegionType": "region_type", "StateName": "state_name",
    })
    return long_df.reset_index(drop=True)


def fetch_zillow(now: datetime.datetime) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    zhvi_frames = []
    for key in ("zhvi_metro", "zhvi_state"):
        raw = fetch_zillow_csv(key, ZILLOW_URLS[key])
        if raw is not None:
            long_df = melt_zillow(raw, "zhvi")
            zhvi_frames.append(long_df)
        time.sleep(REQUEST_INTERVAL)

    zhvi_df = None
    if zhvi_frames:
        zhvi_df = pd.concat(zhvi_frames, ignore_index=True)
        zhvi_df["source"] = "Zillow Research ZHVI"
        zhvi_df["fetched_at"] = now.isoformat()
        print(f"  ZHVI: {len(zhvi_df):,} rows, {zhvi_df['region_name'].nunique()} regions.")

    zori_df = None
    raw = fetch_zillow_csv("zori_metro", ZILLOW_URLS["zori_metro"])
    if raw is not None:
        zori_df = melt_zillow(raw, "zori")
        zori_df["source"] = "Zillow Research ZORI"
        zori_df["fetched_at"] = now.isoformat()
        print(f"  ZORI: {len(zori_df):,} rows, {zori_df['region_name'].nunique()} regions.")

    return zhvi_df, zori_df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="FHFA HPI + Zillow ZHVI/ZORI real estate pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Same as default — full history is always included")
    parser.add_argument("--only", choices=["fhfa", "zillow"], help="Run only one source")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"

    os.makedirs(FHFA_DIR, exist_ok=True)
    os.makedirs(ZHVI_DIR, exist_ok=True)
    os.makedirs(ZORI_DIR, exist_ok=True)

    if args.only in (None, "fhfa"):
        fhfa_df = fetch_fhfa(now)
        if fhfa_df is not None and not fhfa_df.empty:
            path = write_partitioned(fhfa_df, FHFA_DIR, f"fhfa_hpi_{mode}_{today_str}.parquet")
            print(f"  -> {path}\n")

    if args.only in (None, "zillow"):
        zhvi_df, zori_df = fetch_zillow(now)
        if zhvi_df is not None and not zhvi_df.empty:
            path = write_partitioned(zhvi_df, ZHVI_DIR, f"zillow_zhvi_{mode}_{today_str}.parquet")
            print(f"  -> {path}")
        if zori_df is not None and not zori_df.empty:
            path = write_partitioned(zori_df, ZORI_DIR, f"zillow_zori_{mode}_{today_str}.parquet")
            print(f"  -> {path}")

    print("\n--- REAL ESTATE PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
