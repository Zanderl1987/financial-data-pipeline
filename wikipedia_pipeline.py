#!/usr/bin/env python3
"""
Wikipedia Pageviews Pipeline — daily article traffic for company and sector pages.

Uses the Wikimedia REST API v1 (free, no API key, no account needed).
Endpoint: https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/...

Investor attention on Wikipedia strongly correlates with options implied volatility
and pre-earnings trading volume. Sudden traffic spikes on a company page often
precede significant price moves or media events.

Tracks three article groups:
  1. DJI component company pages (30 stocks)
  2. Sector & market index pages  (S&P 500, Nasdaq, sector names)
  3. Macro concept pages          (Inflation, Recession, Federal Reserve, etc.)

Outputs:
  storage/raw/wikipedia/year=YYYY/month=MM/wikipedia_{mode}_{YYYYMMDD}.parquet
  CATALOG table: wikipedia_pageviews

Usage:
  python wikipedia_pipeline.py             # incremental (last 90 days)
  python wikipedia_pipeline.py --backfill  # full history from 2015-07-01
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests

from storage_utils import write_partitioned

BASE_URL    = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
OUTPUT_DIR  = os.path.join("storage", "raw", "wikipedia")

BACKFILL_START    = "2015-07-01"   # Wikimedia pageview API availability
INCREMENTAL_DAYS  = 90
REQUEST_INTERVAL  = 0.1            # polite; API allows much higher
MAX_RETRIES       = 3

ARTICLES: list[dict] = [
    # ── DJI components ──────────────────────────────────────────────────────────
    {"article": "Apple_Inc.",               "ticker": "AAPL",  "group": "dji"},
    {"article": "Microsoft",                "ticker": "MSFT",  "group": "dji"},
    {"article": "Amazon_(company)",         "ticker": "AMZN",  "group": "dji"},
    {"article": "Nvidia",                   "ticker": "NVDA",  "group": "dji"},
    {"article": "Visa_Inc.",                "ticker": "V",     "group": "dji"},
    {"article": "JPMorgan_Chase",           "ticker": "JPM",   "group": "dji"},
    {"article": "Walmart",                  "ticker": "WMT",   "group": "dji"},
    {"article": "UnitedHealth_Group",       "ticker": "UNH",   "group": "dji"},
    {"article": "Goldman_Sachs",            "ticker": "GS",    "group": "dji"},
    {"article": "Home_Depot",               "ticker": "HD",    "group": "dji"},
    {"article": "Johnson_%26_Johnson",      "ticker": "JNJ",   "group": "dji"},
    {"article": "Procter_%26_Gamble",       "ticker": "PG",    "group": "dji"},
    {"article": "Caterpillar_Inc.",         "ticker": "CAT",   "group": "dji"},
    {"article": "Boeing",                   "ticker": "BA",    "group": "dji"},
    {"article": "McDonald%27s",             "ticker": "MCD",   "group": "dji"},
    {"article": "Salesforce",               "ticker": "CRM",   "group": "dji"},
    {"article": "Chevron_Corporation",      "ticker": "CVX",   "group": "dji"},
    {"article": "American_Express",         "ticker": "AXP",   "group": "dji"},
    {"article": "Honeywell",                "ticker": "HON",   "group": "dji"},
    {"article": "Cisco_Systems",            "ticker": "CSCO",  "group": "dji"},
    {"article": "IBM",                      "ticker": "IBM",   "group": "dji"},
    {"article": "Walt_Disney_Company",      "ticker": "DIS",   "group": "dji"},
    {"article": "Nike,_Inc.",               "ticker": "NKE",   "group": "dji"},
    {"article": "Merck_%26_Co.",            "ticker": "MRK",   "group": "dji"},
    {"article": "Verizon_Communications",   "ticker": "VZ",    "group": "dji"},
    {"article": "Sherwin-Williams",         "ticker": "SHW",   "group": "dji"},
    {"article": "Travelers_Companies",      "ticker": "TRV",   "group": "dji"},
    {"article": "Amgen",                    "ticker": "AMGN",  "group": "dji"},
    {"article": "3M",                       "ticker": "MMM",   "group": "dji"},
    {"article": "Coca-Cola_Company",        "ticker": "KO",    "group": "dji"},
    # ── Sector / Index pages ─────────────────────────────────────────────────────
    {"article": "S%26P_500",                "ticker": None,    "group": "index"},
    {"article": "Nasdaq_Composite",         "ticker": None,    "group": "index"},
    {"article": "Dow_Jones_Industrial_Average", "ticker": None, "group": "index"},
    {"article": "CBOE_Volatility_Index",    "ticker": None,    "group": "index"},
    {"article": "Technology_sector",        "ticker": None,    "group": "sector"},
    {"article": "Energy_industry",          "ticker": None,    "group": "sector"},
    {"article": "Financial_services",       "ticker": None,    "group": "sector"},
    {"article": "Health_care_industry",     "ticker": None,    "group": "sector"},
    # ── Macro concepts ───────────────────────────────────────────────────────────
    {"article": "Inflation",                "ticker": None,    "group": "macro"},
    {"article": "Recession",                "ticker": None,    "group": "macro"},
    {"article": "Federal_Reserve",          "ticker": None,    "group": "macro"},
    {"article": "Interest_rate",            "ticker": None,    "group": "macro"},
    {"article": "Unemployment",             "ticker": None,    "group": "macro"},
    {"article": "Quantitative_easing",      "ticker": None,    "group": "macro"},
    {"article": "Bank_run",                 "ticker": None,    "group": "macro"},
    {"article": "Cryptocurrency",           "ticker": None,    "group": "macro"},
]


def _get_with_retry(url: str) -> dict | None:
    headers = {"User-Agent": "financial-data-pipeline/1.0 (research; contact: github.com/Zanderl1987)"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None   # article not found; skip silently
            if r.status_code == 429:
                time.sleep(60 * attempt)
            else:
                print(f"    HTTP {r.status_code} for {url.split('/')[-3]}")
                return None
        except requests.RequestException as exc:
            print(f"    Error (attempt {attempt}): {exc}")
            time.sleep(10 * attempt)
    return None


def fetch_article(article: dict, start: str, end: str) -> pd.DataFrame | None:
    """Fetch daily pageviews for one article between start/end (YYYYMMDD format)."""
    url = (
        f"{BASE_URL}/en.wikipedia.org/all-access/all-agents"
        f"/{article['article']}/daily/{start}/{end}"
    )
    data = _get_with_retry(url)
    if data is None or "items" not in data:
        return None

    rows = []
    for item in data["items"]:
        ts = item.get("timestamp", "")
        rows.append({
            "article":  article["article"],
            "ticker":   article.get("ticker"),
            "group":    article["group"],
            "date":     f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else None,
            "views":    item.get("views", 0),
        })

    return pd.DataFrame(rows) if rows else None


def main(backfill: bool = False) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    now  = datetime.datetime.utcnow()
    mode = "backfill" if backfill else "incremental"

    if backfill:
        start_dt = datetime.datetime.strptime(BACKFILL_START, "%Y-%m-%d")
    else:
        start_dt = now - datetime.timedelta(days=INCREMENTAL_DAYS)

    # API requires YYYYMMDD00 format
    start_str = start_dt.strftime("%Y%m%d") + "00"
    end_str   = now.strftime("%Y%m%d") + "00"
    today     = now.strftime("%Y%m%d")

    print(f"Wikipedia Pageviews Pipeline  mode={mode}")
    print(f"Date range: {start_dt.strftime('%Y-%m-%d')} -> {now.strftime('%Y-%m-%d')}")
    print(f"Articles:   {len(ARTICLES)}")
    print()

    frames = []
    for art in ARTICLES:
        print(f"  [{art['group']:8s}] {art['article'][:40]}...", end=" ", flush=True)
        df = fetch_article(art, start_str, end_str)
        if df is not None and not df.empty:
            frames.append(df)
            print(f"{len(df):,} rows")
        else:
            print("no data")
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        print("\nNo data returned.")
        return

    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["fetched_at"] = now.isoformat()

    path = write_partitioned(df, OUTPUT_DIR, f"wikipedia_{mode}_{today}.parquet")

    date_min = df["date"].dropna().min().strftime("%Y-%m-%d")
    date_max = df["date"].dropna().max().strftime("%Y-%m-%d")
    print(f"\n[+] {path}")
    print(f"    {len(df):,} rows | {df['article'].nunique()} articles | {date_min} to {date_max}")
    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Wikipedia pageviews pipeline — investor attention signal (keyless)"
    )
    parser.add_argument("--backfill", action="store_true",
                        help=f"Full history from {BACKFILL_START}. Default: last {INCREMENTAL_DAYS} days.")
    args = parser.parse_args()
    main(backfill=args.backfill)
