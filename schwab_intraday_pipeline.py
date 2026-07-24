#!/usr/bin/env python3
"""
Schwab Intraday Pipeline:
  Minute-level OHLCV bars from the Schwab price_history endpoint.

  Schwab retention limits (approximate, empirically observed):
    1-minute bars  : ~48 calendar days
    5/10/15/30-min : ~9 months

  This enables intraday event studies (e.g. price reaction in the first
  30/60 minutes after an earnings release or filing) that close-to-close
  CARs cannot resolve. Run daily to accumulate history beyond Schwab's
  retention window.

CLI:
  python schwab_intraday_pipeline.py                     # last 5 days, 5-min bars
  python schwab_intraday_pipeline.py --freq 1 --days 2
  python schwab_intraday_pipeline.py --backfill          # max retention for freq
  python schwab_intraday_pipeline.py --symbols SPY QQQ --freq 1

Output:
  storage/raw/schwab/intraday/year=YYYY/month=MM/schwab_intraday_{freq}m_{mode}_{YYYYMMDD}.parquet

Schema:
  symbol | datetime | date | open | high | low | close | volume |
  freq_min | fetched_at
"""

import os
import time
import datetime
import argparse
import pandas as pd
import schwabdev
from dotenv import load_dotenv

from storage_utils import write_partitioned

load_dotenv()

API_KEY      = os.environ["SCHWAB_API_KEY"]
APP_SECRET   = os.environ["SCHWAB_APP_SECRET"]
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH   = os.environ.get("SCHWAB_TOKEN_PATH", "tokens.db")

OUTPUT_DIR = os.path.join("storage", "raw", "schwab", "intraday")

# Broad ETFs + the most event-active megacaps: enough for intraday event
# studies without blowing up row counts (390 one-min bars/day/symbol).
DEFAULT_SYMBOLS = [
    "SPY", "QQQ", "IWM", "DIA", "TLT", "GLD", "USO",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
]

VALID_FREQS = (1, 5, 10, 15, 30)
# Max lookback Schwab serves per bar size (calendar days, conservative)
MAX_DAYS = {1: 45, 5: 260, 10: 260, 15: 260, 30: 260}

MAX_RETRIES      = 3
BACKOFF_SECONDS  = 30
REQUEST_INTERVAL = 0.5   # 120 req/min hard cap


def fetch_with_backoff(client, symbol, freq, start_ms, end_ms):
    for attempt in range(1, MAX_RETRIES + 1):
        response = client.price_history(
            symbol=symbol,
            periodType="day",
            frequencyType="minute",
            frequency=freq,
            startDate=start_ms,
            endDate=end_ms,
            needExtendedHoursData=False,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code == 429:
            wait = BACKOFF_SECONDS * attempt
            print(f"  429 rate limit for {symbol}. Backing off {wait}s "
                  f"(attempt {attempt}/{MAX_RETRIES}).")
            time.sleep(wait)
        else:
            print(f"  HTTP {response.status_code} for {symbol}: {response.text[:120]}")
            return None
    print(f"  Giving up on {symbol} after {MAX_RETRIES} attempts.")
    return None


def fetch_symbol(client, symbol, freq, start_ms, end_ms):
    data = fetch_with_backoff(client, symbol, freq, start_ms, end_ms)
    if not data or not data.get("candles"):
        return None
    df = pd.DataFrame(data["candles"])
    # Schwab candle timestamps are epoch ms in US/Eastern market time
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
    df["date"] = df["datetime"].dt.date.astype(str)
    df["datetime"] = df["datetime"].astype(str)
    df["symbol"] = symbol
    df["freq_min"] = freq
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df[["symbol", "datetime", "date", "open", "high", "low", "close",
               "volume", "freq_min", "fetched_at"]]


def main(freq=5, days=5, backfill=False, symbols=None):
    if freq not in VALID_FREQS:
        raise SystemExit(f"--freq must be one of {VALID_FREQS}")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    client = schwabdev.Client(
        app_key=API_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        tokens_db=TOKEN_PATH,
    )

    if backfill:
        days = MAX_DAYS[freq]
    end_dt = datetime.datetime.utcnow()
    start_dt = end_dt - datetime.timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    symbols = [s.upper() for s in (symbols or DEFAULT_SYMBOLS)]
    mode = "backfill" if backfill else "incremental"
    print(f"Schwab Intraday Pipeline  freq={freq}m  days={days}  mode={mode}  "
          f"symbols={len(symbols)}")

    results, failed = [], []
    for i, symbol in enumerate(symbols, 1):
        df = fetch_symbol(client, symbol, freq, start_ms, end_ms)
        if df is not None:
            print(f"  [{i}/{len(symbols)}] {symbol}: {len(df):,} bars "
                  f"({df['date'].min()} -> {df['date'].max()})")
            results.append(df)
        else:
            failed.append(symbol)
        time.sleep(REQUEST_INTERVAL)

    if not results:
        print("No data collected. Exiting.")
        return

    combined = pd.concat(results, ignore_index=True)
    today = datetime.datetime.utcnow().strftime("%Y%m%d")
    filename = write_partitioned(
        combined, OUTPUT_DIR, f"schwab_intraday_{freq}m_{mode}_{today}.parquet")

    print(f"\n--- SCHWAB INTRADAY PIPELINE COMPLETE ---")
    print(f"Saved {len(combined):,} bars for {len(results)} symbols -> {filename}")
    if failed:
        print(f"Failed symbols ({len(failed)}): {', '.join(failed)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Schwab intraday minute-bar pipeline")
    parser.add_argument("--freq", type=int, default=5, choices=VALID_FREQS,
                        help="Bar size in minutes (default 5)")
    parser.add_argument("--days", type=int, default=5,
                        help="Calendar-day lookback (default 5)")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch Schwab's maximum retention for the chosen freq")
    parser.add_argument("--symbols", nargs="+",
                        help="Symbol override (default: broad ETFs + megacaps)")
    args = parser.parse_args()
    main(freq=args.freq, days=args.days, backfill=args.backfill,
         symbols=args.symbols)
