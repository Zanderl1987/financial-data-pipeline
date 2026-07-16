#!/usr/bin/env python3
"""
Tiingo Corporate Actions Pipeline -- dividends and stock splits.

Fetches comprehensive dividend and split history from Tiingo's corporate
actions API, covering 80,000+ tickers with 60+ years of history.

  Table 1: tiingo_corporate_actions_dividends
    - Ex-date, payment date, declaration date, record date
    - Dividend amount, frequency
    - Historical distribution yield

  Table 2: tiingo_corporate_actions_splits
    - Ex-date, split from/to, split factor
    - Status (active/cancelled)

Requires: TIINGO_API_KEY in .env (same key as existing Tiingo pipelines)

CLI:
  python tiingo_corporate_actions_pipeline.py             # incremental (last 90 days)
  python tiingo_corporate_actions_pipeline.py --backfill  # full history (60+ years)
  python tiingo_corporate_actions_pipeline.py --symbols AAPL,MSFT,NVDA

Outputs:
  storage/raw/tiingo/corporate_actions_dividends/tiingo_corporate_actions_dividends_{mode}_{YYYYMMDD}.parquet
  storage/raw/tiingo/corporate_actions_splits/tiingo_corporate_actions_splits_{mode}_{YYYYMMDD}.parquet
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

TIINGO_API_KEY = os.environ.get("TIINGO_API_KEY", "")
BASE_URL = "https://api.tiingo.com"
BASE_DIR = os.path.join("storage", "raw", "tiingo")
REQUEST_INTERVAL = 0.35
MAX_RETRIES = 3

# DOW 30 + major tech
DEFAULT_SYMBOLS = [
    "AAPL", "AMGN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "DOW",
    "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM", "KO", "MCD", "MMM",
    "MRK", "MSFT", "NKE", "PG", "TRV", "UNH", "V", "VZ", "WBA", "WMT",
    "NVDA", "META", "GOOGL", "AMZN", "TSLA", "PLTR", "AMD", "ORCL", "NFLX",
]


def make_headers():
    return {"Authorization": f"Token {TIINGO_API_KEY}", "Content-Type": "application/json"}


def get_with_backoff(url, params=None):
    headers = make_headers()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  429 rate limit. Waiting {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            elif resp.status_code in (403, 404):
                print(f"  HTTP {resp.status_code} for {url}: symbol likely lacks corporate "
                      f"actions add-on entitlement or has no data. Skipping.")
                return None
            elif resp.status_code >= 500:
                wait = 30 * attempt
                print(f"  HTTP {resp.status_code} for {url} (attempt {attempt}/{MAX_RETRIES}). "
                      f"Retrying in {wait}s.")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code} for {url}: {resp.text[:200]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(15 * attempt)
    return None


# ---------------------------------------------------------------------------
# Dividends
# ---------------------------------------------------------------------------

def fetch_dividends(symbol, start_date=None, end_date=None):
    """Fetch dividend distribution history for one symbol."""
    url = f"{BASE_URL}/tiingo/corporate-actions/{symbol}/distributions"
    params = {}
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date

    data = get_with_backoff(url, params)
    if not data or not isinstance(data, list):
        return None

    rows = []
    for obs in data:
        rows.append({
            "symbol":          symbol,
            "perma_ticker":    obs.get("permaTicker", ""),
            "ex_date":         str(obs.get("exDate", ""))[:10],
            "payment_date":    str(obs.get("paymentDate", ""))[:10],
            "declaration_date": str(obs.get("declarationDate", ""))[:10],
            "record_date":     str(obs.get("recordDate", ""))[:10],
            "amount":          obs.get("amount"),
            "frequency":       obs.get("frequency", ""),
        })

    return rows


# ---------------------------------------------------------------------------
# Distribution Yield
# ---------------------------------------------------------------------------

def fetch_distribution_yield(symbol, start_date=None, end_date=None):
    """Fetch historical distribution yield for one symbol."""
    url = f"{BASE_URL}/tiingo/corporate-actions/{symbol}/distribution-yield"
    params = {}
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date

    data = get_with_backoff(url, params)
    if not data or not isinstance(data, list):
        return None

    rows = []
    for obs in data:
        rows.append({
            "symbol":             symbol,
            "date":               str(obs.get("date", ""))[:10],
            "distribution_yield": obs.get("distributionYield"),
        })

    return rows


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------

def fetch_splits(symbol, start_date=None, end_date=None):
    """Fetch stock split history for one symbol."""
    url = f"{BASE_URL}/tiingo/corporate-actions/{symbol}/splits"
    params = {}
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date

    data = get_with_backoff(url, params)
    if not data or not isinstance(data, list):
        return None

    rows = []
    for obs in data:
        rows.append({
            "symbol":        symbol,
            "perma_ticker":  obs.get("permaTicker", ""),
            "ex_date":       str(obs.get("exDate", ""))[:10],
            "split_from":    obs.get("splitFrom"),
            "split_to":      obs.get("splitTo"),
            "split_factor":  obs.get("splitFactor"),
            "status":        obs.get("status", ""),
        })

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Tiingo Corporate Actions Pipeline (dividends + splits)")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full available history (60+ years)")
    parser.add_argument("--symbols", default="",
                        help="Comma-separated symbol list (default: DOW 30 + large-cap tech)")
    args = parser.parse_args()

    if not TIINGO_API_KEY:
        print("CRITICAL ERROR: TIINGO_API_KEY not set in .env. Exiting.")
        return

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    today = now.strftime("%Y-%m-%d")
    mode = "backfill" if args.backfill else "incremental"

    if args.backfill:
        start_date = "1960-01-01"
        print(f"Mode: BACKFILL  symbols={len(symbols)}  from={start_date}")
    else:
        start_date = (now - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
        print(f"Mode: INCREMENTAL  symbols={len(symbols)}  from={start_date}")

    div_dir = os.path.join(BASE_DIR, "corporate_actions_dividends")
    split_dir = os.path.join(BASE_DIR, "corporate_actions_splits")
    yield_dir = os.path.join(BASE_DIR, "corporate_actions_yield")
    for d in [div_dir, split_dir, yield_dir]:
        os.makedirs(d, exist_ok=True)

    # ---- Dividends ----
    print(f"\n[tiingo_corporate_actions_dividends] Fetching dividends for {len(symbols)} symbols...")
    div_frames = []
    div_failed = []

    for i, sym in enumerate(symbols, 1):
        data = fetch_dividends(sym, start_date=start_date, end_date=today)
        if data:
            df = pd.DataFrame(data)
            div_frames.append(df)
            print(f"  [{i}/{len(symbols)}] {sym}: {len(data)} dividends")
        else:
            div_failed.append(sym)
            print(f"  [{i}/{len(symbols)}] {sym}: no data")
        time.sleep(REQUEST_INTERVAL)

    if div_frames:
        div_df = pd.concat(div_frames, ignore_index=True)
        div_df["amount"] = pd.to_numeric(div_df["amount"], errors="coerce")
        div_df["fetched_at"] = now.isoformat()
        path = write_partitioned(
            div_df, div_dir,
            f"tiingo_corporate_actions_dividends_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(div_df):,} rows, {div_df['symbol'].nunique()} symbols)")
    else:
        print("  No dividend data returned.")

    # ---- Distribution Yield ----
    print(f"\n[tiingo_corporate_actions_yield] Fetching distribution yield for {len(symbols)} symbols...")
    yield_frames = []

    for i, sym in enumerate(symbols, 1):
        data = fetch_distribution_yield(sym, start_date=start_date, end_date=today)
        if data:
            df = pd.DataFrame(data)
            yield_frames.append(df)
            print(f"  [{i}/{len(symbols)}] {sym}: {len(data)} yield records")
        else:
            print(f"  [{i}/{len(symbols)}] {sym}: no data")
        time.sleep(REQUEST_INTERVAL)

    if yield_frames:
        yield_df = pd.concat(yield_frames, ignore_index=True)
        yield_df["distribution_yield"] = pd.to_numeric(yield_df["distribution_yield"], errors="coerce")
        yield_df["fetched_at"] = now.isoformat()
        path = write_partitioned(
            yield_df, yield_dir,
            f"tiingo_corporate_actions_yield_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(yield_df):,} rows, {yield_df['symbol'].nunique()} symbols)")
    else:
        print("  No yield data returned.")

    # ---- Splits ----
    print(f"\n[tiingo_corporate_actions_splits] Fetching splits for {len(symbols)} symbols...")
    split_frames = []
    split_failed = []

    for i, sym in enumerate(symbols, 1):
        data = fetch_splits(sym, start_date=start_date, end_date=today)
        if data:
            df = pd.DataFrame(data)
            split_frames.append(df)
            print(f"  [{i}/{len(symbols)}] {sym}: {len(data)} splits")
        else:
            split_failed.append(sym)
            print(f"  [{i}/{len(symbols)}] {sym}: no data")
        time.sleep(REQUEST_INTERVAL)

    if split_frames:
        split_df = pd.concat(split_frames, ignore_index=True)
        split_df["split_factor"] = pd.to_numeric(split_df["split_factor"], errors="coerce")
        split_df["fetched_at"] = now.isoformat()
        path = write_partitioned(
            split_df, split_dir,
            f"tiingo_corporate_actions_splits_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(split_df):,} rows, {split_df['symbol'].nunique()} symbols)")
    else:
        print("  No split data returned.")

    # ---- Summary ----
    print("\n--- TIINGO CORPORATE ACTIONS PIPELINE COMPLETE ---")
    if div_failed:
        print(f"  Dividend failed ({len(div_failed)}): {', '.join(div_failed)}")
    if split_failed:
        print(f"  Split failed ({len(split_failed)}): {', '.join(split_failed)}")


if __name__ == "__main__":
    main()
