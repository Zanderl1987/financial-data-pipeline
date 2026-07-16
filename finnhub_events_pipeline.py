#!/usr/bin/env python3
"""
Finnhub Events Pipeline:
  - Earnings Calendar (/calendar/earnings) — upcoming + historical earnings
    dates with EPS and revenue estimates vs. actuals. The endpoint is a
    date-range query, NOT per-symbol: a single request returns the whole
    market for the window, so this feed costs exactly one API call per run.
  - Insider Transactions (/stock/insider-transactions) — per-symbol insider
    buy/sell filings (SEC Form 3/4/5). One request per target symbol.
  - IPO Calendar (/calendar/ipo) — upcoming and recently priced IPOs with
    offering price range, shares offered, exchange, and status. Single
    market-wide request per date window, same pattern as earnings calendar.

Rate-limit compliance:
  Reuses the RateLimiter + get_with_backoff fetch layer from finnhub_pipeline.py,
  which enforces ~1.1s spacing between calls (~54 req/min, under Finnhub's
  free-tier limit of 60 calls/minute) plus escalating backoff on HTTP 429.
  Importing the shared layer (rather than re-implementing it) guarantees both
  Finnhub pipelines throttle identically and can't drift apart.

CLI:
  python finnhub_events_pipeline.py             # incremental (recent window)
  python finnhub_events_pipeline.py --backfill  # full history window

Outputs:
  storage/raw/finnhub/earnings_calendar/earnings_calendar_{mode}_{YYYYMMDD}.parquet
  storage/raw/finnhub/insider_transactions/insider_transactions_{mode}_{YYYYMMDD}.parquet
  storage/raw/finnhub/ipo_calendar/ipo_calendar_{mode}_{YYYYMMDD}.parquet
"""

import os
import datetime
import argparse
import pandas as pd

# Shared, policy-conforming fetch layer (RateLimiter + 429 backoff).
from storage_utils import write_partitioned
from finnhub_pipeline import (
    FINNHUB_API_KEY,
    get_with_backoff,
    get_dji_symbols,
)

OUTPUT_BASE = os.path.join("storage", "raw", "finnhub")
DIRS = {
    "earnings_calendar":   os.path.join(OUTPUT_BASE, "earnings_calendar"),
    "insider_transactions": os.path.join(OUTPUT_BASE, "insider_transactions"),
    "ipo_calendar":        os.path.join(OUTPUT_BASE, "ipo_calendar"),
}

# Date windows (days). Earnings reaches forward to capture upcoming releases;
# insider transactions only look backward (filings are historical).
EARNINGS_BACK = {"incremental": 7,  "backfill": 365}
EARNINGS_FWD  = {"incremental": 30, "backfill": 90}
INSIDER_BACK  = {"incremental": 90, "backfill": 730}
IPO_BACK      = {"incremental": 30, "backfill": 365}
IPO_FWD       = {"incremental": 90, "backfill": 90}

# Standardize API field names to lowercase snake_case for a consistent schema.
EARNINGS_RENAME = {
    "epsActual": "eps_actual",
    "epsEstimate": "eps_estimate",
    "revenueActual": "revenue_actual",
    "revenueEstimate": "revenue_estimate",
    # "year" renamed: DuckDB's hive_partitioning=True treats "year" as a
    # reserved virtual column derived from the directory path (year=YYYY).
    # If the DataFrame also has a "year" column, DuckDB silently overwrites
    # the data values with the partition value — corrupting fiscal-year data.
    "year": "obs_year",
    # date, symbol, hour, quarter already lowercase
}
INSIDER_RENAME = {
    "filingDate": "filing_date",
    "transactionDate": "transaction_date",
    "transactionCode": "transaction_code",
    "transactionPrice": "transaction_price",
    "isDerivative": "is_derivative",
    # name, share, change, symbol, id, source, currency already lowercase
}


IPO_RENAME = {
    "numberOfShares":   "shares_offered",
    "totalSharesValue": "total_value",
    "priceRangeLow":    "price_range_low",
    "priceRangeHigh":   "price_range_high",
    # date, exchange, name, price, status, symbol already lowercase
}


def _now_iso():
    return datetime.datetime.utcnow().isoformat()


