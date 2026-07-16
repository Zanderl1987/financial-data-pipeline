#!/usr/bin/env python3
"""
Finnhub Expansion Pipeline -- alternative data + extended endpoints.

Fetches ~12 new Finnhub data categories not covered by the existing
finnhub_pipeline.py, finnhub_events_pipeline.py, or dividend_pipeline.py:

  HIGH PRIORITY (unique alt-data, free tier):
    1. ESG Scores              /stock/esg
    2. Congressional Trading   /stock/congressional-trading
    3. Supply Chain             /stock/supply-chain
    4. Insider Sentiment        /stock/insider-sentiment
    5. Social Sentiment         /stock/social-sentiment
    6. SEC Filings Metadata     /stock/filings
    7. Earnings Quality Score   /stock/earnings-quality-score

  MEDIUM PRIORITY (per-symbol, date-range):
    8. Lobbying                 /stock/lobbying
    9. USA Spending             /stock/usa-spending
   10. USPTO Patents            /stock/uspto-patent
   11. Visa Applications        /stock/visa-application

  MARKET-WIDE (single request each):
   12. Economic Calendar        /economic-calendar

Reuses the shared RateLimiter + get_with_backoff from finnhub_pipeline.py.

Rate limit: 60 req/min free tier. The existing pipeline runs ~7 calls/symbol
(profile, quote, metrics, recommendations, price_targets, upgrades, news).
This expansion adds ~10 calls/symbol. For 30 DJI symbols that's ~300 extra
calls = ~5 minutes extra runtime.

CLI:
  python finnhub_expansion_pipeline.py
  python finnhub_expansion_pipeline.py --backfill

Outputs (all under storage/raw/finnhub/):
  esg/esg_{mode}_{YYYYMMDD}.parquet
  congressional_trading/congressional_trading_{mode}_{YYYYMMDD}.parquet
  supply_chain/supply_chain_{mode}_{YYYYMMDD}.parquet
  insider_sentiment/insider_sentiment_{mode}_{YYYYMMDD}.parquet
  social_sentiment/social_sentiment_{mode}_{YYYYMMDD}.parquet
  sec_filings/sec_filings_{mode}_{YYYYMMDD}.parquet
  earnings_quality/earnings_quality_{mode}_{YYYYMMDD}.parquet
  lobbying/lobbying_{mode}_{YYYYMMDD}.parquet
  usa_spending/usa_spending_{mode}_{YYYYMMDD}.parquet
  uspto_patents/uspto_patents_{mode}_{YYYYMMDD}.parquet
  visa_applications/visa_applications_{mode}_{YYYYMMDD}.parquet
  economic_calendar/economic_calendar_{mode}_{YYYYMMDD}.parquet
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
    "esg":                  os.path.join(OUTPUT_BASE, "esg"),
    "congressional_trading": os.path.join(OUTPUT_BASE, "congressional_trading"),
    "supply_chain":         os.path.join(OUTPUT_BASE, "supply_chain"),
    "insider_sentiment":    os.path.join(OUTPUT_BASE, "insider_sentiment"),
    "social_sentiment":     os.path.join(OUTPUT_BASE, "social_sentiment"),
    "sec_filings":          os.path.join(OUTPUT_BASE, "sec_filings"),
    "earnings_quality":     os.path.join(OUTPUT_BASE, "earnings_quality"),
    "lobbying":             os.path.join(OUTPUT_BASE, "lobbying"),
    "usa_spending":         os.path.join(OUTPUT_BASE, "usa_spending"),
    "uspto_patents":        os.path.join(OUTPUT_BASE, "uspto_patents"),
    "visa_applications":    os.path.join(OUTPUT_BASE, "visa_applications"),
    "economic_calendar":    os.path.join(OUTPUT_BASE, "economic_calendar"),
}

# Date windows (days back for incremental / backfill)
LOOKBACK = {
    "incremental": 90,
    "backfill":    365 * 3,   # 3 years
}

# Renames for consistent snake_case columns
CONGRESSIONAL_RENAME = {
    "memberName":     "member_name",
    "memberParty":    "member_party",
    "memberState":    "member_state",
    "chamber":        "chamber",
    "transactionType":"transaction_type",
    "transactionDate":"transaction_date",
    "notificationDate":"notification_date",
    "owner":          "owner",
    "ticker":         "ticker",
    "assetDescription":"asset_description",
    "amount":         "amount",
    "comment":        "comment",
}

INSIDER_SENTIMENT_RENAME = {
    "mspr":           "mspr",
    "mspr_numeric":   "mspr_numeric",
    "sentiment":      "sentiment_change",
    "change":         "share_change",
    # NOTE: never name output columns "month"/"year" -- Hive partitioning
    # exposes those as virtual columns and silently overwrites them with the
    # fetch date (see CLAUDE.md hard-won gotchas). Use obs_month/obs_year.
    "month":          "obs_month",
    "year":           "obs_year",
}

SOCIAL_SENTIMENT_RENAME = {
    "atTime":         "timestamp",
    "buzz":           "buzz",
    "articleSentimentLower":"article_sentiment_lower",
    "articleSentimentUpper":"article_sentiment_upper",
    "articleSentimentAverage":"article_sentiment_avg",
    "newsScore":      "news_score",
    "sectorAverageNewsScore":"sector_avg_news_score",
    "sectorAverageBuzz":"sector_avg_buzz",
    "sectorAverageArticleSentiment":"sector_avg_article_sentiment",
}

FILING_RENAME = {
    "filingDate":     "filing_date",
    "acceptedDate":   "accepted_date",
    "form":           "form_type",
    "filedAt":        "filed_at",
    "url":            "url",
    "primaryDocument":"primary_document",
}

LOBBYING_RENAME = {
    "startDate":      "start_date",
    "endDate":        "end_date",
    "lobbyingFirm":   "lobbying_firm",
    "generalIssueCode":"general_issue_code",
    "specificIssue":  "specific_issue",
    "lobbyistName":   "lobbyist_name",
    "amount":         "amount",
}

USA_SPENDING_RENAME = {
    "startDate":      "start_date",
    "endDate":        "end_date",
    "awardingAgency": "awarding_agency",
    "awardeeName":    "awardee_name",
    "amount":         "amount",
    "contractType":   "contract_type",
    "naicsCode":      "naics_code",
    "description":    "description",
}


def _now_iso():
    return datetime.datetime.utcnow().isoformat()


def _fmt(dt):
    return dt.strftime("%Y-%m-%d")


# ============================================================
#  Per-symbol fetchers
# ============================================================

def fetch_esg(symbol):
    """ESG scores: environmental, social, governance 0-100."""
    data = get_with_backoff("stock/esg", {"symbol": symbol})
    if not data or not data.get("data"):
        return None
    rows = data["data"]
    df = pd.DataFrame(rows) if isinstance(rows, list) else pd.DataFrame([rows])
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_congressional_trading(symbol, start_date, end_date):
    """Congressional member stock trades for a given ticker."""
    data = get_with_backoff(
        "stock/congressional-trading",
        {"symbol": symbol, "from": start_date, "to": end_date},
    )
    if not data:
        return None
    rows = data if isinstance(data, list) else data.get("data", [])
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns=CONGRESSIONAL_RENAME)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_supply_chain(symbol):
    """Key customers and suppliers map."""
    data = get_with_backoff("stock/supply-chain", {"symbol": symbol})
    if not data or not data.get("data"):
        return None
    rows = data["data"]
    # Supply chain returns a dict with 'customers' and 'suppliers' lists
    frames = []
    for side in ("customers", "suppliers"):
        items = rows.get(side, [])
        if items:
            sub = pd.DataFrame(items)
            sub["side"] = side
            sub["symbol"] = symbol
            frames.append(sub)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df["fetched_at"] = _now_iso()
    return df


def fetch_insider_sentiment(symbol, start_date, end_date):
    """Insider sentiment (MSPR scores) over a date range."""
    data = get_with_backoff(
        "stock/insider-sentiment",
        {"symbol": symbol, "from": start_date, "to": end_date},
    )
    if not data:
        return None
    rows = data.get("data", []) if isinstance(data, dict) else data
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns=INSIDER_SENTIMENT_RENAME)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_social_sentiment(symbol, start_date, end_date):
    """Aggregated Reddit / StockTwits sentiment."""
    data = get_with_backoff(
        "stock/social-sentiment",
        {"symbol": symbol, "from": start_date, "to": end_date},
    )
    if not data:
        return None
    rows = data.get("data", []) if isinstance(data, dict) else data
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns=SOCIAL_SENTIMENT_RENAME)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_sec_filings(symbol, start_date, end_date):
    """SEC filing metadata (10-K, 10-Q, 8-K, etc.)."""
    data = get_with_backoff(
        "stock/filings",
        {"symbol": symbol, "from": start_date, "to": end_date},
    )
    if not data:
        return None
    rows = data if isinstance(data, list) else []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns=FILING_RENAME)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_earnings_quality(symbol):
    """Finnhub proprietary earnings quality score."""
    data = get_with_backoff("stock/earnings-quality-score", {"symbol": symbol})
    if not data or not data.get("data"):
        return None
    rows = data["data"]
    df = pd.DataFrame(rows) if isinstance(rows, list) else pd.DataFrame([rows])
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_lobbying(symbol, start_date, end_date):
    """Lobbying expenditure data for a company."""
    data = get_with_backoff(
        "stock/lobbying",
        {"symbol": symbol, "from": start_date, "to": end_date},
    )
    if not data:
        return None
    rows = data if isinstance(data, list) else []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns=LOBBYING_RENAME)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_usa_spending(symbol, start_date, end_date):
    """US government contract wins for a company."""
    data = get_with_backoff(
        "stock/usa-spending",
        {"symbol": symbol, "from": start_date, "to": end_date},
    )
    if not data:
        return None
    rows = data if isinstance(data, list) else []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns=USA_SPENDING_RENAME)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_uspto_patents(symbol, start_date, end_date):
    """USPTO patent applications (up to 250 per call)."""
    data = get_with_backoff(
        "stock/uspto-patent",
        {"symbol": symbol, "from": start_date, "to": end_date},
    )
    if not data:
        return None
    rows = data if isinstance(data, list) else []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


def fetch_visa_applications(symbol, start_date, end_date):
    """H1-B and permanent visa applications filed by a company."""
    data = get_with_backoff(
        "stock/visa-application",
        {"symbol": symbol, "from": start_date, "to": end_date},
    )
    if not data:
        return None
    rows = data if isinstance(data, list) else []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    df["fetched_at"] = _now_iso()
    return df


# ============================================================
#  Market-wide fetchers (single request each)
# ============================================================

def fetch_economic_calendar(start_date, end_date):
    """Macro economic calendar events (GDP, CPI, FOMC, etc.)."""
    data = get_with_backoff(
        "economic-calendar",
        {"from": start_date, "to": end_date},
    )
    if not data:
        return None
    rows = data if isinstance(data, list) else []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["fetched_at"] = _now_iso()
    return df


# ============================================================
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Finnhub Expansion Pipeline -- ESG, congress, supply chain, sentiment, filings"
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
    start_str = _fmt(today - datetime.timedelta(days=LOOKBACK[mode]))
    end_str = _fmt(today)

    symbols = get_dji_symbols()
    total = len(symbols)

    # Accumulators for per-symbol feeds
    collected = {
        "esg": [],
        "congressional_trading": [],
        "supply_chain": [],
        "insider_sentiment": [],
        "social_sentiment": [],
        "sec_filings": [],
        "earnings_quality": [],
        "lobbying": [],
        "usa_spending": [],
        "uspto_patents": [],
        "visa_applications": [],
    }

    print(f"[finnhub_expansion] {mode}: {start_str} -> {end_str} ({total} symbols)")
    print("=" * 60)

    for i, symbol in enumerate(symbols, 1):
        print(f"\n[{i}/{total}] {symbol}")

        # 1. ESG
        df = fetch_esg(symbol)
        if df is not None:
            collected["esg"].append(df)
            print(f"  + esg ({len(df)} rows)")

        # 2. Congressional Trading
        df = fetch_congressional_trading(symbol, start_str, end_str)
        if df is not None:
            collected["congressional_trading"].append(df)
            print(f"  + congressional_trading ({len(df)} rows)")

        # 3. Supply Chain
        df = fetch_supply_chain(symbol)
        if df is not None:
            collected["supply_chain"].append(df)
            print(f"  + supply_chain ({len(df)} rows)")

        # 4. Insider Sentiment
        df = fetch_insider_sentiment(symbol, start_str, end_str)
        if df is not None:
            collected["insider_sentiment"].append(df)
            print(f"  + insider_sentiment ({len(df)} rows)")

        # 5. Social Sentiment
        df = fetch_social_sentiment(symbol, start_str, end_str)
        if df is not None:
            collected["social_sentiment"].append(df)
            print(f"  + social_sentiment ({len(df)} rows)")

        # 6. SEC Filings
        df = fetch_sec_filings(symbol, start_str, end_str)
        if df is not None:
            collected["sec_filings"].append(df)
            print(f"  + sec_filings ({len(df)} rows)")

        # 7. Earnings Quality
        df = fetch_earnings_quality(symbol)
        if df is not None:
            collected["earnings_quality"].append(df)
            print(f"  + earnings_quality ({len(df)} rows)")

        # 8. Lobbying
        df = fetch_lobbying(symbol, start_str, end_str)
        if df is not None:
            collected["lobbying"].append(df)
            print(f"  + lobbying ({len(df)} rows)")

        # 9. USA Spending
        df = fetch_usa_spending(symbol, start_str, end_str)
        if df is not None:
            collected["usa_spending"].append(df)
            print(f"  + usa_spending ({len(df)} rows)")

        # 10. USPTO Patents
        df = fetch_uspto_patents(symbol, start_str, end_str)
        if df is not None:
            collected["uspto_patents"].append(df)
            print(f"  + uspto_patents ({len(df)} rows)")

        # 11. Visa Applications
        df = fetch_visa_applications(symbol, start_str, end_str)
        if df is not None:
            collected["visa_applications"].append(df)
            print(f"  + visa_applications ({len(df)} rows)")

    # ============================================================
    #  Market-wide: Economic Calendar (single request)
    # ============================================================
    print("\n[market-wide] economic_calendar...")
    eco_df = fetch_economic_calendar(start_str, end_str)
    if eco_df is not None:
        print(f"  + economic_calendar ({len(eco_df)} rows)")
    else:
        print("  No economic calendar data returned.")

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
            print(f"  Saved {key} -> {out_path} ({len(combined):,} rows, "
                  f"{combined['symbol'].nunique()} symbols)")
        else:
            print(f"  Warning: No data collected for {key}")

    # Economic calendar is market-wide, not per-symbol
    if eco_df is not None:
        filename = f"economic_calendar_{mode}_{today_str}.parquet"
        out_path = write_partitioned(eco_df, DIRS["economic_calendar"], filename)
        print(f"  Saved economic_calendar -> {out_path} ({len(eco_df):,} rows)")

    print("\n--- PIPELINE RUN COMPLETE ---")


if __name__ == "__main__":
    main()
