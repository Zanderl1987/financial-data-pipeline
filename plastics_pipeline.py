#!/usr/bin/env python3
"""
Plastics Production Pipeline — global annual plastic production.

No API key required. Source: Our World in Data's "Global plastics production"
grapher dataset (derived from Geyer et al. 2017 / OECD Global Plastics
Outlook), a keyless CSV export with no rate limit observed.

This is a STATIC historical series, not a live-updating one: it covers
1950-2019 and OWID has not published a newer vintage as of 2026-08. Every
fetch returns the same 69 rows; there is no --backfill/incremental
distinction the way there is for a paginated API. It is fetched fresh each
run (rather than committed as a static file) so a future OWID update is
picked up automatically without a code change.

Plastics PRICE data is not duplicated here — it already exists as PPI index
series (not $/tonne) in bls_pipeline.py (WPU066/WPU0662/WPU0653/WPU06) and
commodity_macro_pipeline.py (PCU325211325211). This pipeline exists only to
fill the missing PRODUCTION side.

No material-specific plastics TRANSPORTATION data source was found free and
keyless as of 2026-08-24: BTS's Freight Analysis Framework (FAF6, SCTG code
24 "Plastics/rubber" — would have covered plastics AND rubber together) is
hosted at faf.ornl.gov, which resets every connection attempt regardless of
User-Agent/headers (not a 403 — a TCP-level reset, consistent with a
firewall/geo block rather than a scrapeable anti-bot page); UN Comtrade's
full history requires a subscription key (only its small unauthenticated
"preview" endpoint works keyless). Neither was pursued further. The
existing freight_ppi/GSCPI shipping-cost proxies in this repo remain the
best available general-purpose stand-in until one of those unblocks.

Outputs:
  storage/raw/plastics/production/plastics_production_{mode}_{YYYYMMDD}.parquet

CLI:
  python plastics_pipeline.py             # fetch current OWID snapshot
  python plastics_pipeline.py --backfill  # same fetch; flag kept for CLI consistency
"""

import argparse
import datetime
import io
import os

import pandas as pd
import requests
from storage_utils import write_partitioned

OWID_URL = "https://ourworldindata.org/grapher/global-plastics-production.csv"
BASE_DIR = os.path.join("storage", "raw", "plastics")
PROD_DIR = os.path.join(BASE_DIR, "production")
REQUEST_TIMEOUT = 30


def fetch_production() -> "pd.DataFrame | None":
    try:
        r = requests.get(OWID_URL, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"  Request error: {e}")
        return None
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:150]}")
        return None
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty:
        return None
    return df


def normalize_df(df: pd.DataFrame, fetched_at: str) -> pd.DataFrame:
    value_col = [c for c in df.columns if c not in ("Entity", "Code", "Year")][0]
    out = pd.DataFrame({
        "country":  df["Entity"],
        "iso_code": df["Code"],
        # Renamed from "Year": DuckDB's hive_partitioning=True treats "year"
        # as a reserved virtual column (from storage/raw/.../year=YYYY/), and
        # silently overwrites a same-named physical column with the fetch
        # year instead of the real observation year. See fao_pipeline.py.
        "obs_year": pd.to_numeric(df["Year"], errors="coerce"),
        "value":    pd.to_numeric(df[value_col], errors="coerce"),
        "unit":     "tonnes",
    })
    out = out.dropna(subset=["obs_year", "value"])
    out["fetched_at"] = fetched_at
    return out


def main():
    parser = argparse.ArgumentParser(description="Plastics production pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="No-op (source always returns full history) -- kept for CLI consistency")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"

    print(f"Plastics Production Pipeline  mode={mode}\n")
    os.makedirs(PROD_DIR, exist_ok=True)

    print("[plastics_production] Fetching OWID global plastics production...")
    df = fetch_production()
    if df is None or df.empty:
        print("  No data returned.")
        return

    df = normalize_df(df, now.isoformat())
    path = write_partitioned(
        df, PROD_DIR,
        f"plastics_production_{mode}_{today_str}.parquet",
    )
    print(f"  -> {path}  ({len(df):,} rows, {int(df['obs_year'].min())}-{int(df['obs_year'].max())})")

    print("\n--- PLASTICS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
