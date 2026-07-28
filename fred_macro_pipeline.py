"""
FRED Macro Extended Pipeline -- housing, sentiment, industrial production,
retail sales, personal income/spending, PCE inflation, trade balance,
consumer credit, and durable goods.

Extends commodity_macro_pipeline.py with ~37 new FRED series that fill
major macro data gaps. All series use the existing FRED_API_KEY.

CLI:
  python fred_macro_pipeline.py             # incremental (last 90 days)
  python fred_macro_pipeline.py --backfill  # full available history

Output:
  storage/raw/fred_macro/housing/fred_macro_housing_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_macro/sentiment/fred_macro_sentiment_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_macro/industrial/fred_macro_industrial_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_macro/consumer/fred_macro_consumer_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_macro/trade/fred_macro_trade_{mode}_{YYYYMMDD}.parquet
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

FRED_API_KEY = os.environ.get("FRED_API_KEY")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

BASE_DIR = os.path.join("storage", "raw", "fred_macro")

REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3
BACKOFF_SECONDS = 60

# ---------------------------------------------------------------------------
# Series catalog -- grouped by sub-category
# Each entry: (series_id, name, frequency, unit, sub_category)
# ---------------------------------------------------------------------------

SERIES = {
    # ── Housing ────────────────────────────────────────────────────────────
    "HOUST":           ("Housing Starts (thousands)",                  "monthly",  "Thousands of Units",  "housing"),
    "PERMIT":          ("Building Permits (thousands)",                "monthly",  "Thousands of Units",  "housing"),
    "EXHOSLUSM495S":   ("Existing Home Sales (SAAR)",                  "monthly",  "Millions of Units",   "housing"),
    "HSN1F":           ("New Home Sales (SAAR)",                       "monthly",  "Thousands of Units",  "housing"),
    "CSUSHPINSA":      ("Case-Shiller US National Home Price Index",   "monthly",  "Index (Jan 2000=100)","housing"),
    "MSPUS":           ("Median Sales Price of Houses Sold",           "quarterly","Thousands of Dollars","housing"),
    "ASPUS":           ("Average Sales Price of Houses Sold",          "quarterly","Thousands of Dollars","housing"),
    "MSACSR":          ("Monthly Supply of New Houses",                "monthly",  "Months' Supply",       "housing"),

    # ── Consumer Sentiment ─────────────────────────────────────────────────
    "UMCSENT":         ("U Mich Consumer Sentiment",                  "monthly",  "Index (1966=100)",    "sentiment"),
    "UMCSENT_CURR":    ("U Mich Current Conditions",                  "monthly",  "Index (1966=100)",    "sentiment"),
    "UMCSENT_EXP":     ("U Mich Consumer Expectations",               "monthly",  "Index (1966=100)",    "sentiment"),
    "MICH":            ("U Mich 1-Year Inflation Expectation",        "monthly",  "Percent",             "sentiment"),
    "MICH5Y":          ("U Mich 5-Year Inflation Expectation",        "monthly",  "Percent",             "sentiment"),

    # ── Industrial Production ──────────────────────────────────────────────
    "INDPRO":          ("Industrial Production Index",                "monthly",  "Index (2017=100)",    "industrial"),
    "TCU":             ("Capacity Utilization Rate",                  "monthly",  "Percent",             "industrial"),

    # ── Retail Sales ───────────────────────────────────────────────────────
    "RSAFS":           ("Advance Retail Sales: Total",                "monthly",  "Millions of USD",     "consumer"),
    "RSXFS":           ("Advance Retail Sales: Ex Auto",              "monthly",  "Millions of USD",     "consumer"),
    "MRTSSM44100USS":  ("Retail Trade: Motor Vehicle & Parts Dealers","monthly",  "Millions of USD",     "consumer"),

    # ── Personal Income / Spending ─────────────────────────────────────────
    "PI":              ("Personal Income",                            "monthly",  "Billions of USD",     "consumer"),
    "DSPI":            ("Disposable Personal Income",                 "monthly",  "Billions of USD",     "consumer"),
    "PCEC":            ("Personal Consumption Expenditures",          "monthly",  "Billions of USD",     "consumer"),
    "PSAVERT":         ("Personal Saving Rate",                       "monthly",  "Percent",             "consumer"),

    # ── PCE Inflation ──────────────────────────────────────────────────────
    "PCEPI":           ("PCE Price Index (Headline)",                 "monthly",  "Index (2017=100)",    "consumer"),
    "PCEPILFE":        ("PCE Price Index (Core, Ex Food & Energy)",   "monthly",  "Index (2017=100)",    "consumer"),
    "PCETRIM12M159SFRBDAL": ("Dallas Fed Trimmed Mean PCE",           "monthly",  "Percent",             "consumer"),

    # ── Trade Balance ──────────────────────────────────────────────────────
    "BOPGSTB":         ("Trade Balance: Goods & Services",            "monthly",  "Millions of USD",     "trade"),
    "BOPGTB":          ("Trade Balance: Goods",                       "monthly",  "Millions of USD",     "trade"),

    # ── Consumer Credit ────────────────────────────────────────────────────
    "TOTALSL":         ("Total Consumer Credit",                      "monthly",  "Billions of USD",     "consumer"),
    "REVSL":           ("Revolving Consumer Credit",                  "monthly",  "Billions of USD",     "consumer"),
    "NONREVSL":        ("Non-Revolving Consumer Credit",              "monthly",  "Billions of USD",     "consumer"),
    "SLOAS":           ("Student Loans Outstanding",                  "quarterly","Billions of USD",     "consumer"),
    "DRCCLACBS":       ("Consumer Credit Delinquency Rate",           "quarterly","Percent",             "consumer"),

    # ── Durable Goods ──────────────────────────────────────────────────────
    "DGORDER":         ("Durable Goods Orders: Total",               "monthly",  "Millions of USD",     "consumer"),
    "DGORDEREXD":      ("Durable Goods Orders: Ex Defense",          "monthly",  "Millions of USD",     "consumer"),
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def get_with_backoff(url, params):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from FRED. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code} for {params.get('series_id')}: {r.text[:120]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts: {params.get('series_id')}")
    return None


# ---------------------------------------------------------------------------
# Fetch a single FRED series
# ---------------------------------------------------------------------------

def fetch_series(series_id, observation_start=None):
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "asc",
        "limit": 100000,
    }
    if observation_start:
        params["observation_start"] = observation_start

    r = get_with_backoff(FRED_BASE, params)
    if not r:
        return None

    observations = r.json().get("observations", [])
    if not observations:
        return None

    df = pd.DataFrame(observations)[["date", "value"]]
    # FRED encodes missing values as "."
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    df["date"] = pd.to_datetime(df["date"])
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill=False):
    # Create sub-category directories
    sub_categories = sorted({s[3] for s in SERIES.values()})
    for sub_cat in sub_categories:
        os.makedirs(os.path.join(BASE_DIR, sub_cat), exist_ok=True)

    if backfill:
        observation_start = None
        print("Mode: BACKFILL (full history)")
    else:
        observation_start = (datetime.datetime.utcnow() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
        print(f"Mode: INCREMENTAL (from {observation_start})")

    # Group frames by sub-category for separate parquet files
    frames_by_cat: dict[str, list[pd.DataFrame]] = {c: [] for c in sub_categories}
    failed = []

    total = len(SERIES)
    for i, (series_id, (name, frequency, unit, sub_cat)) in enumerate(SERIES.items(), 1):
        print(f"[{i}/{total}] {series_id} -- {name}...")
        df = fetch_series(series_id, observation_start)

        if df is None or df.empty:
            print(f"  No data returned.")
            failed.append(series_id)
            time.sleep(REQUEST_INTERVAL)
            continue

        df["series_id"] = series_id
        df["name"] = name
        df["frequency"] = frequency
        df["unit"] = unit
        df["sub_category"] = sub_cat
        df["fetched_at"] = datetime.datetime.utcnow().isoformat()

        frames_by_cat[sub_cat].append(df)
        time.sleep(REQUEST_INTERVAL)

    today = datetime.datetime.utcnow().strftime("%Y%m%d")
    mode_tag = "backfill" if backfill else "incremental"

    total_rows = 0
    total_series = 0
    for sub_cat, frames in frames_by_cat.items():
        if not frames:
            continue
        out_df = pd.concat(frames, ignore_index=True)
        cat_dir = os.path.join(BASE_DIR, sub_cat)
        filename = f"fred_macro_{sub_cat}_{mode_tag}_{today}.parquet"
        path = write_partitioned(out_df, cat_dir, filename)
        n_series = out_df["series_id"].nunique()
        total_rows += len(out_df)
        total_series += n_series
        print(f"  {sub_cat:20s} -> {path} ({len(out_df):,} rows, {n_series} series)")

    print(f"\nTotal: {total_rows:,} rows across {total_series} series in {len(sub_categories)} categories")

    if failed:
        print(f"\nFailed/empty ({len(failed)}): {', '.join(failed)}")

    print("\n--- FRED MACRO EXTENDED PIPELINE COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FRED Macro Extended Pipeline -- housing, sentiment, industrial, consumer, trade")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch full available history for all series (use on first run).",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
