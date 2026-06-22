#!/usr/bin/env python3
"""
SimFin Pipeline — Financial Statements & Ratios.

SimFin provides structured income statements, balance sheets, and cash flow
statements sourced from SEC filings. The free API tier has a 12-month data
delay but includes 10+ years of history for 4,000+ US stocks.

Data fetched via SimFin API v3 (direct HTTP, no extra package required):
  - Income statements — revenue, gross profit, EBIT, EBITDA, net income, EPS
  - Balance sheets — assets, liabilities, equity, cash, debt, working capital
  - Cash flow statements — operating CF, capex, FCF, dividends, buybacks
  - Share price history (daily) — for P/E, P/S, P/B ratio computation

All three statements are fetched for both annual and quarterly periods.
The 'period_type' column distinguishes them within each table.

Requires: SIMFIN_API_KEY in .env

CLI:
  python simfin_pipeline.py             # fetch for standard watchlist
  python simfin_pipeline.py --backfill  # same (SimFin always returns full history)
  python simfin_pipeline.py --symbols AAPL,MSFT  # override symbols

Outputs:
  storage/raw/simfin/income/simfin_income_{mode}_{YYYYMMDD}.parquet
  storage/raw/simfin/balance/simfin_balance_{mode}_{YYYYMMDD}.parquet
  storage/raw/simfin/cashflow/simfin_cashflow_{mode}_{YYYYMMDD}.parquet
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

SIMFIN_API_KEY = os.environ.get("SIMFIN_API_KEY", "")
BASE_URL = "https://backend.simfin.com/api/v3"
BASE_DIR = os.path.join("storage", "raw", "simfin")
REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3

# Same core watchlist as tiingo_pipeline; SimFin uses tickers directly
DEFAULT_SYMBOLS = [
    # DJI 30
    "AAPL", "AMGN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "DOW",
    "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM", "KO", "MCD", "MMM",
    "MRK", "MSFT", "NKE", "PG", "TRV", "UNH", "V", "VZ", "WBA", "WMT",
    # High-interest large-cap tech + growth
    "NVDA", "META", "GOOGL", "AMZN", "TSLA", "PLTR", "AMD", "ORCL", "NFLX",
    # Banks & financials
    "BAC", "C", "WFC", "MS",
]

# SimFin statement type codes
STATEMENT_TYPES = {
    "income":   "IS",
    "balance":  "BS",
    "cashflow": "CF",
}


def make_headers():
    return {"Authorization": f"api-key {SIMFIN_API_KEY}"}


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
            elif resp.status_code == 404:
                return None
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:200]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(15 * attempt)
    return None


def fetch_statement(ticker, statement_code, period):
    """
    Fetch one statement type for one ticker and period ('annual' or 'quarterly').
    Returns a list of dicts (one per fiscal period) or empty list.
    """
    url = f"{BASE_URL}/companies/statements/compact"
    params = {
        "ticker":      ticker,
        "statements":  statement_code,
        "period":      period,
    }
    data = get_with_backoff(url, params)
    if not data:
        return []

    # SimFin compact format: {"columns": [...], "data": [[...], ...]}
    # The outer key is the statement code (e.g., "IS", "BS", "CF")
    stmt = data.get(statement_code, {})
    if not stmt:
        # Some responses return data directly
        stmt = data

    columns = stmt.get("columns", [])
    rows_raw = stmt.get("data", [])
    if not columns or not rows_raw:
        return []

    rows = []
    for row in rows_raw:
        record = dict(zip(columns, row))
        record["symbol"] = ticker
        record["period_type"] = period
        rows.append(record)
    return rows


def normalize_columns(df):
    """Lowercase and snake_case all column names."""
    df.columns = (
        df.columns.str.strip()
        .str.lower()
        .str.replace(r"[^a-z0-9]+", "_", regex=True)
        .str.strip("_")
    )
    return df


def main():
    parser = argparse.ArgumentParser(description="SimFin financial statements pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full history (SimFin always returns full history)")
    parser.add_argument("--symbols", default="",
                        help="Comma-separated symbol list (default: standard watchlist)")
    args = parser.parse_args()

    if not SIMFIN_API_KEY:
        print("CRITICAL ERROR: SIMFIN_API_KEY not set in .env. Exiting.")
        return

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"

    print(f"SimFin Pipeline  mode={mode}  symbols={len(symbols)}")
    print("Note: SimFin free tier has a 12-month data delay on recent filings.\n")

    for stmt_name, stmt_code in STATEMENT_TYPES.items():
        output_dir = os.path.join(BASE_DIR, stmt_name)
        os.makedirs(output_dir, exist_ok=True)
        print(f"[simfin_{stmt_name}] Fetching {stmt_name} statements...")

        all_rows = []
        for i, sym in enumerate(symbols, 1):
            for period in ("annual", "quarterly"):
                rows = fetch_statement(sym, stmt_code, period)
                all_rows.extend(rows)
                time.sleep(REQUEST_INTERVAL)
            if i % 10 == 0 or i == len(symbols):
                print(f"  [{i}/{len(symbols)}] processed {sym}...")

        if not all_rows:
            print(f"  No data returned for simfin_{stmt_name}.\n")
            continue

        df = pd.DataFrame(all_rows)
        df = normalize_columns(df)

        # Standardize date column if present (SimFin uses 'Report Date' or 'Fiscal Year')
        for date_col in ("report_date", "fiscal_year", "period_end_date"):
            if date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")

        # Convert all numeric-looking columns
        for col in df.columns:
            if col not in ("symbol", "period_type", "report_date", "fiscal_year",
                           "period_end_date", "currency", "restated_date",
                           "shares_basic", "shares_diluted"):
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df["fetched_at"] = now.isoformat()

        path = write_partitioned(
            df, output_dir,
            f"simfin_{stmt_name}_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(df):,} rows, {df['symbol'].nunique()} symbols, "
              f"{df['period_type'].value_counts().to_dict()})\n")

    print("--- SIMFIN PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
