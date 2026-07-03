#!/usr/bin/env python3
"""
TradingView technical-rating snapshot pipeline.

Pulls TradingView's aggregate Technical Rating (the "Strong Buy / Buy /
Neutral / Sell / Strong Sell" gauge shown on every TV chart) for US stocks
and key ETFs via the free scanner endpoint.

  Recommend.All   — overall rating in [-1, 1] (MA + oscillator combined)
  Recommend.MA    — moving-average component
  Recommend.Other — oscillator component

TradingView only serves the CURRENT rating — there is no history endpoint.
Running this daily accumulates a rating history; for deep backtests use the
local replica of the same formula in analytics/technical.py (tv_rating),
which reproduces these values from OHLCV history.

No API key required.

CLI:
  python tradingview_pipeline.py              # top 500 stocks by mkt cap + ETF list
  python tradingview_pipeline.py --top 1500   # widen the stock universe
  python tradingview_pipeline.py --backfill   # same as default (no history exists)

Output:
  storage/raw/tradingview/year=YYYY/month=MM/tv_ratings_{date}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR = "storage/raw/tradingview"
SCAN_URL = "https://scanner.tradingview.com/america/scan"
PAGE_SIZE = 500
REQUEST_GAP = 1.0

COLUMNS = [
    "name", "description", "close", "change", "volume", "market_cap_basic",
    "sector", "industry",
    "Recommend.All", "Recommend.MA", "Recommend.Other",
    "RSI", "ADX", "CCI20", "Stoch.K", "Stoch.D", "W.R", "Mom", "AO",
    "MACD.macd", "MACD.signal",
    "SMA20", "SMA50", "SMA200", "EMA20", "EMA50", "EMA200",
]

ETFS = ["SPY", "QQQ", "IWM", "DIA", "TLT", "HYG", "GLD", "USO", "XLE", "XLF",
        "XLK", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC", "SMH"]

HEADERS = {"Content-Type": "application/json",
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def rating_label(x) -> str:
    """TradingView's published bucketing of the [-1, 1] score."""
    if x is None or pd.isna(x):
        return "unknown"
    if x >= 0.5:
        return "strong_buy"
    if x >= 0.1:
        return "buy"
    if x > -0.1:
        return "neutral"
    if x > -0.5:
        return "sell"
    return "strong_sell"


def scan(payload: dict) -> pd.DataFrame:
    resp = requests.post(SCAN_URL, json=payload, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([r["d"] for r in rows], columns=COLUMNS)
    df.insert(0, "tv_symbol", [r["s"] for r in rows])
    return df


def fetch_stocks(top_n: int) -> pd.DataFrame:
    frames = []
    for offset in range(0, top_n, PAGE_SIZE):
        payload = {
            "columns": COLUMNS,
            "filter": [
                {"left": "type", "operation": "equal", "right": "stock"},
                {"left": "subtype", "operation": "in_range", "right": ["common", ""]},
                {"left": "exchange", "operation": "in_range",
                 "right": ["NASDAQ", "NYSE", "AMEX"]},
            ],
            "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
            "range": [offset, min(offset + PAGE_SIZE, top_n)],
        }
        df = scan(payload)
        if df.empty:
            break
        frames.append(df)
        print(f"  stocks {offset}-{offset + len(df)}: {len(df)} rows")
        time.sleep(REQUEST_GAP)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_etfs() -> pd.DataFrame:
    tickers = [f"AMEX:{s}" for s in ETFS] + [f"NASDAQ:{s}" for s in ETFS]
    payload = {"symbols": {"tickers": tickers, "query": {"types": []}},
               "columns": COLUMNS}
    df = scan(payload)
    print(f"  etfs: {len(df)} rows")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="TradingView technical-rating snapshot")
    parser.add_argument("--top", type=int, default=500, help="Top N US stocks by market cap")
    parser.add_argument("--backfill", action="store_true",
                        help="No-op alias (TradingView serves current ratings only)")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    os.makedirs(BASE_DIR, exist_ok=True)

    print(f"TradingView Ratings Pipeline  date={today}  top={args.top}\n")
    print("[tv_ratings]")

    frames = [fetch_stocks(args.top), fetch_etfs()]
    frames = [f for f in frames if not f.empty]
    if not frames:
        print("  No data retrieved")
        return

    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["tv_symbol"])
    df["symbol"] = df["name"]
    df = df.drop(columns=["name"])
    df = df.rename(columns={
        "description": "company",
        "Recommend.All": "rating_all", "Recommend.MA": "rating_ma",
        "Recommend.Other": "rating_osc",
        "RSI": "rsi", "ADX": "adx", "CCI20": "cci20",
        "Stoch.K": "stoch_k", "Stoch.D": "stoch_d", "W.R": "willr",
        "Mom": "momentum", "AO": "ao",
        "MACD.macd": "macd", "MACD.signal": "macd_signal",
        "SMA20": "sma20", "SMA50": "sma50", "SMA200": "sma200",
        "EMA20": "ema20", "EMA50": "ema50", "EMA200": "ema200",
        "market_cap_basic": "market_cap",
    })
    df["rating_label"] = df["rating_all"].map(rating_label)
    df["date"] = today
    df["fetched_at"] = now.isoformat()

    path = write_partitioned(df, BASE_DIR, f"tv_ratings_{now.strftime('%Y%m%d')}.parquet")
    print(f"  -> {path}  ({len(df):,} rows)")
    print(f"  rating mix: {df['rating_label'].value_counts().to_dict()}")

    print("\n--- TRADINGVIEW PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
