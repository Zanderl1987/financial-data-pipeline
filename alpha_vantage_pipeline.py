#!/usr/bin/env python3
"""
Alpha Vantage Pipeline — Technical Indicators & Forex Rates.

Alpha Vantage provides pre-computed technical indicators and forex exchange
rates. These are not available anywhere else in the pipeline, making this
a unique data source despite the tight rate limit.

Free tier: 25 API calls/day (300 calls/day if upgraded to premium).
This pipeline is designed to run efficiently within the free limit.

Data fetched:
  - Technical indicators — RSI, MACD, SMA (20/50/200), EMA, Bollinger Bands,
    ADX, ATR, OBV, Stochastic, CCI, ROC, Williams %R for our core symbols
  - Forex daily rates — major currency pairs (EURUSD, GBPUSD, JPYUSD,
    CADUSD, AUDUSD, CHFUSD, CNYUSD, MXNUSD, INRUSD, BRLUSD)

Rate-limit strategy: indicators are the highest-value unique data; we fetch
all forex pairs first (10 calls), then fill remaining budget with indicators.
On backfill, full history is returned per call (same cost as incremental).

Requires: ALPHA_VANTAGE_API_KEY in .env

CLI:
  python alpha_vantage_pipeline.py             # all forex + indicators for core symbols
  python alpha_vantage_pipeline.py --backfill  # same (AV returns full history per call)
  python alpha_vantage_pipeline.py --forex-only
  python alpha_vantage_pipeline.py --indicators-only

Outputs:
  storage/raw/alpha_vantage/forex/alpha_vantage_forex_{mode}_{YYYYMMDD}.parquet
  storage/raw/alpha_vantage/technical/alpha_vantage_technical_{mode}_{YYYYMMDD}.parquet
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

AV_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
BASE_URL = "https://www.alphavantage.co/query"
BASE_DIR = os.path.join("storage", "raw", "alpha_vantage")
REQUEST_INTERVAL = 12.5  # 25 calls/day = ~1 per 52 min; use burst of 5/min then wait
MAX_RETRIES = 3

# Core symbols for technical indicators (keep small to fit daily call budget)
INDICATOR_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "JPM", "GLD", "TLT"]

# Major forex pairs — from_currency / to_currency
FOREX_PAIRS = [
    ("EUR", "USD"), ("GBP", "USD"), ("JPY", "USD"),
    ("CAD", "USD"), ("AUD", "USD"), ("CHF", "USD"),
    ("CNY", "USD"), ("MXN", "USD"), ("INR", "USD"),
    ("BRL", "USD"),
]

# Technical indicators to fetch per symbol
# Each is one API call. Ordered by uniqueness / value.
INDICATORS = [
    # Momentum
    ("RSI",    {"interval": "daily", "time_period": 14,  "series_type": "close"}),
    ("MACD",   {"interval": "daily", "fastperiod": 12, "slowperiod": 26, "signalperiod": 9, "series_type": "close"}),
    ("STOCH",  {"interval": "daily", "fastkperiod": 5, "slowkperiod": 3, "slowdperiod": 3}),
    ("CCI",    {"interval": "daily", "time_period": 20}),
    ("WILLR",  {"interval": "daily", "time_period": 14}),
    ("ROC",    {"interval": "daily", "time_period": 10, "series_type": "close"}),
    # Trend
    ("ADX",    {"interval": "daily", "time_period": 14}),
    ("AROON",  {"interval": "daily", "time_period": 25}),
    ("SAR",    {"interval": "daily", "acceleration": 0.02, "maximum": 0.2}),
    # Moving averages
    ("SMA",    {"interval": "daily", "time_period": 20,  "series_type": "close"}),
    ("SMA",    {"interval": "daily", "time_period": 50,  "series_type": "close"}),
    ("SMA",    {"interval": "daily", "time_period": 200, "series_type": "close"}),
    ("EMA",    {"interval": "daily", "time_period": 20,  "series_type": "close"}),
    ("EMA",    {"interval": "daily", "time_period": 50,  "series_type": "close"}),
    # Volatility
    ("BBANDS", {"interval": "daily", "time_period": 20,  "series_type": "close", "nbdevup": 2, "nbdevdn": 2}),
    ("ATR",    {"interval": "daily", "time_period": 14}),
    # Volume
    ("OBV",    {"interval": "daily", "series_type": "close"}),
    ("AD",     {"interval": "daily"}),
]


def get_with_backoff(params):
    params["apikey"] = AV_API_KEY
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                # Alpha Vantage returns 200 even for errors; check for note/info keys
                if "Note" in data:
                    # Rate limit message
                    print(f"  AV rate limit note: {data['Note'][:80]}")
                    time.sleep(60 * attempt)
                    continue
                if "Information" in data:
                    print(f"  AV info: {data['Information'][:80]}")
                    return None
                if "Error Message" in data:
                    print(f"  AV error: {data['Error Message'][:80]}")
                    return None
                return data
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(15 * attempt)
    return None


def fetch_forex_daily(from_currency, to_currency, outputsize="full"):
    """Fetch daily forex rates for a currency pair."""
    pair = f"{from_currency}/{to_currency}"
    data = get_with_backoff({
        "function": "FX_DAILY",
        "from_symbol": from_currency,
        "to_symbol": to_currency,
        "outputsize": outputsize,
    })
    if not data:
        return None

    ts_key = "Time Series FX (Daily)"
    time_series = data.get(ts_key, {})
    if not time_series:
        return None

    rows = []
    for date_str, values in time_series.items():
        rows.append({
            "pair":      pair,
            "from_ccy":  from_currency,
            "to_ccy":    to_currency,
            "date":      date_str,
            "open":      float(values.get("1. open", 0) or 0),
            "high":      float(values.get("2. high", 0) or 0),
            "low":       float(values.get("3. low", 0) or 0),
            "close":     float(values.get("4. close", 0) or 0),
        })
    return rows


def fetch_indicator(symbol, function, extra_params, outputsize="full"):
    """Fetch a single technical indicator for one symbol."""
    params = {
        "function": function,
        "symbol": symbol,
        "outputsize": outputsize,
        **extra_params,
    }
    data = get_with_backoff(params)
    if not data:
        return None

    # Find the time series key (varies by indicator)
    ts_key = next((k for k in data if "Technical Analysis" in k), None)
    if not ts_key:
        return None

    time_series = data[ts_key]
    # Determine a label that includes the period if applicable
    period = extra_params.get("time_period", "")
    label = f"{function}{period}" if period else function

    rows = []
    for date_str, values in time_series.items():
        row = {
            "symbol":    symbol,
            "date":      date_str,
            "indicator": label,
        }
        for k, v in values.items():
            col = k.lower().replace(" ", "_").replace(".", "").strip("_")
            try:
                row[col] = float(v)
            except (ValueError, TypeError):
                row[col] = v
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description="Alpha Vantage technical indicators + forex pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full history (AV returns full history per call regardless)")
    parser.add_argument("--forex-only", action="store_true",
                        help="Only fetch forex rates")
    parser.add_argument("--indicators-only", action="store_true",
                        help="Only fetch technical indicators")
    args = parser.parse_args()

    if not AV_API_KEY:
        print("CRITICAL ERROR: ALPHA_VANTAGE_API_KEY not set in .env. Exiting.")
        return

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"
    outputsize = "full"  # Always full — same API cost, more data

    print(f"Alpha Vantage Pipeline  mode={mode}")
    print(f"Rate limit: 25 calls/day free tier. Pacing at {REQUEST_INTERVAL}s between calls.\n")

    forex_dir = os.path.join(BASE_DIR, "forex")
    technical_dir = os.path.join(BASE_DIR, "technical")
    for d in [forex_dir, technical_dir]:
        os.makedirs(d, exist_ok=True)

    calls_used = 0

    # ---- Forex ----
    if not args.indicators_only:
        print(f"[alpha_vantage_forex] Fetching {len(FOREX_PAIRS)} currency pairs...")
        forex_rows = []
        for from_ccy, to_ccy in FOREX_PAIRS:
            rows = fetch_forex_daily(from_ccy, to_ccy, outputsize)
            if rows:
                forex_rows.extend(rows)
                print(f"  {from_ccy}/{to_ccy}: {len(rows)} daily bars")
            else:
                print(f"  {from_ccy}/{to_ccy}: no data")
            calls_used += 1
            time.sleep(REQUEST_INTERVAL)

        if forex_rows:
            forex_df = pd.DataFrame(forex_rows)
            forex_df["fetched_at"] = now.isoformat()
            path = write_partitioned(
                forex_df, forex_dir,
                f"alpha_vantage_forex_{mode}_{today_str}.parquet",
            )
            print(f"  -> {path}  ({len(forex_df):,} rows, {forex_df['pair'].nunique()} pairs)")
        else:
            print("  No forex data returned.")

    # ---- Technical Indicators ----
    if not args.forex_only:
        print(f"\n[alpha_vantage_technical] Fetching technical indicators "
              f"({len(INDICATOR_SYMBOLS)} symbols x {len(INDICATORS)} indicators)...")
        print(f"  WARNING: {len(INDICATOR_SYMBOLS) * len(INDICATORS)} calls needed; "
              f"free tier allows 25/day. Fetching in priority order, stopping at limit.")

        technical_rows = []
        call_count = 0
        DAILY_LIMIT = 24  # leave one call for safety

        outer_break = False
        for sym in INDICATOR_SYMBOLS:
            if outer_break:
                break
            for function, extra_params in INDICATORS:
                if call_count >= DAILY_LIMIT - calls_used:
                    print(f"  Reached daily call budget ({DAILY_LIMIT}). "
                          f"Run again tomorrow to fetch remaining indicators.")
                    outer_break = True
                    break

                period = extra_params.get("time_period", "")
                label = f"{function}{period}" if period else function
                rows = fetch_indicator(sym, function, extra_params, outputsize)
                if rows:
                    technical_rows.extend(rows)
                else:
                    print(f"  {sym} {label}: no data")
                call_count += 1
                time.sleep(REQUEST_INTERVAL)

        if technical_rows:
            tech_df = pd.DataFrame(technical_rows)
            tech_df["fetched_at"] = now.isoformat()
            # Numeric coercion for all value columns
            for col in tech_df.columns:
                if col not in ("symbol", "date", "indicator", "fetched_at"):
                    tech_df[col] = pd.to_numeric(tech_df[col], errors="coerce")
            path = write_partitioned(
                tech_df, technical_dir,
                f"alpha_vantage_technical_{mode}_{today_str}.parquet",
            )
            print(f"  -> {path}  ({len(tech_df):,} rows, "
                  f"{tech_df['symbol'].nunique()} symbols, "
                  f"{tech_df['indicator'].nunique()} indicators)")
        else:
            print("  No technical indicator data returned.")

    print(f"\n  Total API calls used: {calls_used + call_count if not args.forex_only else calls_used}")
    print("\n--- ALPHA VANTAGE PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
