#!/usr/bin/env python3
"""
Tiingo Pipeline.

Tiingo provides clean, corporate-action-adjusted EOD prices and ticker-tagged
financial news. Unlike Schwab, it works without OAuth and is always available.

Data fetched:
  - EOD prices — adjusted open/high/low/close/volume, splits, dividends,
    split-adjusted and dividend-adjusted close, for our standard watchlist
  - News — articles tagged with tickers, with source, published timestamp,
    and full description; useful as a supplementary news sentiment feed

Requires: TIINGO_API_KEY in .env

CLI:
  python tiingo_pipeline.py             # incremental (last 90 days)
  python tiingo_pipeline.py --backfill  # full available history (up to 30+ years)
  python tiingo_pipeline.py --symbols AAPL,MSFT,NVDA  # override symbol list

Outputs:
  storage/raw/tiingo/prices/tiingo_prices_{mode}_{YYYYMMDD}.parquet
  storage/raw/tiingo/news/tiingo_news_{mode}_{YYYYMMDD}.parquet
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

TIINGO_API_KEY = os.environ.get("TIINGO_API_KEY", "")
BASE_URL = "https://api.tiingo.com"
BASE_DIR = os.path.join("storage", "raw", "tiingo")
REQUEST_INTERVAL = 0.3   # Tiingo free: 50 symbols/hour — generous
MAX_RETRIES = 3

# Standard watchlist — DJI 30 + major ETFs + high-interest tech
DEFAULT_SYMBOLS = [
    # DJI 30
    "AAPL", "AMGN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "DOW",
    "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM", "KO", "MCD", "MMM",
    "MRK", "MSFT", "NKE", "PG", "TRV", "UNH", "V", "VZ", "WBA", "WMT",
    # High-interest large-cap tech
    "NVDA", "META", "GOOGL", "AMZN", "TSLA", "PLTR", "AMD", "ORCL", "NFLX",
    # Broad market ETFs
    "SPY", "QQQ", "IWM", "DIA",
    # Sector ETFs
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "XLC", "XLRE", "XLP", "XLU", "XLB",
    # Fixed income
    "TLT", "IEF", "SHY", "HYG", "LQD",
    # Commodity ETFs
    "GLD", "SLV", "USO", "UNG",
]


def make_headers():
    return {"Authorization": f"Token {TIINGO_API_KEY}", "Content-Type": "application/json"}


def get_with_backoff(url, params=None):
    headers = make_headers()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  429 rate limit. Waiting {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            elif resp.status_code == 404:
                return None  # Symbol not found — not an error worth retrying
            else:
                print(f"  HTTP {resp.status_code} for {url}: {resp.text[:120]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(15 * attempt)
    return None


def fetch_daily_prices(symbol, start_date, end_date):
    """Fetch EOD price history for one symbol. Returns list of dicts or None."""
    url = f"{BASE_URL}/tiingo/daily/{symbol}/prices"
    params = {
        "startDate": start_date,
        "endDate": end_date,
        "resampleFreq": "daily",
        "token": TIINGO_API_KEY,
    }
    data = get_with_backoff(url, params)
    if not data or not isinstance(data, list):
        return None
    rows = []
    for obs in data:
        rows.append({
            "symbol":        symbol,
            "date":          obs.get("date", "")[:10],
            "open":          obs.get("open"),
            "high":          obs.get("high"),
            "low":           obs.get("low"),
            "close":         obs.get("close"),
            "volume":        obs.get("volume"),
            "adj_open":      obs.get("adjOpen"),
            "adj_high":      obs.get("adjHigh"),
            "adj_low":       obs.get("adjLow"),
            "adj_close":     obs.get("adjClose"),
            "adj_volume":    obs.get("adjVolume"),
            "div_cash":      obs.get("divCash"),       # cash dividend on ex-date
            "split_factor":  obs.get("splitFactor"),   # split ratio (e.g. 4.0 for 4:1)
        })
    return rows


def fetch_news(symbols, start_date, end_date, limit=1000):
    """
    Fetch news articles for a list of tickers. Tiingo returns articles tagged
    with any of the requested symbols. Each article appears once even if tagged
    with multiple tickers in the list.
    """
    url = f"{BASE_URL}/tiingo/news"
    params = {
        "tickers": ",".join(symbols),
        "startDate": start_date,
        "endDate": end_date,
        "limit": limit,
        "token": TIINGO_API_KEY,
    }
    data = get_with_backoff(url, params)
    if not data or not isinstance(data, list):
        return pd.DataFrame()

    rows = []
    for article in data:
        tickers = article.get("tickers", [])
        rows.append({
            "article_id":    article.get("id"),
            "date":          article.get("publishedDate", "")[:10],
            "published_at":  article.get("publishedDate"),
            "source":        article.get("source"),
            "url":           article.get("url"),
            "title":         article.get("title"),
            "description":   article.get("description"),
            "tickers":       ",".join(tickers) if tickers else "",
            "n_tickers":     len(tickers),
        })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="Tiingo EOD prices + news pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full available history")
    parser.add_argument("--symbols", default="",
                        help="Comma-separated symbol list (default: standard watchlist)")
    args = parser.parse_args()

    if not TIINGO_API_KEY:
        print("CRITICAL ERROR: TIINGO_API_KEY not set in .env. Exiting.")
        return

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    today = now.strftime("%Y-%m-%d")
    mode = "backfill" if args.backfill else "incremental"

    if args.backfill:
        start_date = "1990-01-01"
        news_start = (now - datetime.timedelta(days=730)).strftime("%Y-%m-%d")
        print(f"Mode: BACKFILL  symbols={len(symbols)}  prices_from={start_date}  news_from={news_start}")
    else:
        start_date = (now - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
        news_start = start_date
        print(f"Mode: INCREMENTAL  symbols={len(symbols)}  from={start_date}")

    prices_dir = os.path.join(BASE_DIR, "prices")
    news_dir = os.path.join(BASE_DIR, "news")
    for d in [prices_dir, news_dir]:
        os.makedirs(d, exist_ok=True)

    # ---- EOD Prices ----
    print(f"\n[tiingo_prices] Fetching EOD prices for {len(symbols)} symbols...")
    price_frames = []
    failed = []

    for i, sym in enumerate(symbols, 1):
        data = fetch_daily_prices(sym, start_date, today)
        if data:
            price_frames.append(pd.DataFrame(data))
            print(f"  [{i}/{len(symbols)}] {sym}: {len(data)} days")
        else:
            failed.append(sym)
            print(f"  [{i}/{len(symbols)}] {sym}: no data")
        time.sleep(REQUEST_INTERVAL)

    if price_frames:
        prices_df = pd.concat(price_frames, ignore_index=True)
        # Standardize types
        for col in ["open", "high", "low", "close", "adj_close", "adj_open",
                    "adj_high", "adj_low", "div_cash", "split_factor"]:
            if col in prices_df.columns:
                prices_df[col] = pd.to_numeric(prices_df[col], errors="coerce")
        prices_df["volume"] = pd.to_numeric(prices_df.get("volume", 0), errors="coerce")
        prices_df["adj_volume"] = pd.to_numeric(prices_df.get("adj_volume", 0), errors="coerce")
        prices_df["fetched_at"] = now.isoformat()
        path = write_partitioned(
            prices_df, prices_dir,
            f"tiingo_prices_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(prices_df):,} rows, {prices_df['symbol'].nunique()} symbols)")
    else:
        print("  No price data returned.")

    if failed:
        print(f"  Failed/not found ({len(failed)}): {', '.join(failed)}")

    # ---- News ----
    # Tiingo news API accepts comma-separated tickers; batch into 50-ticker groups
    print(f"\n[tiingo_news] Fetching news articles...")
    news_frames = []
    batch_size = 50
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        df = fetch_news(batch, news_start, today, limit=1000)
        if not df.empty:
            news_frames.append(df)
        time.sleep(REQUEST_INTERVAL)

    if news_frames:
        news_df = pd.concat(news_frames, ignore_index=True)
        # Deduplicate by article_id
        news_df = news_df.drop_duplicates(subset=["article_id"]).sort_values("published_at")
        news_df["fetched_at"] = now.isoformat()
        path = write_partitioned(
            news_df, news_dir,
            f"tiingo_news_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(news_df):,} articles)")
    else:
        print("  No news articles returned.")

    print("\n--- TIINGO PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
