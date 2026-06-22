#!/usr/bin/env python3
"""
SimFin Pipeline -- Financial Statements.

SimFin provides structured income statements, balance sheets, and cash flow
statements sourced from SEC filings. The free API tier has a 12-month data
delay but includes 10+ years of history for 4,000+ US stocks.

API: SimFin v3 (backend.simfin.com/api/v3)
  Statement codes: PL (income/P&L), BS (balance sheet), CF (cash flow)
  Period codes: FY, Q1, Q2, Q3, Q4, H1, H2, 9M
  Omitting period returns all periods in a single call.

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
BATCH_SIZE = 2  # free tier allows max 2 companies per request

DEFAULT_SYMBOLS = [
    # DJI 30
    "AAPL", "AMGN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "DOW",
    "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM", "KO", "MCD", "MMM",
    "MRK", "MSFT", "NKE", "PG", "TRV", "UNH", "V", "VZ", "WBA", "WMT",
    # Large-cap tech + growth
    "NVDA", "META", "GOOGL", "AMZN", "TSLA", "PLTR", "AMD", "ORCL", "NFLX",
    # Banks & financials
    "BAC", "C", "WFC", "MS",
]

# SimFin v3 statement codes: PL, BS, CF (NOT IS/BS/CF like v2)
STATEMENT_TYPES = {
    "income":   "PL",
    "balance":  "BS",
    "cashflow": "CF",
}


def make_headers():
    return {"Authorization": f"api-key {SIMFIN_API_KEY}"}


def get_with_backoff(url, params=None):
    headers = make_headers()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=60)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  429 rate limit. Waiting {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            elif resp.status_code == 404:
                return None
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:300]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(15 * attempt)
    return None


def fetch_statements_batch(tickers, statement_code):
    """
    Fetch one statement type for a batch of tickers (all fiscal periods).
    Tickers sent as comma-separated string. Omitting period returns all periods.
    Response: list of company objects with nested statements array.
    """
    url = f"{BASE_URL}/companies/statements/compact"
    params = [
        ("ticker",     ",".join(tickers)),
        ("statements", statement_code),
    ]
    return get_with_backoff(url, params) or []


def parse_company_statements(company_data, statement_code, symbol_col="ticker"):
    """
    Parse the v3 response for one company entry into flat row dicts.
    company_data: {"ticker": "AAPL", "statements": [{"statement": "PL", "columns": [...], "data": [[...]]}]}
    """
    ticker = company_data.get("ticker", "")
    name = company_data.get("name", "")
    currency = company_data.get("currency", "")
    stmts = company_data.get("statements", [])

    rows = []
    for stmt in stmts:
        if stmt.get("statement") != statement_code:
            continue
        columns = stmt.get("columns", [])
        raw_data = stmt.get("data", [])
        for row_vals in raw_data:
            record = dict(zip(columns, row_vals))
            record["symbol"] = ticker
            record["company_name"] = name
            record["currency"] = currency
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
        print(f"[simfin_{stmt_name}] Fetching {stmt_name} statements (code={stmt_code})...")

        all_rows = []
        total_batches = (len(symbols) + BATCH_SIZE - 1) // BATCH_SIZE

        for batch_num, batch_start in enumerate(range(0, len(symbols), BATCH_SIZE), 1):
            batch = symbols[batch_start:batch_start + BATCH_SIZE]
            company_list = fetch_statements_batch(batch, stmt_code)

            batch_rows = 0
            for company in company_list:
                rows = parse_company_statements(company, stmt_code)
                all_rows.extend(rows)
                batch_rows += len(rows)

            end_idx = min(batch_start + BATCH_SIZE, len(symbols))
            print(f"  batch {batch_num}/{total_batches} [{end_idx}/{len(symbols)}] "
                  f"+{batch_rows} rows ({len(company_list)} companies)")
            time.sleep(REQUEST_INTERVAL)

        if not all_rows:
            print(f"  No data returned for simfin_{stmt_name}.\n")
            continue

        df = pd.DataFrame(all_rows)
        df = normalize_columns(df)

        # Rename fiscal_period to period_type for consistency
        if "fiscal_period" in df.columns:
            df["period_type"] = df["fiscal_period"]
        elif "period_type" not in df.columns:
            df["period_type"] = "unknown"

        # Standardize date columns
        for date_col in ("report_date", "publish_date", "restated"):
            if date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")

        # Convert numeric columns (skip known string/date columns)
        skip_cols = {"symbol", "company_name", "currency", "period_type", "fiscal_period",
                     "fiscal_year", "report_date", "publish_date", "restated", "source",
                     "value_check", "data_model", "ttm"}
        for col in df.columns:
            if col not in skip_cols:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df["fetched_at"] = now.isoformat()

        period_counts = df["period_type"].value_counts().to_dict() if "period_type" in df.columns else {}
        path = write_partitioned(
            df, output_dir,
            f"simfin_{stmt_name}_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}")
        print(f"     {len(df):,} rows | {df['symbol'].nunique()} symbols | periods: {period_counts}\n")

    print("--- SIMFIN PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
