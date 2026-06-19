#!/usr/bin/env python3
"""
Finnhub Pipeline:
  - Fetches various data feeds from the Finnhub API (free tier) for target symbols:
    1. Company Profile (/stock/profile2)
    2. Real-time Quotes (/quote)
    3. Basic Financial Metrics (/stock/metric?metric=all)
    4. Recommendation Trends (/stock/recommendation)
    5. Price Targets (/stock/price-target)
    6. Upgrades/Downgrades (/stock/upgrade-downgrade)
    7. Company News (/company-news)
  - Abides by the 60 requests/minute free tier rate limit.

Outputs:
  storage/raw/finnhub/profile/profile_{YYYYMMDD}.parquet
  storage/raw/finnhub/quotes/quotes_{YYYYMMDD}.parquet
  storage/raw/finnhub/metrics/metrics_{YYYYMMDD}.parquet
  storage/raw/finnhub/recommendations/recommendations_{YYYYMMDD}.parquet
  storage/raw/finnhub/price_targets/price_targets_{YYYYMMDD}.parquet
  storage/raw/finnhub/upgrades/upgrades_{YYYYMMDD}.parquet
  storage/raw/finnhub/news/news_{YYYYMMDD}.parquet
"""

import os
import time
import datetime
import argparse
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
BASE_URL = "https://finnhub.io/api/v1"

# Output directories
OUTPUT_BASE = os.path.join("storage", "raw", "finnhub")
DIRS = {
    "profile": os.path.join(OUTPUT_BASE, "profile"),
    "quotes": os.path.join(OUTPUT_BASE, "quotes"),
    "metrics": os.path.join(OUTPUT_BASE, "metrics"),
    "recommendations": os.path.join(OUTPUT_BASE, "recommendations"),
    "price_targets": os.path.join(OUTPUT_BASE, "price_targets"),
    "upgrades": os.path.join(OUTPUT_BASE, "upgrades"),
    "news": os.path.join(OUTPUT_BASE, "news")
}

# Rate limit is 60 req/min. We enforce a strict spacing to avoid bursts.
REQUEST_INTERVAL = 1.1  # seconds
MAX_RETRIES = 3
BACKOFF_SECONDS = 60

FALLBACK_SYMBOLS = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
    "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
]

def get_dji_symbols():
    """Scrape the 30 Dow components from Wikipedia.

    The components table's index on the page shifts over time, so locate it by
    content — the table carrying a Symbol/Ticker column with ~30 rows — rather
    than by a hardcoded position. Falls back to a static list on any failure.
    """
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
            storage_options={"User-Agent": "Mozilla/5.0"},
        )
        for df in tables:
            col = next(
                (c for c in df.columns
                 if str(c).strip().lower() in ("symbol", "ticker")),
                None,
            )
            if col is not None and 25 <= len(df) <= 35:
                symbols = (
                    df[col].astype(str).str.strip().str.upper()
                    .str.replace(r"\s+.*$", "", regex=True)  # drop footnote suffixes
                    .tolist()
                )
                print(f"Scraped {len(symbols)} DJI symbols from Wikipedia.")
                return symbols
        raise ValueError("no components table with a Symbol/Ticker column found")
    except Exception as e:
        print(f"Scraping DJI symbols failed ({e}). Using hardcoded fallback symbols.")
        return FALLBACK_SYMBOLS

class RateLimiter:
    def __init__(self, interval):
        self.interval = interval
        self.last_call = 0.0

    def wait(self):
        now = time.time()
        elapsed = now - self.last_call
        if elapsed < self.interval:
            sleep_time = self.interval - elapsed
            time.sleep(sleep_time)
        self.last_call = time.time()

limiter = RateLimiter(REQUEST_INTERVAL)

def get_with_backoff(endpoint, params=None):
    if not params:
        params = {}
    params["token"] = FINNHUB_API_KEY
    url = f"{BASE_URL}/{endpoint}"
    
    for attempt in range(1, MAX_RETRIES + 1):
        limiter.wait()
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                wait_time = BACKOFF_SECONDS * attempt
                print(f"  [429] Rate limit hit. Backing off {wait_time}s (attempt {attempt}/{MAX_RETRIES})...")
                time.sleep(wait_time)
            else:
                print(f"  [HTTP {r.status_code}] Error fetching {endpoint} for params {params}: {r.text[:120]}")
                return None
        except requests.RequestException as e:
            print(f"  Request connection error (attempt {attempt}): {e}")
            time.sleep(10)
            
    print(f"  Failed to retrieve data after {MAX_RETRIES} attempts.")
    return None

def fetch_profile(symbol):
    data = get_with_backoff("stock/profile2", {"symbol": symbol})
    if not data or "name" not in data:
        return None
    # Profile response is a flat dictionary. Convert to single row.
    df = pd.DataFrame([data])
    df["symbol"] = symbol
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df