def fetch_ipo_calendar(start_date, end_date):
    """One market-wide call returning upcoming and recently priced IPOs."""
    data = get_with_backoff("calendar/ipo", {"from": start_date, "to": end_date})
    if not data:
        return None
    rows = data.get("ipoCalendar")
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns=IPO_RENAME)
    # Normalize numeric fields
    for col in ("shares_offered", "total_value", "price_range_low", "price_range_high", "price"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["fetched_at"] = _now_iso()
    return df


def fetch_earnings_calendar(start_date, end_date):
    """One market-wide call returning every earnings release in the window."""
    data = get_with_backoff("calendar/earnings", {"from": start_date, "to": end_date})
    if not data:
        return None
    rows = data.get("earningsCalendar")
    if not rows:
        return None

    df = pd.DataFrame(rows)
    df = df.rename(columns=EARNINGS_RENAME)
    df["fetched_at"] = _now_iso()
    return df


def fetch_insider_transactions(symbol, start_date, end_date):
    """Per-symbol insider filings. Endpoint caps at 100 transactions per call."""
    data = get_with_backoff(
        "stock/insider-transactions",
        {"symbol": symbol, "from": start_date, "to": end_date},
    )
    if not data:
        return None
    rows = data.get("data")
    if not rows:
        return None

    df = pd.DataFrame(rows)
    df = df.rename(columns=INSIDER_RENAME)
    # Endpoint echoes symbol at top level; ensure the column is always present.
    if "symbol" not in df.columns:
        df["symbol"] = data.get("symbol", symbol)
    df["fetched_at"] = _now_iso()
    if len(df) >= 100:
        print(f"    NOTE: {symbol} returned {len(df)} rows (endpoint cap is 100; "
              f"window may be truncated — narrow the date range to capture all).")
    return df


def main():
    parser = argparse.ArgumentParser(
        description="Finnhub earnings calendar + insider transactions pipeline"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch a wide history window instead of the recent incremental window.",
    )
    args = parser.parse_args()

    if not FINNHUB_API_KEY:
        print("CRITICAL ERROR: FINNHUB_API_KEY env variable is not set. Exiting.")
        return

    for path in DIRS.values():
        os.makedirs(path, exist_ok=True)

    mode = "backfill" if args.backfill else "incremental"
    today = datetime.datetime.utcnow()
    today_str = today.strftime("%Y%m%d")

    def fmt(dt):
        return dt.strftime("%Y-%m-%d")

    # ---- Earnings calendar (single market-wide request) ----
    earn_from = fmt(today - datetime.timedelta(days=EARNINGS_BACK[mode]))
    earn_to = fmt(today + datetime.timedelta(days=EARNINGS_FWD[mode]))
    print(f"[earnings_calendar] {mode}: {earn_from} -> {earn_to} (1 request)")
    earn_df = fetch_earnings_calendar(earn_from, earn_to)
    if earn_df is not None:
        out = write_partitioned(
            earn_df,
            DIRS["earnings_calendar"],
            f"earnings_calendar_{mode}_{today_str}.parquet",
        )
        print(f"  Saved earnings_calendar -> {out} ({len(earn_df):,} rows)")
    else:
        print("  Warning: no earnings calendar data returned.")

    # ---- IPO calendar (single market-wide request) ----
    ipo_from = fmt(today - datetime.timedelta(days=IPO_BACK[mode]))
    ipo_to = fmt(today + datetime.timedelta(days=IPO_FWD[mode]))
    print(f"\n[ipo_calendar] {mode}: {ipo_from} -> {ipo_to} (1 request)")
    ipo_df = fetch_ipo_calendar(ipo_from, ipo_to)
    if ipo_df is not None:
        out = write_partitioned(
            ipo_df,
            DIRS["ipo_calendar"],
            f"ipo_calendar_{mode}_{today_str}.parquet",
        )
        print(f"  Saved ipo_calendar -> {out} ({len(ipo_df):,} rows)")
    else:
        print("  Warning: no IPO calendar data returned.")

    # ---- Insider transactions (per-symbol) ----
    symbols = get_dji_symbols()
    ins_from = fmt(today - datetime.timedelta(days=INSIDER_BACK[mode]))
    ins_to = fmt(today)
    print(f"\n[insider_transactions] {mode}: {ins_from} -> {ins_to} "
          f"({len(symbols)} symbols)")

    ins_frames = []
    for i, symbol in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] {symbol}...")
        df = fetch_insider_transactions(symbol, ins_from, ins_to)
        if df is not None:
            ins_frames.append(df)

    if ins_frames:
        ins_df = pd.concat(ins_frames, ignore_index=True)
        out = write_partitioned(
            ins_df,
            DIRS["insider_transactions"],
            f"insider_transactions_{mode}_{today_str}.parquet",
        )
        print(f"  Saved insider_transactions -> {out} ({len(ins_df):,} rows)")
    else:
        print("  Warning: no insider transaction data returned.")

    print("\n--- PIPELINE RUN COMPLETE ---")


if __name__ == "__main__":
    main()
