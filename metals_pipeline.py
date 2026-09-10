#!/usr/bin/env python3
"""
Metals Price Pipeline.

Two data sources:
  1. yfinance front-month continuous futures (GC=F, SI=F, PL=F, PA=F) as a
     daily "spot" proxy for gold, silver, platinum, palladium (keyless, free).
     The front-month close is the standard spot proxy and this is the same
     proven fetch path futures_pipeline.py uses. It replaces api.metals.live,
     which went dead (SSL failure on this host).
  2. FRED API - monthly base metals historical prices (IMF PCPS series).

FRED no longer carries gold/platinum/palladium spot series (IBA data deleted
from FRED Jan 2022) and api.metals.live is dead, so precious metals live here
as yfinance front-month closes rather than a dedicated spot feed. The same
contracts are fetched full-OHLCV daily by futures_pipeline.py; this table keeps
the close only so metals_spot stays the one-value-per-metal spot view.

CLI:
  python metals_pipeline.py             # incremental (last 90 days)
  python metals_pipeline.py --backfill  # full FRED history + latest spot

Output:
  storage/raw/metals/metals_spot_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv
from storage_utils import write_partitioned

load_dotenv()

FRED_API_KEY  = os.environ.get("FRED_API_KEY", "")
FRED_BASE     = "https://api.stlouisfed.org/fred/series/observations"
BASE_DIR      = os.path.join("storage", "raw", "metals")
REQUEST_INTERVAL = 0.5
MAX_RETRIES      = 3
BACKOFF_SECONDS  = 60

# Base metals via FRED (IMF PCPS, monthly)
# Precious metals are NOT here: FRED dropped its gold/platinum/palladium spot
# series (IBA data deleted Jan 2022) and api.metals.live went dead. They come
# from YF_SPOT below (yfinance front-month futures as the spot proxy).
FRED_METALS: dict[str, tuple] = {
    "PCOPPUSDM":        ("Copper",               "monthly", "USD/MT"),
    "PALUMUSDM":        ("Aluminum",             "monthly", "USD/MT"),
    "PNICKUSDM":        ("Nickel",               "monthly", "USD/MT"),
    "PZINCUSDM":        ("Zinc",                 "monthly", "USD/MT"),
    "PLEADUSDM":        ("Lead",                 "monthly", "USD/MT"),
    "PIORECRUSDM":      ("Iron Ore",             "monthly", "USD/DMT"),
    "PTINUSDM":         ("Tin",                  "monthly", "USD/MT"),
    "PURANUSDM":        ("Uranium",              "monthly", "USD/lb"),
}

# Precious metals "spot" via yfinance front-month continuous futures.
# series_id is the yf_<contract> slug; value is the last valid daily close.
YF_SPOT: dict[str, tuple] = {
    "GC=F":  ("Gold",      "yf_gc"),
    "SI=F":  ("Silver",    "yf_si"),
    "PL=F":  ("Platinum",  "yf_pl"),
    "PA=F":  ("Palladium", "yf_pa"),
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
                print(f"  HTTP {r.status_code}: {r.text[:120]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    return None


def fetch_fred_series(series_id, observation_start=None):
    params = {
        "series_id":  series_id,
        "api_key":    FRED_API_KEY,
        "file_type":  "json",
        "sort_order": "asc",
        "limit":      100000,
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


def fetch_yf_spot():
    """Fetch the latest daily close for each precious-metal front-month contract.

    Returns (df, failed): df with one row per metal (tz-naive trading date from
    the bar itself, NOT the wall-clock run date), or empty df if nothing landed;
    failed = list of contracts that errored or returned no valid close.
    """
    rows = []
    failed = []
    for symbol, (display_name, series_id) in YF_SPOT.items():
        try:
            hist = yf.Ticker(symbol).history(period="2d", auto_adjust=True)
            closes = hist["Close"].dropna()
            if closes.empty:
                print(f"  {symbol} ({display_name}): no valid close")
                failed.append(symbol)
                time.sleep(REQUEST_INTERVAL)
                continue
            rows.append({
                "series_id": series_id,
                "name":      display_name,
                "frequency": "daily",
                "unit":      "USD/troy oz",
                "source":    "yfinance",
                "date":      pd.Timestamp(closes.index[-1]).tz_localize(None),
                "value":     float(closes.iloc[-1]),
            })
            print(f"  {symbol} ({display_name}): {closes.iloc[-1]:.2f} on {closes.index[-1].date()}")
        except Exception as e:
            print(f"  {symbol} ({display_name}): error: {e}")
            failed.append(symbol)
        time.sleep(REQUEST_INTERVAL)
    return pd.DataFrame(rows), failed


def main():
    parser = argparse.ArgumentParser(description="Metals price pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full FRED history")
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

    print(f"Metals Pipeline  mode={mode}\n")
    os.makedirs(BASE_DIR, exist_ok=True)
    frames = []

    # Part 1: precious-metals spot proxy via yfinance front-month futures
    print("[yfinance] Fetching gold/silver/platinum/palladium closes...")
    spot_df, spot_failed = fetch_yf_spot()
    if spot_df is not None and not spot_df.empty:
        spot_df["fetched_at"] = now.isoformat()
        frames.append(spot_df)
        print(f"  {len(spot_df)} metals fetched from yfinance")
    if spot_failed:
        print(f"  WARNING: no spot close for: {', '.join(spot_failed)}")
    if not spot_failed and (spot_df is None or spot_df.empty):
        print("  WARNING: precious-metals spot fetch returned nothing")

    # Part 2: FRED historical base metals
    print(f"\n[fred_metals] Fetching {len(FRED_METALS)} series from FRED...")
    failed = []
    for i, (series_id, (name, frequency, unit)) in enumerate(FRED_METALS.items(), 1):
        print(f"  [{i}/{len(FRED_METALS)}] {series_id} - {name}...", end=" ")
        df = fetch_fred_series(series_id, observation_start)
        if df is None or df.empty:
            print("no data")
            failed.append(series_id)
            time.sleep(REQUEST_INTERVAL)
            continue

        df["series_id"] = series_id
        df["name"]      = name
        df["frequency"] = frequency
        df["unit"]      = unit
        df["source"]    = "FRED/IMF"
        df["fetched_at"] = now.isoformat()
        frames.append(df)
        print(f"{len(df):,} rows")
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        print("\nNo data fetched.")
        return

    combined = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["series_id", "date"])
        .sort_values(["series_id", "date"])
        .reset_index(drop=True)
    )

    path = write_partitioned(
        combined, BASE_DIR,
        f"metals_spot_{mode}_{today_str}.parquet",
    )
    print(f"\n-> {path}")
    print(f"   {len(combined):,} rows | {combined['series_id'].nunique()} series")
    if failed:
        print(f"   No data: {', '.join(failed)}")
    if spot_failed:
        print(f"   Spot fetch failed for: {', '.join(spot_failed)}")

    print("\n--- METALS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()