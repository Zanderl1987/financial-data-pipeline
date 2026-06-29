#!/usr/bin/env python3
"""
Google Trends Pipeline — search interest time series for financial keywords.

Uses pytrends (unofficial Google Trends API wrapper). No API key required.
Rate limit: ~1 request per 1-2 seconds; batches of 5 keywords per request.

Google Trends data is normalized 0-100 (relative interest within the time window).
Absolute volume is not available, but relative peaks and troughs are highly
predictive of consumer behavior — hedge funds have used search volume for
terms like "unemployment benefits" and "mortgage rates" as leading indicators
for 10+ years.

Three keyword groups:
  1. Economic stress indicators — "recession", "layoffs", "unemployment benefits",
     "inflation", "credit card debt": leading indicators for consumer health
  2. Market activity signals — company name + "stock", "buy stocks", "put options":
     retail participation and directional positioning signals
  3. Commodity / sector demand — "gas prices", "electric car", "solar panels",
     "AI stocks", "gold price": sector rotation and macro demand signals

Note on pytrends reliability: the library makes unauthenticated requests and
Google periodically rate-limits or changes the internal API. The pipeline uses
exponential backoff and catches all errors gracefully — partial results are
still written if some batches succeed.

Outputs:
  storage/raw/google_trends/year=YYYY/month=MM/google_trends_{group}_{mode}_{YYYYMMDD}.parquet
  CATALOG tables: google_trends_economic, google_trends_market, google_trends_sector

Usage:
  python google_trends_pipeline.py             # incremental (last 90 days, daily)
  python google_trends_pipeline.py --backfill  # 5 years of weekly data
"""

import argparse
import datetime
import os
import time

import pandas as pd
from pytrends.request import TrendReq
from dotenv import load_dotenv

from storage_utils import write_partitioned

load_dotenv()

OUTPUT_DIR = os.path.join("storage", "raw", "google_trends")

BACKFILL_YEARS    = 5
INCREMENTAL_DAYS  = 90
BATCH_SIZE        = 5     # Google Trends API max keywords per request
REQUEST_PAUSE     = 2.0   # seconds between batches; essential to avoid 429s
BACKOFF_BASE      = 60    # seconds for first retry on rate-limit
MAX_RETRIES       = 3

# ── Keyword groups ─────────────────────────────────────────────────────────────

KEYWORD_GROUPS: dict[str, list[str]] = {
    "economic": [
        # Consumer stress / macro leading indicators
        "recession",
        "layoffs",
        "unemployment benefits",
        "inflation",
        "credit card debt",
        "mortgage rates",
        "how to save money",
        "food prices",
        "gas prices",
        "cost of living",
        "interest rates",
        "Federal Reserve",
        "bank failure",
        "housing market crash",
        "student loan forgiveness",
    ],
    "market": [
        # Retail investor participation and sentiment
        "buy stocks",
        "how to invest",
        "stock market crash",
        "put options",
        "call options",
        "short selling",
        "Robinhood",
        "day trading",
        "Bitcoin",
        "meme stocks",
        # Company-specific search interest (proxy for retail attention)
        "Apple stock",
        "Tesla stock",
        "Nvidia stock",
        "Amazon stock",
        "Microsoft stock",
    ],
    "sector": [
        # Sector rotation and commodity demand signals
        "gold price",
        "oil price",
        "electric car",
        "solar panels",
        "AI stocks",
        "semiconductor shortage",
        "crypto crash",
        "real estate bubble",
        "pharmaceutical stocks",
        "defense stocks",
        "bank stocks",
        "airline stocks",
        "retail sales",
        "supply chain",
        "tariffs",
    ],
}


def _make_pytrends() -> TrendReq:
    # Don't pass retries/backoff_factor — pytrends passes them to urllib3's
    # Retry() which renamed method_whitelist to allowed_methods in urllib3>=2.0.
    # Our own _fetch_batch() handles retries instead.
    return TrendReq(hl="en-US", tz=0, timeout=(10, 25))


