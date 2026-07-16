#!/usr/bin/env python3
"""
Finnhub Fundamentals Pipeline -- earnings, estimates, ownership, splits,
peers, executives, filing sentiment, transcripts, and news sentiment.

Fetches ~10 new Finnhub data categories not covered by the existing
finnhub_pipeline.py, finnhub_events_pipeline.py, finnhub_expansion_pipeline.py,
or dividend_pipeline.py:

  HIGH PRIORITY (core fundamental data, free tier):
    1. Earnings History        /stock/earnings
    2. EPS Estimates           /stock/eps-estimate
    3. Revenue Estimates       /stock/revenue-estimate
    4. Ownership (13F/13D)     /stock/ownership
    5. Splits                  /stock/split
    6. Peers                   /stock/peers
    7. Executives              /stock/executive

  MEDIUM PRIORITY (NLP/alternative):
    8. Filing Sentiment        /stock/filings-sentiment
    9. Transcripts             /stock/transcripts
   10. News Sentiment          /news-sentiment

NOTE: /stock/financials and /stock/revenue-breakdown are Premium-only
and excluded from this free-tier pipeline.

Reuses the shared RateLimiter + get_with_backoff from finnhub_pipeline.py.

CLI:
  python finnhub_fundamentals_pipeline.py
  python finnhub_fundamentals_pipeline.py --backfill

Outputs (all under storage/raw/finnhub/):
  earnings_history/earnings_history_{mode}_{YYYYMMDD}.parquet
  eps_estimates/eps_estimates_{mode}_{YYYYMMDD}.parquet
  revenue_estimates/revenue_estimates_{mode}_{YYYYMMDD}.parquet
  ownership/ownership_{mode}_{YYYYMMDD}.parquet
  splits/splits_{mode}_{YYYYMMDD}.parquet
  peers/peers_{mode}_{YYYYMMDD}.parquet
  executives/executives_{mode}_{YYYYMMDD}.parquet
  filing_sentiment/filing_sentiment_{mode}_{YYYYMMDD}.parquet
  transcripts/transcripts_{mode}_{YYYYMMDD}.parquet
  company_news_sentiment/company_news_sentiment_{mode}_{YYYYMMDD}.parquet
  (renamed from "news_sentiment" -- that dir name collides with the existing
  production news_sentiment_pipeline.py table of the same name)
"""

import os
import datetime
import argparse
import pandas as pd

from storage_utils import write_partitioned
from finnhub_pipeline import (
    FINNHUB_API_KEY,
    get_with_backoff,
    get_dji_symbols,
)

OUTPUT_BASE = os.path.join("storage", "raw", "finnhub")

DIRS = {
    "earnings_history":   os.path.join(OUTPUT_BASE, "earnings_history"),
    "eps_estimates":      os.path.join(OUTPUT_BASE, "eps_estimates"),
    "revenue_estimates":  os.path.join(OUTPUT_BASE, "revenue_estimates"),
    "ownership":          os.path.join(OUTPUT_BASE, "ownership"),
    "splits":             os.path.join(OUTPUT_BASE, "splits"),
    "peers":              os.path.join(OUTPUT_BASE, "peers"),
    "executives":         os.path.join(OUTPUT_BASE, "executives"),
    "filing_sentiment":   os.path.join(OUTPUT_BASE, "filing_sentiment"),
    "transcripts":        os.path.join(OUTPUT_BASE, "transcripts"),
    "company_news_sentiment": os.path.join(OUTPUT_BASE, "company_news_sentiment"),
}

LOOKBACK = {
    "incremental": 90,
    "backfill":    365 * 3,
}

ESTIMATE_RENAME = {
    "symbol":           "symbol",
    "date":             "date",
    "epsEstimate":      "eps_estimate",
    "epsEstimateHigh":  "eps_estimate_high",
    "epsEstimateLow":   "eps_estimate_low",
    "epsNumEstimates":  "eps_num_estimates",
    "revenueEstimate":      "revenue_estimate",
    "revenueEstimateHigh":  "revenue_estimate_high",
    "revenueEstimateLow":   "revenue_estimate_low",
    "revenueNumEstimates":  "revenue_num_estimates",
    "freq":             "freq",
}

OWNERSHIP_RENAME = {
    "name":             "holder_name",
    "share":            "shares_held",
    "change":           "shares_change",
    "pctHeld":          "pct_held",
    "holderType":       "holder_type",
    "activity":         "activity",
}


def _now_iso():
    return datetime.datetime.utcnow().isoformat()


