#!/usr/bin/env python3
"""
Tiingo Fundamentals Pipeline.

Tiingo provides fundamental data via a third-party provider, covering 5,500+
US equities and ADRs with 20+ years of history. This pipeline fetches two
complementary datasets:

  1. Daily Metrics — price-dependent metrics that update daily:
     marketCap, enterpriseVal, peRatio, pbRatio, trailingPEG1Y, and more.
     (Tiingo adds new columns over time; we capture all available fields.)

  2. Statements — quarterly and annual financial statement data:
     income statement, balance sheet, cash flow, and overview ratios.
     Each statement period contains ~60+ normalized data codes.

Data is structured and normalized across tickers for easy backtests.
Updated within 12-24 hours of SEC filing publication.

NOTE: Fundamentals API is an add-on subscription. DOW 30 tickers are
available for free/evaluation. Full coverage requires a paid plan.

Requires: TIINGO_API_KEY in .env

CLI:
  python tiingo_fundamentals_pipeline.py             # incremental (90 days)
  python tiingo_fundamentals_pipeline.py --backfill  # full history (20+ years)
  python tiingo_fundamentals_pipeline.py --symbols AAPL,MSFT,NVDA

Outputs:
  storage/raw/tiingo/fundamentals_daily/tiingo_fundamentals_daily_{mode}_{YYYYMMDD}.parquet
  storage/raw/tiingo/fundamentals_statements/tiingo_fundamentals_statements_{mode}_{YYYYMMDD}.parquet
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
REQUEST_INTERVAL = 0.35   # Tiingo free: 50 req/hr; fundamentals add-on is more generous
MAX_RETRIES = 3

# DOW 30 + major tech + sector ETFs — fundamentals available on free tier
DEFAULT_SYMBOLS = [
    # DOW 30
    "AAPL", "AMGN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "DOW",
    "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM", "KO", "MCD", "MMM",
    "MRK", "MSFT", "NKE", "PG", "TRV", "UNH", "V", "VZ", "WBA", "WMT",
    # High-interest large-cap tech
    "NVDA", "META", "GOOGL", "AMZN", "TSLA", "PLTR", "AMD", "ORCL", "NFLX",
]


def make_headers():
    return {"Authorization": f"Token {TIINGO_API_KEY}", "Content-Type": "application/json"}


def get_with_backoff(url, params=None):
    """GET with retry + exponential backoff on 429/5xx."""
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
                print(f"  HTTP {resp.status_code} for {url}: symbol likely lacks fundamentals "
                      f"add-on entitlement or has no data. Skipping.")
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
# Daily Metrics
# ---------------------------------------------------------------------------

def fetch_daily_metrics(symbol, start_date=None, end_date=None):
    """
    Fetch daily fundamental metrics for one symbol.

    Returns list of dicts (one per trading day) or None.
    Tiingo returns columns dynamically — we capture all available fields.
    Known fields: marketCap, enterpriseVal, peRatio, pbRatio, trailingPEG1Y,
    forwardPE, priceToSales, priceToCashFlow, dividendYield, and more.
    """
    url = f"{BASE_URL}/tiingo/fundamentals/{symbol}/daily"
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
        row = {"symbol": symbol}
        for key, val in obs.items():
            # Normalize column names to snake_case
            col = key
            if col == "date":
                row["date"] = str(val)[:10]
            else:
                # camelCase -> snake_case
                snake = ""
                for i, ch in enumerate(col):
                    if ch.isupper() and i > 0 and col[i - 1].islower():
                        snake += "_"
                    elif ch.isupper() and i > 0 and i + 1 < len(col) and col[i + 1].islower():
                        snake += "_"
                    snake += ch.lower()
                row[snake] = val
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Statements (Quarterly + Annual)
# ---------------------------------------------------------------------------

def fetch_statements(symbol, start_date=None, end_date=None, as_reported=False):
    """
    Fetch quarterly and annual financial statement data for one symbol.

    Returns a flat list of dicts — one row per (period, statement_type, data_code).
    Statement types: balanceSheet, incomeStatement, cashFlow, overview.

    Each row contains:
      symbol, date, fiscal_year, quarter, statement_type, data_code, value, as_reported
    """
    url = f"{BASE_URL}/tiingo/fundamentals/{symbol}/statements"
    params = {
        "asReported": "true" if as_reported else "false",
    }
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date

    data = get_with_backoff(url, params)
    if not data or not isinstance(data, list):
        return None

    rows = []
    for entry in data:
        period_date = str(entry.get("date", ""))[:10]
        fiscal_year = entry.get("year")
        quarter = entry.get("quarter")  # 0=annual, 1-4=fiscal quarter
        statement_data = entry.get("statementData", {})

        # statementData is a dict with keys: balanceSheet, incomeStatement,
        # cashFlow, overview — each value is a list of {dataCode, value} pairs
        for stype in ["balanceSheet", "incomeStatement", "cashFlow", "overview"]:
            items = statement_data.get(stype, [])
            if not items:
                continue
            for item in items:
                data_code = item.get("dataCode", "")
                value = item.get("value")
                if data_code:
                    rows.append({
                        "symbol":       symbol,
                        "date":         period_date,
                        "fiscal_year":  fiscal_year,
                        "quarter":      quarter,
                        "statement_type": stype,
                        "data_code":    data_code,
                        "value":        value,
                        "as_reported":  as_reported,
                    })

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Tiingo Fundamentals Pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full available history (20+ years)")
    parser.add_argument("--symbols", default="",
                        help="Comma-separated symbol list (default: DOW 30 + large-cap tech)")
    parser.add_argument("--as-reported", action="store_true",
                        help="Fetch data as originally reported to SEC (default: restated)")
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
        start_date = "2000-01-01"
        print(f"Mode: BACKFILL  symbols={len(symbols)}  from={start_date}")
    else:
        start_date = (now - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
        print(f"Mode: INCREMENTAL  symbols={len(symbols)}  from={start_date}")

    daily_dir = os.path.join(BASE_DIR, "fundamentals_daily")
    stmt_dir = os.path.join(BASE_DIR, "fundamentals_statements")
    for d in [daily_dir, stmt_dir]:
        os.makedirs(d, exist_ok=True)

    # ---- Daily Metrics ----
    print(f"\n[tiingo_fundamentals_daily] Fetching daily metrics for {len(symbols)} symbols...")
    daily_frames = []
    daily_failed = []

    for i, sym in enumerate(symbols, 1):
        data = fetch_daily_metrics(sym, start_date=start_date, end_date=today)
        if data:
            df = pd.DataFrame(data)
            daily_frames.append(df)
            # Count unique columns (excluding symbol/date)
            metric_cols = [c for c in df.columns if c not in ("symbol", "date")]
            print(f"  [{i}/{len(symbols)}] {sym}: {len(data)} days, {len(metric_cols)} metrics")
        else:
            daily_failed.append(sym)
            print(f"  [{i}/{len(symbols)}] {sym}: no data")
        time.sleep(REQUEST_INTERVAL)

    if daily_frames:
        daily_df = pd.concat(daily_frames, ignore_index=True)
        # Standardize numeric columns (everything except symbol, date)
        for col in daily_df.columns:
            if col not in ("symbol", "date"):
                daily_df[col] = pd.to_numeric(daily_df[col], errors="coerce")
        daily_df["fetched_at"] = now.isoformat()
        path = write_partitioned(
            daily_df, daily_dir,
            f"tiingo_fundamentals_daily_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(daily_df):,} rows, {daily_df['symbol'].nunique()} symbols)")
    else:
        print("  No daily metrics returned.")

    if daily_failed:
        print(f"  Failed/not found ({len(daily_failed)}): {', '.join(daily_failed)}")

    # ---- Statements ----
    print(f"\n[tiingo_fundamentals_statements] Fetching statements for {len(symbols)} symbols...")
    stmt_frames = []
    stmt_failed = []

    for i, sym in enumerate(symbols, 1):
        data = fetch_statements(sym, start_date=start_date, end_date=today,
                                as_reported=args.as_reported)
        if data:
            df = pd.DataFrame(data)
            stmt_frames.append(df)
            n_codes = df["data_code"].nunique()
            n_periods = df["date"].nunique()
            print(f"  [{i}/{len(symbols)}] {sym}: {len(data)} rows, {n_codes} codes, {n_periods} periods")
        else:
            stmt_failed.append(sym)
            print(f"  [{i}/{len(symbols)}] {sym}: no data")
        time.sleep(REQUEST_INTERVAL)

    if stmt_frames:
        stmt_df = pd.concat(stmt_frames, ignore_index=True)
        # Standardize numeric value column
        stmt_df["value"] = pd.to_numeric(stmt_df["value"], errors="coerce")
        stmt_df["fetched_at"] = now.isoformat()
        path = write_partitioned(
            stmt_df, stmt_dir,
            f"tiingo_fundamentals_statements_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(stmt_df):,} rows, {stmt_df['symbol'].nunique()} symbols)")
    else:
        print("  No statement data returned.")

    if stmt_failed:
        print(f"  Failed/not found ({len(stmt_failed)}): {', '.join(stmt_failed)}")

    # ---- Summary ----
    print("\n--- TIINGO FUNDAMENTALS PIPELINE COMPLETE ---")
    print(f"  Daily metrics: {len(daily_frames)} DataFrames")
    print(f"  Statements:    {len(stmt_frames)} DataFrames")


if __name__ == "__main__":
    main()
