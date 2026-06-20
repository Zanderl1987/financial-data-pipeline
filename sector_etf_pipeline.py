#!/usr/bin/env python3
"""
Sector ETF Pipeline (Schwab API):
  Daily OHLCV price history for the 11 SPDR sector ETFs and 4 broad index ETFs.
  Uses the same Schwab client and fetch pattern as price_history_pipeline.py.

CLI:
  python sector_etf_pipeline.py             # incremental (last 7 days)
  python sector_etf_pipeline.py --backfill  # full year of history

Output:
  storage/raw/sector_etfs/sector_etfs_{mode}_{YYYYMMDD}.parquet

Schema (same as prices table):
  symbol | sector | date | open | high | low | close | volume |
  pct_change | log_return | intraday_change | intraday_range | vwap | fetched_at
"""

import datetime
import os
import time
import argparse
import numpy as np
import pandas as pd
import schwabdev
from dotenv import load_dotenv

load_dotenv()

API_KEY       = os.environ["SCHWAB_API_KEY"]
APP_SECRET    = os.environ["SCHWAB_APP_SECRET"]
CALLBACK_URL  = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH    = os.environ.get("SCHWAB_TOKEN_PATH", "tokens.json")

OUTPUT_DIR = os.path.join("storage", "raw", "sector_etfs")

MAX_RETRIES      = 3
BACKOFF_SECONDS  = 60
REQUEST_INTERVAL = 0.5  # 120 req/min hard cap

# SPDR sector ETFs (one per GICS sector) + major broad-index ETFs
ETF_UNIVERSE: dict[str, str] = {
    # --- SPDR Sectors ---
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLE":  "Energy",
    "XLV":  "Health Care",
    "XLY":  "Consumer Discretionary",
    "XLI":  "Industrials",
    "XLC":  "Communication Services",
    "XLRE": "Real Estate",
    "XLP":  "Consumer Staples",
    "XLU":  "Utilities",
    "XLB":  "Materials",
    # --- Broad Indexes ---
    "SPY":  "S&P 500",
    "QQQ":  "Nasdaq 100",
    "IWM":  "Russell 2000",
    "DIA":  "Dow Jones",
}


def fetch_with_backoff(client, symbol: str, start_ms: int, end_ms: int):
    for attempt in range(1, MAX_RETRIES + 1):
        response = client.price_history(
            symbol=symbol,
            periodType="year",
            period=1,
            frequencyType="daily",
            frequency=1,
            startDate=start_ms,
            endDate=end_ms,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code == 429:
            wait = BACKOFF_SECONDS * attempt
            print(f"  429 rate limit hit for {symbol}. Backing off {wait}s "
                  f"(attempt {attempt}/{MAX_RETRIES}).")
            time.sleep(wait)
        else:
            print(f"  HTTP {response.status_code} for {symbol}: {response.text[:120]}")
            return None
    print(f"  Giving up on {symbol} after {MAX_RETRIES} attempts.")
    return None


def compute_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("date").reset_index(drop=True)
    df["pct_change"]      = df["close"].pct_change().round(6)
    df["log_return"]      = np.log(df["close"] / df["close"].shift(1)).round(6)
    df["intraday_change"] = (df["close"] - df["open"]).round(4)
    df["intraday_range"]  = (df["high"] - df["low"]).round(4)
    df["vwap"]            = ((df["high"] + df["low"] + df["close"]) / 3).round(4)
    return df


def fetch_etf(client, symbol: str, sector: str, start_ms: int, end_ms: int) -> pd.DataFrame | None:
    data = fetch_with_backoff(client, symbol, start_ms, end_ms)
    if not data or not data.get("candles"):
        return None

    df = pd.DataFrame(data["candles"])
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
    df["date"]     = df["datetime"].dt.date.astype(str)
    df["symbol"]   = symbol
    df["sector"]   = sector
    df = df[["symbol", "sector", "date", "open", "high", "low", "close", "volume"]]
    df = compute_derived_columns(df)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def main(backfill: bool = False):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    client = schwabdev.Client(
        app_key=API_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        tokens_file=TOKEN_PATH,
    )

    end_dt   = datetime.datetime.utcnow()
    start_dt = end_dt - datetime.timedelta(days=365 if backfill else 7)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)

    mode = "backfill" if backfill else "incremental"
    print(f"Mode: {mode.upper()} ({len(ETF_UNIVERSE)} ETFs, "
          f"{start_dt.strftime('%Y-%m-%d')} → {end_dt.strftime('%Y-%m-%d')})")

    frames = []
    failed = []

    for i, (symbol, sector) in enumerate(ETF_UNIVERSE.items(), 1):
        print(f"[{i}/{len(ETF_UNIVERSE)}] {symbol} ({sector})...")
        df = fetch_etf(client, symbol, sector, start_ms, end_ms)
        if df is not None:
            frames.append(df)
        else:
            failed.append(symbol)
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        print("No data collected. Exiting.")
        return

    combined  = pd.concat(frames, ignore_index=True)
    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    out_path  = os.path.join(OUTPUT_DIR, f"sector_etfs_{mode}_{today_str}.parquet")
    combined.to_parquet(out_path, index=False, compression="snappy")

    print(f"\n--- COMPLETE ---")
    print(f"Saved {len(combined):,} rows for {len(frames)} ETFs → {out_path}")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}")
    print(combined[["symbol", "sector", "date", "close", "pct_change"]].tail(15).to_string(index=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sector ETF price history pipeline")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch full 365-day history (use on first run). Default: last 7 days.",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