def fetch_quote(symbol):
    data = get_with_backoff("quote", {"symbol": symbol})
    if not data or data.get("t") == 0:
        return None
    
    # Flat dictionary containing price statistics
    quote_data = {
        "symbol": symbol,
        "open": data.get("o"),
        "high": data.get("h"),
        "low": data.get("l"),
        "current": data.get("c"),
        "prev_close": data.get("pc"),
        "change": data.get("d"),
        "percent_change": data.get("dp"),
        "timestamp": data.get("t"),
        "fetched_at": datetime.datetime.utcnow().isoformat()
    }
    return pd.DataFrame([quote_data])

def fetch_metrics(symbol):
    data = get_with_backoff("stock/metric", {"symbol": symbol, "metric": "all"})
    if not data or "metric" not in data:
        return None
    
    # Flatten nested metrics dictionary
    flat_data = {f"metric_{k}": v for k, v in data["metric"].items()}
    flat_data["symbol"] = symbol
    flat_data["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return pd.DataFrame([flat_data])

def fetch_recommendations(symbol):
    data = get_with_backoff("stock/recommendation", {"symbol": symbol})
    if not data:
        return None
    
    df = pd.DataFrame(data)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df

def fetch_price_target(symbol):
    data = get_with_backoff("stock/price-target", {"symbol": symbol})
    if not data or "targetMedian" not in data:
        return None
        
    df = pd.DataFrame([data])
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df

def fetch_upgrades(symbol):
    data = get_with_backoff("stock/upgrade-downgrade", {"symbol": symbol})
    if not data:
        return None
        
    df = pd.DataFrame(data)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df

def fetch_news(symbol, start_date, end_date):
    params = {
        "symbol": symbol,
        "from": start_date,
        "to": end_date
    }
    data = get_with_backoff("company-news", params)
    if not data:
        return None
        
    df = pd.DataFrame(data)
    df["symbol"] = symbol
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df

def main():
    parser = argparse.ArgumentParser(description="Finnhub Financial & Alternative Data Pipeline")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch longer range of history for news (30 days instead of 3 days).",
    )
    args = parser.parse_args()

    if not FINNHUB_API_KEY:
        print("CRITICAL ERROR: FINNHUB_API_KEY env variable is not set. Exiting.")
        return

    # Create storage directories
    for path in DIRS.values():
        os.makedirs(path, exist_ok=True)

    symbols = get_dji_symbols()
    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")

    # Define news date range
    end_dt = datetime.datetime.utcnow()
    lookback_days = 30 if args.backfill else 3
    start_dt = end_dt - datetime.timedelta(days=lookback_days)
    
    start_date_str = start_dt.strftime("%Y-%m-%d")
    end_date_str = end_dt.strftime("%Y-%m-%d")

    # Accumulator dicts
    collected = {k: [] for k in DIRS.keys()}

    total_symbols = len(symbols)
    print(f"Starting Finnhub data harvest for {total_symbols} symbols...")

    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{total_symbols}] Processing {symbol}...")
        
        # 1. Company Profile
        prof_df = fetch_profile(symbol)
        if prof_df is not None:
            collected["profile"].append(prof_df)
            
        # 2. Quotes
        quote_df = fetch_quote(symbol)
        if quote_df is not None:
            collected["quotes"].append(quote_df)

        # 3. Basic Metrics
        metrics_df = fetch_metrics(symbol)
        if metrics_df is not None:
            collected["metrics"].append(metrics_df)

        # 4. Recommendation Trends
        rec_df = fetch_recommendations(symbol)
        if rec_df is not None:
            collected["recommendations"].append(rec_df)

        # 5. Price Targets
        pt_df = fetch_price_target(symbol)
        if pt_df is not None:
            collected["price_targets"].append(pt_df)

        # 6. Upgrades/Downgrades
        up_df = fetch_upgrades(symbol)
        if up_df is not None:
            collected["upgrades"].append(up_df)

        # 7. Company News
        news_df = fetch_news(symbol, start_date_str, end_date_str)
        if news_df is not None:
            collected["news"].append(news_df)

    # Concatenate and save collected datasets
    mode_tag = "backfill" if args.backfill else "incremental"
    print("\nSaving collected data to Parquet...")

    for key, dfs in collected.items():
        if dfs:
            combined = pd.concat(dfs, ignore_index=True)
            filename = f"{key}_{mode_tag}_{today_str}.parquet"
            out_path = os.path.join(DIRS[key], filename)
            combined.to_parquet(out_path, index=False)
            print(f"  Saved {key} -> {out_path} ({len(combined)} rows)")
        else:
            print(f"  Warning: No data collected for {key}")

    print("\n--- PIPELINE RUN COMPLETE ---")

if __name__ == "__main__":
    main()