def _fmt(dt):
    return dt.strftime("%Y-%m-%d")


# ============================================================
#  Per-symbol fetchers
# ============================================================

def fetch_earnings_history(symbol, limit=20):
    """Quarterly earnings surprise history (actual vs estimate EPS)."""
    data = get_with_backoff("stock/earnings", {"symbol": symbol, "limit": limit})
    if not data:
        return None
    rows = data if isinstance(data, list) else data.get("data", [])
    if not rows:
        return None
    df = pd.DataFrame(rows)
    # Finnhub's /stock/earnings response includes a literal "year" field.
    # Never leave that as a DataFrame column name -- Hive partitioning exposes
    # "year"/"month" as virtual columns and silently overwrites them with the
    # fetch date (see CLAUDE.md hard-won gotchas).
    if "year" in df.columns:
        df = df.rename(columns={"year": "obs_year"})
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_eps_estimates(symbol, freq="quarterly"):
    """Analyst EPS consensus estimates."""
    data = get_with_backoff(
        "stock/eps-estimate",
        {"symbol": symbol, "freq": freq},
    )
    if not data:
        return None
    rows = data.get("data", []) if isinstance(data, dict) else data
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns={k: v for k, v in ESTIMATE_RENAME.items() if k in df.columns})
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["estimate_type"] = "eps"
    df["fetched_at"] = _now_iso()
    return df


def fetch_revenue_estimates(symbol, freq="quarterly"):
    """Analyst revenue consensus estimates."""
    data = get_with_backoff(
        "stock/revenue-estimate",
        {"symbol": symbol, "freq": freq},
    )
    if not data:
        return None
    rows = data.get("data", []) if isinstance(data, dict) else data
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns={k: v for k, v in ESTIMATE_RENAME.items() if k in df.columns})
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["estimate_type"] = "revenue"
    df["fetched_at"] = _now_iso()
    return df


def fetch_ownership(symbol, limit=10):
    """Full shareholder list from 13F/13D/13G filings."""
    data = get_with_backoff(
        "stock/ownership",
        {"symbol": symbol, "limit": limit},
    )
    if not data:
        return None
    rows = data.get("data", []) if isinstance(data, dict) else data
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns={k: v for k, v in OWNERSHIP_RENAME.items() if k in df.columns})
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_splits(symbol):
    """Stock split history."""
    data = get_with_backoff("stock/split", {"symbol": symbol})
    if not data:
        return None
    rows = data if isinstance(data, list) else data.get("data", [])
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_peers(symbol):
    """Company peer list by sector/industry."""
    data = get_with_backoff("stock/peers", {"symbol": symbol})
    if not data:
        return None
    # Peers returns a list of ticker strings
    rows = data if isinstance(data, list) else data.get("data", [])
    if not rows:
        return None
    df = pd.DataFrame({"peer": rows})
    df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_executives(symbol):
    """C-suite executives and board members."""
    data = get_with_backoff("stock/executive", {"symbol": symbol})
    if not data:
        return None
    rows = data.get("executive", []) if isinstance(data, dict) else data
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_filing_sentiment(symbol):
    """NLP sentiment analysis on SEC 10-K/10-Q filings."""
    data = get_with_backoff(
        "stock/filings-sentiment",
        {"symbol": symbol},
    )
    if not data:
        return None
    rows = data.get("data", []) if isinstance(data, dict) else data
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_transcripts_list(symbol):
    """List of available earnings call transcripts."""
    data = get_with_backoff(
        "stock/transcripts/list",
        {"symbol": symbol},
    )
    if not data:
        return None
    # Finnhub nests the transcript array under "transcripts", not "data":
    # {"symbol": ..., "transcripts": [...]}
    rows = data.get("transcripts", []) if isinstance(data, dict) else data
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_news_sentiment(symbol):
    """Automated news sentiment score."""
    data = get_with_backoff("news-sentiment", {"symbol": symbol})
    if not data:
        return None
    # Flatten the nested response
    result = {}
    result["symbol"] = symbol
    result["buzz"] = data.get("buzz", {}).get("buzzHigh", None)
    result["buzz_low"] = data.get("buzz", {}).get("buzzLow", None)
    result["article_volume"] = data.get("buzz", {}).get("articlesInLastWeek", None)
    result["weekly_average"] = data.get("buzz", {}).get("weeklyAverage", None)
    result["sentiment"] = data.get("sentiment", {}).get("sentiment", None)
    result["sentiment_bullish"] = data.get("sentiment", {}).get("bullishPercent", None)
    result["sentiment_bearish"] = data.get("sentiment", {}).get("bearishPercent", None)
    result["company_news_score"] = data.get("sentiment", {}).get("companyNewsScore", None)
    result["sector_avg_sentiment"] = data.get("sectorAverage", {}).get("sectorAverageSentiment", None)
    result["sector_avg_bullish"] = data.get("sectorAverage", {}).get("sectorAverageBullishPercent", None)
    result["sector_avg_news_score"] = data.get("sectorAverage", {}).get("sectorAverageNewsScore", None)
    result["fetched_at"] = _now_iso()
    return pd.DataFrame([result])


