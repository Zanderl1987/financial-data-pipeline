#!/usr/bin/env python3
"""
Schwab Quotes Pipeline:
  Daily snapshot of real-time quotes plus fundamental data (PE, EPS,
  dividend yield, 52-week range) for the S&P 500 (via IVV holdings) plus
  sector ETFs. Schwab has no daily quota (120 req/min hard cap only), so
  this pipeline is not scoped down to DJI-30 the way quota-gated sources are.

  Uses client.quotes() which batches up to 500 symbols in a single request,
  so the entire universe runs in 1-2 API calls.

  This complements price_history_pipeline.py (historical OHLCV) by adding
  intraday/market-close snapshot data and fundamental metrics not available
  from the candles endpoint.

CLI:
  python schwab_quotes_pipeline.py

Output:
  storage/raw/schwab/quotes/quotes_{YYYYMMDD}.parquet

Schema:
  symbol | description | exchange | last | open | high | low | close |
  volume | bid | ask | bid_size | ask_size | net_change | pct_change |
  mark | week_52_high | week_52_low | pe_ratio | eps | div_yield |
  div_amount | div_ex_date | div_pay_date | fetched_at
"""

import os
import datetime
import time
import pandas as pd
import schwabdev
from dotenv import load_dotenv

from symbol_universe import get_broad_universe
from storage_utils import write_partitioned

load_dotenv()

API_KEY      = os.environ["SCHWAB_API_KEY"]
APP_SECRET   = os.environ["SCHWAB_APP_SECRET"]
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH   = os.environ.get("SCHWAB_TOKEN_PATH", "tokens.db")

OUTPUT_DIR = os.path.join("storage", "raw", "schwab", "quotes")

# Sector ETFs to include alongside DJI components
SECTOR_ETFS = [
    "XLK", "XLF", "XLE", "XLV", "XLY",
    "XLI", "XLC", "XLRE", "XLP", "XLU", "XLB",
    "SPY", "QQQ", "IWM", "DIA",
]

MAX_RETRIES     = 3
BACKOFF_SECONDS = 30
BATCH_SIZE      = 500  # Schwab /quotes hard cap per request


def _safe_get(d: dict, *keys, default=None):
    """Nested dict lookup that returns default instead of raising."""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, {})
    return d if d != {} else default


def flatten_quote(symbol: str, data: dict) -> dict | None:
    """Flatten the nested Schwab quote response into a single row."""
    if not data:
        return None

    q   = data.get("quote", {})
    fun = data.get("fundamental", {})
    ref = data.get("reference", {})

    return {
        "symbol":       symbol,
        "description":  ref.get("description"),
        "exchange":     ref.get("exchangeName"),
        # Price snapshot
        "last":         q.get("lastPrice"),
        "open":         q.get("openPrice"),
        "high":         q.get("highPrice"),
        "low":          q.get("lowPrice"),
        "close":        q.get("closePrice"),
        "volume":       q.get("totalVolume"),
        # Bid / ask
        "bid":          q.get("bidPrice"),
        "ask":          q.get("askPrice"),
        "bid_size":     q.get("bidSize"),
        "ask_size":     q.get("askSize"),
        # Change
        "net_change":   q.get("netChange"),
        "pct_change":   q.get("netPercentChange"),
        "mark":         q.get("mark"),
        # 52-week range
        "week_52_high": q.get("52WeekHigh"),
        "week_52_low":  q.get("52WeekLow"),
        # Fundamentals
        "pe_ratio":     fun.get("peRatio"),
        "eps":          fun.get("eps"),
        "div_yield":    fun.get("divYield"),
        "div_amount":   fun.get("divAmount"),
        "div_ex_date":  fun.get("divExDate"),
        "div_pay_date": fun.get("divPayDate"),
        "fetched_at":   datetime.datetime.utcnow().isoformat(),
    }


def _fetch_batch(client, symbols: list[str]) -> pd.DataFrame:
    """Fetch quotes for a single batch (<=500 symbols, Schwab's per-request cap)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.quotes(symbols, fields="quote,fundamental,reference")
            if resp.status_code == 200:
                raw = resp.json()
                rows = [
                    flatten_quote(sym, raw.get(sym, {}))
                    for sym in symbols
                    if sym in raw
                ]
                rows = [r for r in rows if r is not None]
                return pd.DataFrame(rows)
            if resp.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 rate limit. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
                break
        except Exception as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)

    return pd.DataFrame()


def fetch_quotes(client, symbols: list[str]) -> pd.DataFrame:
    """Fetch quotes for the full symbol list, chunked to Schwab's 500-symbol batch cap."""
    batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    frames = []
    for i, batch in enumerate(batches, 1):
        if len(batches) > 1:
            print(f"  Batch {i}/{len(batches)} ({len(batch)} symbols)...")
        frames.append(_fetch_batch(client, batch))
        if i < len(batches):
            time.sleep(0.5)
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    client = schwabdev.Client(
        app_key=API_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        tokens_db=TOKEN_PATH,
    )

    all_symbols = get_broad_universe(extra=SECTOR_ETFS)
    print(f"Fetching quotes for {len(all_symbols)} symbols...")

    df = fetch_quotes(client, all_symbols)

    if df.empty:
        print("No data returned. Exiting.")
        return

    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    out_path  = write_partitioned(df, OUTPUT_DIR, f"quotes_{today_str}.parquet")

    print(f"\n--- COMPLETE ---")
    print(f"Saved {len(df)} rows -> {out_path}")
    print(df[["symbol", "last", "pct_change", "pe_ratio", "div_yield"]].to_string(index=False))


if __name__ == "__main__":
    main()
