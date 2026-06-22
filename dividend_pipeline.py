#!/usr/bin/env python3
"""
Dividend Pipeline (Finnhub /stock/dividend2):
  Per-symbol cash dividend history including ex-date, pay date, record date,
  declaration date, amount, adjusted amount, frequency, and currency.

Reuses the shared RateLimiter + get_with_backoff fetch layer from
finnhub_pipeline.py to stay under the 60 req/min free-tier limit.

CLI:
  python dividend_pipeline.py             # incremental (last 2 years)
  python dividend_pipeline.py --backfill  # full available history

Output:
  storage/raw/finnhub/dividends/dividends_{mode}_{YYYYMMDD}.parquet

Schema:
  symbol | ex_date | pay_date | record_date | declaration_date |
  amount | adj_amount | frequency | currency | fetched_at
"""

import os
import datetime
import argparse
import pandas as pd

from storage_utils import write_partitioned
from finnhub_pipeline import (
    FINNHUB_API_KEY,
    get_with_backoff,
    get_dji_symbols,
)

OUTPUT_DIR = os.path.join("storage", "raw", "finnhub", "dividends")

LOOKBACK_DAYS = {"incremental": 730, "backfill": 3650}  # 2yr / 10yr

RENAME = {
    "date":            "ex_date",
    "payDate":         "pay_date",
    "recordDate":      "record_date",
    "declarationDate": "declaration_date",
    "adjustedAmount":  "adj_amount",
    "freq":            "frequency",
}


def fetch_dividends(symbol: str, start_date: str, end_date: str) -> pd.DataFrame | None:
    data = get_with_backoff(
        "stock/dividend2",
        {"symbol": symbol, "from": start_date, "to": end_date},
    )
    if not data:
        return None
    rows = data.get("data")
    if not rows:
        return None

    df = pd.DataFrame(rows)
    df = df.rename(columns=RENAME)
    if "symbol" not in df.columns:
        df["symbol"] = data.get("symbol", symbol)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()

    # Normalise date columns to strings
    for col in ("ex_date", "pay_date", "record_date", "declaration_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")

    return df


def main():
    parser = argparse.ArgumentParser(description="Finnhub dividend history pipeline")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch 10 years of history instead of the default 2-year window.",
    )
    args = parser.parse_args()

    if not FINNHUB_API_KEY:
        print("CRITICAL ERROR: FINNHUB_API_KEY env variable is not set. Exiting.")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    mode = "backfill" if args.backfill else "incremental"
    today = datetime.datetime.utcnow()
    today_str = today.strftime("%Y%m%d")
    start_str = (today - datetime.timedelta(days=LOOKBACK_DAYS[mode])).strftime("%Y-%m-%d")
    end_str = today.strftime("%Y-%m-%d")

    symbols = get_dji_symbols()
    print(f"[dividends] {mode}: {start_str} -> {end_str} ({len(symbols)} symbols)")

    frames = []
    for i, symbol in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] {symbol}...")
        df = fetch_dividends(symbol, start_str, end_str)
        if df is not None:
            frames.append(df)

    if not frames:
        print("  Warning: no dividend data returned.")
        return

    out_df = pd.concat(frames, ignore_index=True)
    out_path = write_partitioned(out_df, OUTPUT_DIR, f"dividends_{mode}_{today_str}.parquet")
    print(f"  Saved dividends -> {out_path} ({len(out_df):,} rows, "
          f"{out_df['symbol'].nunique()} symbols)")

    print("\n--- PIPELINE RUN COMPLETE ---")


if __name__ == "__main__":
    main()
