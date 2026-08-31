"""
FRED Credit / Fixed-Income Index Pipeline.

Free proxy for the Bloomberg (legacy Barclays) fixed-income index family, which
is NOT itself available on FRED (BBUSATR/LBUSTRUU 400 "series does not exist").
FRED carries the ICE BofA (formerly BofA Merrill Lynch / "Bloomberg Barclays")
credit index series instead, plus daily OAS spread and total-return levels that
are the standard free stand-in for the Bloomberg Corporate / High Yield indices.

Also wired in as the index-level fallback for the S&P 500 sector/index work —
see docs/RESEARCH_NOTES_INDEX_DATA.md for the source-vetting verdict.

Bloomberg-constituents note: no free source of bond-level membership exists for
the Bloomberg fixed-income indices (proprietary). This pipeline intentionally
provides the proxy the user accepted: index-level OAS / total-return series.
Use AGG/BND ETF prices (market_history) for the Aggregate *level*.

CLI:
  python fred_credit_pipeline.py             # incremental (last 90 days)
  python fred_credit_pipeline.py --backfill  # full available history

Output:
  storage/raw/fred_credit/fred_credit_{mode}_{YYYYMMDD}.parquet
  CATALOG table: fred_credit
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

FRED_API_KEY = os.environ.get("FRED_API_KEY")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

BASE_DIR = os.path.join("storage", "raw", "fred_credit")

REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3
BACKOFF_SECONDS = 60

# (series_id, name, unit) -- all verified live against FRED 2026-08-31.
SERIES = {
    # --- OAS spreads (daily) ---
    "BAMLC0A0CM":        ("ICE BofA US Corporate Index Option-Adjusted Spread",     "Percent"),
    "BAMLC0A1CAAA":      ("ICE BofA AAA US Corporate Index Option-Adjusted Spread", "Percent"),
    "BAMLC0A4CBBB":      ("ICE BofA BBB US Corporate Index Option-Adjusted Spread", "Percent"),
    "BAMLH0A0HYM2":      ("ICE BofA US High Yield Index Option-Adjusted Spread",    "Percent"),
    "BAMLH0A1HYBB":      ("ICE BofA BB US High Yield Index Option-Adjusted Spread", "Percent"),
    "BAMLH0A3HYC":       ("ICE BofA CCC & Lower US High Yield Index OAS",           "Percent"),
    # --- Total return index levels (daily) ---
    "BAMLCC0A0CMTRIV":   ("ICE BofA US Corporate Index Total Return",               "Index"),
    "BAMLHYH0A0HYM2TRIV": ("ICE BofA US High Yield Index Total Return",             "Index"),
}


def get_with_backoff(url, params):
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
    print(f"  Giving up after {MAX_RETRIES} attempts: {params.get('series_id')}")
    return None


def fetch_series(series_id, observation_start=None):
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "asc",
        "limit": 100000,
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
    return df


def main(backfill=False):
    os.makedirs(BASE_DIR, exist_ok=True)

    if backfill:
        observation_start = None
        print("Mode: BACKFILL (full history)")
    else:
        observation_start = (datetime.datetime.utcnow() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
        print(f"Mode: INCREMENTAL (from {observation_start})")

    frames = []
    failed = []

    total = len(SERIES)
    for i, (series_id, (name, unit)) in enumerate(SERIES.items(), 1):
        print(f"[{i}/{total}] {series_id} -- {name}...")
        df = fetch_series(series_id, observation_start)

        if df is None or df.empty:
            print("  No data returned.")
            failed.append(series_id)
            time.sleep(REQUEST_INTERVAL)
            continue

        df["series_id"] = series_id
        df["name"] = name
        df["unit"] = unit
        df["fetched_at"] = datetime.datetime.utcnow().isoformat()
        frames.append(df)
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        print("No data retrieved for any series.")
        if failed:
            print(f"Failed/empty ({len(failed)}): {', '.join(failed)}")
        return

    out_df = pd.concat(frames, ignore_index=True)
    today = datetime.datetime.utcnow().strftime("%Y%m%d")
    mode_tag = "backfill" if backfill else "incremental"
    filename = f"fred_credit_{mode_tag}_{today}.parquet"
    path = write_partitioned(out_df, BASE_DIR, filename)

    print(f"\n  -> {path} ({len(out_df):,} rows, {out_df['series_id'].nunique()} series)")
    for sid, n in out_df.groupby("series_id").size().items():
        print(f"    {sid:22s} {n:>8,}")
    if failed:
        print(f"\nFailed/empty ({len(failed)}): {', '.join(failed)}")

    print("\n--- FRED CREDIT PIPELINE COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FRED Credit / Fixed-Income Index Pipeline (ICE BofA proxies for Bloomberg indices)"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch full available history for all series (use on first run).",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