# ============================================================
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Finnhub Fundamentals Pipeline -- earnings, estimates, ownership, splits, peers, executives"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch 3 years of history instead of 90 days.",
    )
    args = parser.parse_args()

    if not FINNHUB_API_KEY:
        print("CRITICAL ERROR: FINNHUB_API_KEY env variable is not set. Exiting.")
        return

    for path in DIRS.values():
        os.makedirs(path, exist_ok=True)

    mode = "backfill" if args.backfill else "incremental"
    today = datetime.datetime.utcnow()
    today_str = today.strftime("%Y%m%d")

    symbols = get_dji_symbols()
    total = len(symbols)

    collected = {
        "earnings_history":   [],
        "eps_estimates":      [],
        "revenue_estimates":  [],
        "ownership":          [],
        "splits":             [],
        "peers":              [],
        "executives":         [],
        "filing_sentiment":   [],
        "transcripts":        [],
        "company_news_sentiment": [],
    }

    print(f"[finnhub_fundamentals] {mode}: {today_str} ({total} symbols)")
    print("=" * 60)

    for i, symbol in enumerate(symbols, 1):
        print(f"\n[{i}/{total}] {symbol}")

        # 1. Earnings History
        df = fetch_earnings_history(symbol)
        if df is not None:
            collected["earnings_history"].append(df)
            print(f"  + earnings_history ({len(df)} rows)")

        # 2. EPS Estimates
        df = fetch_eps_estimates(symbol)
        if df is not None:
            collected["eps_estimates"].append(df)
            print(f"  + eps_estimates ({len(df)} rows)")

        # 3. Revenue Estimates
        df = fetch_revenue_estimates(symbol)
        if df is not None:
            collected["revenue_estimates"].append(df)
            print(f"  + revenue_estimates ({len(df)} rows)")

        # 4. Ownership
        df = fetch_ownership(symbol)
        if df is not None:
            collected["ownership"].append(df)
            print(f"  + ownership ({len(df)} rows)")

        # 5. Splits
        df = fetch_splits(symbol)
        if df is not None:
            collected["splits"].append(df)
            print(f"  + splits ({len(df)} rows)")

        # 6. Peers
        df = fetch_peers(symbol)
        if df is not None:
            collected["peers"].append(df)
            print(f"  + peers ({len(df)} rows)")

        # 7. Executives
        df = fetch_executives(symbol)
        if df is not None:
            collected["executives"].append(df)
            print(f"  + executives ({len(df)} rows)")

        # 8. Filing Sentiment
        df = fetch_filing_sentiment(symbol)
        if df is not None:
            collected["filing_sentiment"].append(df)
            print(f"  + filing_sentiment ({len(df)} rows)")

        # 9. Transcripts List
        df = fetch_transcripts_list(symbol)
        if df is not None:
            collected["transcripts"].append(df)
            print(f"  + transcripts ({len(df)} rows)")

        # 10. News Sentiment
        df = fetch_news_sentiment(symbol)
        if df is not None:
            collected["company_news_sentiment"].append(df)
            print(f"  + company_news_sentiment ({len(df)} rows)")

    # ============================================================
    #  Save all collected data
    # ============================================================
    print("\n" + "=" * 60)
    print("Saving collected data to Parquet...")

    for key, dfs in collected.items():
        if dfs:
            combined = pd.concat(dfs, ignore_index=True)
            filename = f"{key}_{mode}_{today_str}.parquet"
            out_path = write_partitioned(combined, DIRS[key], filename)
            n_syms = combined["symbol"].nunique() if "symbol" in combined.columns else 0
            print(f"  Saved {key} -> {out_path} ({len(combined):,} rows, {n_syms} symbols)")
        else:
            print(f"  Warning: No data collected for {key}")

    print("\n--- PIPELINE RUN COMPLETE ---")


if __name__ == "__main__":
    main()
