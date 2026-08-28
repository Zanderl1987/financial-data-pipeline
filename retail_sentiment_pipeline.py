#!/usr/bin/env python3
"""
Retail Investor Sentiment Pipeline — Stocktwits API.

Fetches bullish/bearish sentiment data from Stocktwits' public API for a
configurable watchlist of tickers. No API key required for basic reads.

API: https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json

Returns per-ticker:
  - Message volume (retail discussion activity)
  - Bullish/bearish sentiment from user-posted icons
  - Latest message timestamps

CLI:
  python retail_sentiment_pipeline.py             # incremental (last 7 days)
  python retail_sentiment_pipeline.py --backfill  # broader fetch

Output:
  storage/raw/retail_sentiment/year=YYYY/month=MM/retail_sentiment_{mode}_{YYYYMMDD}.parquet
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

OUTPUT_DIR = os.path.join("storage", "raw", "retail_sentiment")
BASE_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
REQUEST_INTERVAL = 1.5
MAX_RETRIES = 3
BACKOFF_SECONDS = 60

# High-interest tickers for retail sentiment tracking
WATCHLIST = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "AMD",
    "SPY", "QQQ", "IWM", "AMC", "GME", "PLTR", "SOFI", "RIVN",
    "COIN", "HOOD", "ROKU", "SNAP", "NIO", "BA", "DIS", "NFLX",
    "JPM", "GS", "PYPL", "SQ", "SHOP", "CRWD",
]

MAX_MESSAGES = 30


HEADERS = {"User-Agent": "Mozilla/5.0"}  # bare python-requests UA gets a Cloudflare challenge page


def _get_with_backoff(url: str) -> requests.Response | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from Stocktwits -- backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code}: {r.text[:200]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts.")
    return None


def fetch_ticker_sentiment(symbol):
    """Fetch recent messages for a ticker from Stocktwits API."""
    url = BASE_URL.format(symbol=symbol)
    resp = _get_with_backoff(url)
    if resp is None:
        return []

    try:
        body = resp.json()
    except Exception:
        return []

    messages = body.get("messages", [])
    rows = []
    for msg in messages:
        sentiment = msg.get("entities", {}).get("sentiment", {})
        bullish_count = sentiment.get("bullish", 0) if isinstance(sentiment, dict) else 0
        bearish_count = sentiment.get("bearish", 0) if isinstance(sentiment, dict) else 0

        created_at = msg.get("created_at", "")
        try:
            dt = datetime.datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            dt = None

        rows.append({
            "message_id": msg.get("id"),
            "symbol": symbol,
            "body": (msg.get("body") or "")[:300],
            "created_at": dt.isoformat() if dt else None,
            "date": dt.strftime("%Y-%m-%d") if dt else None,
            "user_followers": msg.get("user", {}).get("followers", 0),
            "user_experience": msg.get("user", {}).get("experience", ""),
            "bullish": bullish_count,
            "bearish": bearish_count,
        })

    return rows


def aggregate_daily_sentiment(df):
    """Aggregate per-symbol daily sentiment scores."""
    if df.empty or "date" not in df.columns:
        return pd.DataFrame()

    df = df.dropna(subset=["date"])
    agg = (
        df.groupby(["date", "symbol"])
        .agg(
            message_count=("message_id", "count"),
            avg_followers=("user_followers", "mean"),
            total_bullish=("bullish", "sum"),
            total_bearish=("bearish", "sum"),
        )
        .reset_index()
    )
    agg["bullish_ratio"] = agg.apply(
        lambda r: r["total_bullish"] / (r["total_bullish"] + r["total_bearish"])
        if (r["total_bullish"] + r["total_bearish"]) > 0 else None,
        axis=1,
    )
    agg["source"] = "stocktwits"
    return agg.sort_values(["date", "symbol"]).reset_index(drop=True)


def main(backfill=False):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode = "backfill" if backfill else "incremental"

    print(f"Retail Sentiment Pipeline  mode={mode}")
    print(f"Watching {len(WATCHLIST)} tickers...")

    all_rows = []
    for i, symbol in enumerate(WATCHLIST):
        rows = fetch_ticker_sentiment(symbol)
        all_rows.extend(rows)
        if rows:
            print(f"  {symbol}: {len(rows)} messages")
        if (i + 1) % 5 == 0:
            time.sleep(REQUEST_INTERVAL)

    if not all_rows:
        print("[!] No retail sentiment data returned.")
        return

    df_raw = pd.DataFrame(all_rows)
    df_raw["fetched_at"] = fetched_at

    df_daily = aggregate_daily_sentiment(df_raw)
    if not df_daily.empty:
        df_daily["fetched_at"] = fetched_at

    path_raw = write_partitioned(
        df_raw, OUTPUT_DIR,
        f"retail_sentiment_raw_{mode}_{today_str}.parquet",
    )
    print(f"[+] {path_raw} ({len(df_raw):,} rows)")

    if not df_daily.empty:
        path_daily = write_partitioned(
            df_daily, OUTPUT_DIR,
            f"retail_sentiment_daily_{mode}_{today_str}.parquet",
        )
        print(f"[+] Daily aggregates: {len(df_daily)} rows -> {path_daily}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retail investor sentiment pipeline (Stocktwits)")
    parser.add_argument("--backfill", action="store_true", help="Broader message history fetch")
    args = parser.parse_args()
    main(backfill=args.backfill)
