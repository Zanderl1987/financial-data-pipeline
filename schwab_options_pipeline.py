#!/usr/bin/env python3
"""
Schwab Options Pipeline:
  Options chains with full greeks (delta, gamma, theta, vega, rho) for
  target symbols. The Schwab /chains endpoint returns richer data than
  Yahoo Finance — greeks are always present and reflect real market pricing.

  Fetches the nearest N expirations per symbol across calls and puts.
  For each expiration, captures all strikes within a configurable range
  of the current price.

CLI:
  python schwab_options_pipeline.py                         # default symbols
  python schwab_options_pipeline.py --symbols NVDA TSLA AAPL
  python schwab_options_pipeline.py --expirations 4         # weeks out to fetch

Output:
  storage/raw/schwab/options/schwab_options_{mode}_{YYYYMMDD}.parquet

Schema:
  symbol | put_call | expiration_date | days_to_expiration | strike |
  bid | ask | last | mark | volume | open_interest |
  delta | gamma | theta | vega | rho |
  implied_volatility | in_the_money | intrinsic_value | time_value |
  underlying_price | fetched_at
"""

import os
import time
import datetime
import argparse
import pandas as pd
import schwabdev
from dotenv import load_dotenv

load_dotenv()

API_KEY      = os.environ["SCHWAB_API_KEY"]
APP_SECRET   = os.environ["SCHWAB_APP_SECRET"]
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH   = os.environ.get("SCHWAB_TOKEN_PATH", "tokens.json")

OUTPUT_DIR = os.path.join("storage", "raw", "schwab", "options")

DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "JPM", "SPY", "QQQ",
]

MAX_RETRIES     = 3
BACKOFF_SECONDS = 30
REQUEST_INTERVAL = 0.5


def _flatten_contracts(contracts: dict, symbol: str, underlying_price: float) -> list[dict]:
    """
    Flatten one side (calls or puts) of the Schwab option_chain expDateMap.

    The nested structure is:
        { "YYYY-MM-DD:DTE": { "strike": [contract, ...], ... }, ... }
    """
    rows = []
    for exp_key, strikes in contracts.items():
        # exp_key format: "2024-03-15:20"  (date:days_to_expiration)
        parts = exp_key.split(":")
        exp_date = parts[0]
        dte = int(parts[1]) if len(parts) > 1 else None

        for strike_str, contract_list in strikes.items():
            for c in contract_list:
                rows.append({
                    "symbol":              symbol,
                    "put_call":            c.get("putCall"),
                    "expiration_date":     exp_date,
                    "days_to_expiration":  dte,
                    "strike":              float(strike_str),
                    "bid":                 c.get("bid"),
                    "ask":                 c.get("ask"),
                    "last":                c.get("last"),
                    "mark":                c.get("mark"),
                    "volume":              c.get("totalVolume"),
                    "open_interest":       c.get("openInterest"),
                    "delta":               c.get("delta"),
                    "gamma":               c.get("gamma"),
                    "theta":               c.get("theta"),
                    "vega":                c.get("vega"),
                    "rho":                 c.get("rho"),
                    "implied_volatility":  c.get("volatility"),
                    "in_the_money":        c.get("inTheMoney"),
                    "intrinsic_value":     c.get("intrinsicValue"),
                    "time_value":          c.get("timeValue"),
                    "underlying_price":    underlying_price,
                })
    return rows


def fetch_option_chain(
    client,
    symbol: str,
    weeks_out: int = 4,
) -> pd.DataFrame | None:
    """Fetch the full options chain for a symbol and return a flat DataFrame."""
    today    = datetime.date.today()
    from_dt  = today.strftime("%Y-%m-%d")
    to_dt    = (today + datetime.timedelta(weeks=weeks_out)).strftime("%Y-%m-%d")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.option_chain(
                symbol=symbol,
                contractType="ALL",
                strikeCount=40,        # 20 strikes each side of ATM
                includeUnderlyingQuote=True,
                fromDate=from_dt,
                toDate=to_dt,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") != "SUCCESS":
                    print(f"  {symbol}: API status={data.get('status')}")
                    return None

                underlying_price = (
                    data.get("underlyingPrice") or
                    (data.get("underlying") or {}).get("last", 0.0)
                )

                rows = []
                rows += _flatten_contracts(data.get("callExpDateMap", {}), symbol, underlying_price)
                rows += _flatten_contracts(data.get("putExpDateMap",  {}), symbol, underlying_price)

                if not rows:
                    return None

                df = pd.DataFrame(rows)
                df["fetched_at"] = datetime.datetime.utcnow().isoformat()
                return df

            if resp.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 rate limit for {symbol}. Backing off {wait}s.")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code} for {symbol}: {resp.text[:120]}")
                return None

        except Exception as e:
            print(f"  Error fetching {symbol} (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)

    return None


def main():
    parser = argparse.ArgumentParser(description="Schwab options chain pipeline with greeks")
    parser.add_argument(
        "--symbols", nargs="+", default=DEFAULT_SYMBOLS,
        help="Symbols to fetch (default: top liquid equity + index options)"
    )
    parser.add_argument(
        "--expirations", type=int, default=4,
        help="Number of weeks out to fetch (default: 4)"
    )
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    client = schwabdev.Client(
        app_key=API_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        tokens_file=TOKEN_PATH,
    )

    symbols  = [s.upper() for s in args.symbols]
    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    print(f"Fetching options chains for {len(symbols)} symbols "
          f"({args.expirations} weeks out)...")

    frames = []
    failed = []

    for i, symbol in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] {symbol}...")
        df = fetch_option_chain(client, symbol, weeks_out=args.expirations)
        if df is not None:
            frames.append(df)
            print(f"    {len(df):,} contracts")
        else:
            failed.append(symbol)
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        print("No data collected. Exiting.")
        return

    combined = pd.concat(frames, ignore_index=True)
    out_path = os.path.join(OUTPUT_DIR, f"schwab_options_incremental_{today_str}.parquet")
    combined.to_parquet(out_path, index=False, compression="snappy")

    print(f"\n--- COMPLETE ---")
    print(f"Saved {len(combined):,} contracts for {len(frames)} symbols → {out_path}")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}")

    sample = combined[combined["delta"].notna()][
        ["symbol", "put_call", "expiration_date", "strike",
         "mark", "delta", "implied_volatility", "open_interest"]
    ].head(10)
    print(sample.to_string(index=False))


if __name__ == "__main__":
    main()
