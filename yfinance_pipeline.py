#!/usr/bin/env python3
"""
Yahoo Finance market-history pipeline.

Deep daily OHLCV history for the market-level assets the event backtester
needs but no other pipeline covers with real depth: broad indices, commodity
futures (front-month continuous), FX pairs, and rate/credit ETFs.

  ^GSPC back to 1927, ^DJI to 1992, CL=F (WTI crude) to 2000, GC=F (gold)
  to 2000, EURUSD=X to 2003, TLT/HYG to fund inception.

No API key required (yfinance library).

CLI:
  python yfinance_pipeline.py                       # incremental (last 30 days)
  python yfinance_pipeline.py --backfill            # full available history
  python yfinance_pipeline.py --symbols "CL=F,GC=F" # override universe

Output:
  storage/raw/yfinance/year=YYYY/month=MM/market_history_{mode}_{date}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import yfinance as yf
from storage_utils import write_partitioned

BASE_DIR = "storage/raw/yfinance"
REQUEST_GAP = 0.5  # seconds between symbols — be polite, avoid throttling

# (yahoo symbol, readable name, asset_class)
UNIVERSE = [
    # --- equity indices ---
    ("^GSPC",     "S&P 500",                 "equity_index"),
    ("^DJI",      "Dow Jones Industrial",    "equity_index"),
    ("^IXIC",     "Nasdaq Composite",        "equity_index"),
    ("^RUT",      "Russell 2000",            "equity_index"),
    ("^VIX",      "CBOE VIX",                "volatility"),
    ("^N225",     "Nikkei 225",              "equity_index"),
    ("^STOXX50E", "Euro Stoxx 50",           "equity_index"),
    # --- commodity futures (continuous front month) ---
    ("CL=F",      "WTI Crude Oil",           "commodity"),
    ("BZ=F",      "Brent Crude Oil",         "commodity"),
    ("NG=F",      "Natural Gas",             "commodity"),
    ("GC=F",      "Gold",                    "commodity"),
    ("SI=F",      "Silver",                  "commodity"),
    ("HG=F",      "Copper",                  "commodity"),
    ("ZW=F",      "Wheat",                   "commodity"),
    ("ZC=F",      "Corn",                    "commodity"),
    # --- FX ---
    ("EURUSD=X",  "EUR/USD",                 "fx"),
    ("USDJPY=X",  "USD/JPY",                 "fx"),
    ("GBPUSD=X",  "GBP/USD",                 "fx"),
    ("DX-Y.NYB",  "US Dollar Index",         "fx"),
    # --- rates / credit ETFs (deep proxies for bonds) ---
    ("TLT",       "20+yr Treasury ETF",      "rates"),
    ("IEF",       "7-10yr Treasury ETF",     "rates"),
    ("HYG",       "High-Yield Corporate ETF", "credit"),
    ("LQD",       "Inv-Grade Corporate ETF", "credit"),
    # --- crypto (24/7 daily) ---
    ("BTC-USD",   "Bitcoin",                 "crypto"),
]


def fetch_symbol(symbol: str, name: str, asset_class: str, start: str) -> pd.DataFrame:
    df = yf.Ticker(symbol).history(start=start, auto_adjust=False, actions=False)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.strftime("%Y-%m-%d")
    df["symbol"] = symbol
    df["name"] = name
    df["asset_class"] = asset_class
    for col in ("open", "high", "low", "close", "adj_close", "volume"):
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    return df[["symbol", "name", "asset_class", "date",
               "open", "high", "low", "close", "adj_close", "volume"]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Yahoo Finance market history")
    parser.add_argument("--backfill", action="store_true", help="Full available history")
    parser.add_argument("--symbols", default="", help="Comma-separated Yahoo symbols (override universe)")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"
    start = "1900-01-01" if args.backfill else (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d")

    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        universe = [u for u in UNIVERSE if u[0].upper() in wanted]
        # allow ad-hoc symbols not in the curated universe
        known = {u[0].upper() for u in universe}
        universe += [(s, s, "adhoc") for s in wanted - known]
    else:
        universe = UNIVERSE

    os.makedirs(BASE_DIR, exist_ok=True)
    print(f"Yahoo Finance Pipeline  mode={mode}  symbols={len(universe)}\n")
    print("[market_history]")

    frames = []
    for symbol, name, asset_class in universe:
        try:
            df = fetch_symbol(symbol, name, asset_class, start)
            if df.empty:
                print(f"  {symbol}: no data")
            else:
                print(f"  {symbol}: {len(df):,} rows  ({df['date'].min()} -> {df['date'].max()})")
                frames.append(df)
        except Exception as exc:
            print(f"  {symbol}: ERROR — {exc}")
        time.sleep(REQUEST_GAP)

    if not frames:
        print("  No data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["fetched_at"] = now.isoformat()
    path = write_partitioned(combined, BASE_DIR, f"market_history_{mode}_{today_str}.parquet")
    print(f"  -> {path}  ({len(combined):,} rows, {combined['symbol'].nunique()} symbols)")

    print("\n--- YFINANCE PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