def _fetch_batch(pytrends: TrendReq, keywords: list[str],
                 timeframe: str) -> pd.DataFrame | None:
    """Fetch one batch of up to 5 keywords. Returns tidy long-format DataFrame."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            pytrends.build_payload(keywords, cat=0, timeframe=timeframe, geo="US")
            df = pytrends.interest_over_time()
            if df.empty:
                return None
            # Drop the isPartial column Google Trends appends
            df = df.drop(columns=["isPartial"], errors="ignore")
            df = df.reset_index().rename(columns={"date": "date"})
            # Melt wide -> long
            df = df.melt(id_vars=["date"], var_name="keyword", value_name="interest")
            df["interest"] = pd.to_numeric(df["interest"], errors="coerce")
            return df
        except Exception as exc:
            err_str = str(exc)
            if "429" in err_str or "response" in err_str.lower():
                wait = BACKOFF_BASE * attempt
                print(f"    rate-limited — waiting {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
            else:
                print(f"    error (attempt {attempt}): {exc}")
                time.sleep(REQUEST_PAUSE * 3)
    return None


def fetch_group(pytrends: TrendReq, group_name: str, keywords: list[str],
                timeframe: str) -> pd.DataFrame:
    """Fetch all keywords for a group in BATCH_SIZE chunks."""
    frames = []
    batches = [keywords[i:i + BATCH_SIZE] for i in range(0, len(keywords), BATCH_SIZE)]

    for i, batch in enumerate(batches):
        batch_str = ", ".join(batch)
        print(f"    batch {i+1}/{len(batches)}: {batch_str[:60]}...", end=" ", flush=True)
        df = _fetch_batch(pytrends, batch, timeframe)
        if df is not None and not df.empty:
            frames.append(df)
            print(f"{len(df)} rows")
        else:
            print("no data")
        if i < len(batches) - 1:
            time.sleep(REQUEST_PAUSE)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df["group"] = group_name
    return df


def main(backfill: bool = False) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    now   = datetime.datetime.utcnow()
    today = now.strftime("%Y%m%d")
    mode  = "backfill" if backfill else "incremental"

    if backfill:
        end_date   = now.strftime("%Y-%m-%d")
        start_date = (now - datetime.timedelta(days=365 * BACKFILL_YEARS)).strftime("%Y-%m-%d")
        timeframe  = f"{start_date} {end_date}"
        granularity = "weekly"
    else:
        end_date   = now.strftime("%Y-%m-%d")
        start_date = (now - datetime.timedelta(days=INCREMENTAL_DAYS)).strftime("%Y-%m-%d")
        timeframe  = f"{start_date} {end_date}"
        granularity = "daily"

    total_kw = sum(len(v) for v in KEYWORD_GROUPS.values())
    print(f"Google Trends Pipeline  mode={mode}  granularity={granularity}")
    print(f"Timeframe:  {timeframe}")
    print(f"Keywords:   {total_kw} across {len(KEYWORD_GROUPS)} groups")
    print()

    pytrends = _make_pytrends()

    for group_name, keywords in KEYWORD_GROUPS.items():
        print(f"  [{group_name}] {len(keywords)} keywords...")
        df = fetch_group(pytrends, group_name, keywords, timeframe)

        if df.empty:
            print(f"    No data for group '{group_name}'")
            continue

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["fetched_at"] = now.isoformat()
        df["timeframe"]  = timeframe
        df["granularity"] = granularity

        table_name = f"google_trends_{group_name}"
        path = write_partitioned(
            df, OUTPUT_DIR, f"google_trends_{group_name}_{mode}_{today}.parquet"
        )
        date_min = df["date"].min().strftime("%Y-%m-%d")
        date_max = df["date"].max().strftime("%Y-%m-%d")
        print(f"  [+] {path}")
        print(f"      {len(df):,} rows | {df['keyword'].nunique()} keywords | {date_min} to {date_max}")

        # Pause between groups to be polite
        time.sleep(REQUEST_PAUSE * 2)

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Google Trends pipeline — financial keyword search interest (keyless, unofficial)"
    )
    parser.add_argument("--backfill", action="store_true",
                        help=f"Fetch {BACKFILL_YEARS} years of weekly data. Default: last {INCREMENTAL_DAYS} days daily.")
    args = parser.parse_args()
    main(backfill=args.backfill)
