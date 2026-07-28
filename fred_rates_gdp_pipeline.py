"""
FRED Rates, GDP & Money Supply Pipeline -- interest rates, yield curve,
money supply, GDP, inflation, mortgage rates, commodity prices,
exchange rates, financial markets, and federal debt.

Extends fred_macro_pipeline.py with ~60 new FRED series covering
critical macro data gaps not in the existing pipeline. All series
use the existing FRED_API_KEY.

CLI:
  python fred_rates_gdp_pipeline.py             # incremental (last 90 days)
  python fred_rates_gdp_pipeline.py --backfill  # full available history

Output:
  storage/raw/fred_rates_gdp/interest_rates/fred_rates_gdp_interest_rates_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_rates_gdp/money_supply/fred_rates_gdp_money_supply_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_rates_gdp/gdp/fred_rates_gdp_gdp_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_rates_gdp/inflation/fred_rates_gdp_inflation_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_rates_gdp/mortgage/fred_rates_gdp_mortgage_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_rates_gdp/commodities/fred_rates_gdp_commodities_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_rates_gdp/exchange_rates/fred_rates_gdp_exchange_rates_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_rates_gdp/markets/fred_rates_gdp_markets_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_rates_gdp/federal_debt/fred_rates_gdp_federal_debt_{mode}_{YYYYMMDD}.parquet
  storage/raw/fred_rates_gdp/labor/fred_rates_gdp_labor_{mode}_{YYYYMMDD}.parquet
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

BASE_DIR = os.path.join("storage", "raw", "fred_rates_gdp")

REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3
BACKOFF_SECONDS = 60

# ---------------------------------------------------------------------------
# Series catalog -- grouped by sub-category
# Each entry: (series_id, name, frequency, unit, sub_category)
# ---------------------------------------------------------------------------

SERIES = {
    # ── Interest Rates -- Treasury Yields ────────────────────────────────────
    "DFF":             ("Federal Funds Effective Rate (Daily)",            "daily",    "Percent",             "interest_rates"),
    "DGS3":            ("3-Year Treasury Constant Maturity Rate",          "daily",    "Percent",             "interest_rates"),
    "TB3MS":           ("3-Month Treasury Bill Rate",                      "monthly",  "Percent",             "interest_rates"),
    "TB6MS":           ("6-Month Treasury Bill Rate",                      "monthly",  "Percent",             "interest_rates"),
    "DISRATE":         ("Discount Rate",                                   "monthly",  "Percent",             "interest_rates"),

    # ── Yield Curve Spreads ─────────────────────────────────────────────────
    "T10YFF":          ("10Y Treasury - Fed Funds Spread",                 "daily",    "Percent",             "interest_rates"),
    "GS10":            ("10-Year Treasury (Monthly)",                      "monthly",  "Percent",             "interest_rates"),
    "GS2":             ("2-Year Treasury (Monthly)",                       "monthly",  "Percent",             "interest_rates"),
    "GS5":             ("5-Year Treasury (Monthly)",                       "monthly",  "Percent",             "interest_rates"),
    "GS30":            ("30-Year Treasury (Monthly)",                      "monthly",  "Percent",             "interest_rates"),
    "GS1":             ("1-Year Treasury (Monthly)",                       "monthly",  "Percent",             "interest_rates"),
    "GS3":             ("3-Year Treasury (Monthly)",                       "monthly",  "Percent",             "interest_rates"),

    # ── Money Supply ────────────────────────────────────────────────────────
    "M1SL":            ("M1 Money Stock",                                  "monthly",  "Billions of USD",     "money_supply"),
    "M1V":             ("M1 Velocity",                                     "quarterly","Ratio",               "money_supply"),
    "MZM":             ("Money Zero Maturity",                             "monthly",  "Billions of USD",     "money_supply"),
    "BOGMBASEW":       ("Monetary Base (Weekly)",                          "weekly",   "Millions of USD",     "money_supply"),
    "WALCL":           ("Fed Total Assets",                                "weekly",   "Millions of USD",     "money_supply"),
    "TOTRESNS":        ("Total Reserves of Depository Institutions",       "monthly",  "Billions of USD",     "money_supply"),
    "EXCSRESNS":       ("Excess Reserves",                                 "monthly",  "Billions of USD",     "money_supply"),

    # ── GDP ─────────────────────────────────────────────────────────────────
    "GDPPOT":          ("Real Potential GDP",                              "quarterly","Billions of Chained 2017 USD", "gdp"),
    "GDPDEF":          ("GDP Implicit Price Deflator",                     "quarterly","Index (2017=100)",    "gdp"),
    "GPDI":            ("Gross Private Domestic Investment",               "quarterly","Billions of USD",     "gdp"),
    "NETEXP":          ("Net Exports of Goods and Services",               "quarterly","Billions of USD",     "gdp"),
    "GCE":             ("Government Consumption Expenditures",             "quarterly","Billions of USD",     "gdp"),

    # ── Inflation -- CPI ────────────────────────────────────────────────────
    "CPILFESL":        ("CPI All Items Less Food and Energy",              "monthly",  "Index (1982-84=100)", "inflation"),
    "CPIAUCNS":        ("CPI All Items (Not Seasonally Adjusted)",         "monthly",  "Index (1982-84=100)", "inflation"),
    "CPIAPPSL":        ("CPI: Apparel",                                    "monthly",  "Index (1982-84=100)", "inflation"),
    "CPIMEDSL":        ("CPI: Medical Care",                               "monthly",  "Index (1982-84=100)", "inflation"),
    "PPIFGS":          ("PPI: Final Goods",                                "monthly",  "Index (1982=100)",    "inflation"),
    "PPIIDC":          ("PPI: Industrial Commodities",                     "monthly",  "Index (1982=100)",    "inflation"),

    # ── Mortgage Rates ──────────────────────────────────────────────────────
    "MORTGAGE30US":    ("30-Year Fixed Rate Mortgage Average",             "weekly",   "Percent",             "mortgage"),
    "MORTGAGE15US":    ("15-Year Fixed Rate Mortgage Average",             "weekly",   "Percent",             "mortgage"),

    # ── Commodity Prices ────────────────────────────────────────────────────
    "MCOILWTICO":      ("WTI Crude Oil Price (Monthly)",                   "monthly",  "Dollars per Barrel",  "commodities"),
    "GOLDAMGBD228NLBM":("Gold Price (London Fix, Daily)",                  "daily",    "Troy Ounces",         "commodities"),
    "DHHNGWPUSDM":     ("Henry Hub Natural Gas Spot Price",                "monthly",  "Dollars per MMBtu",   "commodities"),

    # ── Exchange Rates ──────────────────────────────────────────────────────
    "DTWEXB":          ("Trade Weighted USD Index: Broad (Monthly)",       "monthly",  "Index (Mar 1973=100)","exchange_rates"),
    "DEXUSEU":         ("USD/EUR Exchange Rate",                           "daily",    "USD per EUR",         "exchange_rates"),
    "DEXJPUS":         ("JPY/USD Exchange Rate",                           "daily",    "JPY per USD",         "exchange_rates"),
    "DEXCHUS":         ("CNY/USD Exchange Rate",                           "daily",    "CNY per USD",         "exchange_rates"),
    "DEXUSUK":         ("USD/GBP Exchange Rate",                           "daily",    "USD per GBP",         "exchange_rates"),
    "DEXCAUS":         ("CAD/USD Exchange Rate",                           "daily",    "CAD per USD",         "exchange_rates"),
    "DEXSZUS":         ("CHF/USD Exchange Rate",                           "daily",    "CHF per USD",         "exchange_rates"),

    # ── Financial Markets ───────────────────────────────────────────────────
    "SP500":           ("S&P 500 Index",                                   "daily",    "Index Level",         "markets"),
    "DJIA":            ("Dow Jones Industrial Average",                    "daily",    "Index Level",         "markets"),
    "NASDAQCOM":       ("NASDAQ Composite Index",                          "daily",    "Index Level",         "markets"),
    "BAA10Y":          ("Moody's Seasoned Baa Corporate Bond Yield",       "monthly",  "Percent",             "markets"),
    "AAA10Y":          ("Moody's Seasoned Aaa Corporate Bond Yield",       "monthly",  "Percent",             "markets"),
    "NFCI":            ("Chicago Fed National Financial Conditions Index",  "weekly",   "Index",               "markets"),

    # ── Federal Debt ────────────────────────────────────────────────────────
    "GFDEBT":          ("Federal Debt: Total Public Debt",                 "monthly",  "Millions of USD",     "federal_debt"),
    "GFDEGDQ188S":     ("Federal Debt Held by Public: % of GDP",           "quarterly","Percent",             "federal_debt"),
    "FGFEXPND":        ("Federal Government Expenditures",                 "monthly",  "Billions of USD",     "federal_debt"),
    "FGRECPT":         ("Federal Government Receipts",                     "monthly",  "Billions of USD",     "federal_debt"),

    # ── Recession Indicator ─────────────────────────────────────────────────
    "USREC":           ("NBER Recession Indicator",                        "monthly",  "Binary (0/1)",        "interest_rates"),

    # ── Labor Market ────────────────────────────────────────────────────────
    "PAYEMS":          ("Nonfarm Payrolls",                                 "monthly",  "Thousands of Persons","labor"),
    "ICSA":            ("Initial Jobless Claims",                          "weekly",   "Number",              "labor"),
    "CCSA":            ("Continued Jobless Claims",                        "weekly",   "Number",              "labor"),
    "CIVPART":         ("Labor Force Participation Rate",                  "monthly",  "Percent",             "labor"),
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
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    df["date"] = pd.to_datetime(df["date"])
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill=False):
    sub_categories = sorted({s[3] for s in SERIES.values()})
    for sub_cat in sub_categories:
        os.makedirs(os.path.join(BASE_DIR, sub_cat), exist_ok=True)

    if backfill:
        observation_start = None
        print("Mode: BACKFILL (full history)")
    else:
        observation_start = (datetime.datetime.utcnow() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
        print(f"Mode: INCREMENTAL (from {observation_start})")

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
        filename = f"fred_rates_gdp_{sub_cat}_{mode_tag}_{today}.parquet"
        path = write_partitioned(out_df, cat_dir, filename)
        n_series = out_df["series_id"].nunique()
        total_rows += len(out_df)
        total_series += n_series
        print(f"  {sub_cat:20s} -> {path} ({len(out_df):,} rows, {n_series} series)")

    print(f"\nTotal: {total_rows:,} rows across {total_series} series in {len(sub_categories)} categories")

    if failed:
        print(f"\nFailed/empty ({len(failed)}): {', '.join(failed)}")

    print("\n--- FRED RATES, GDP & MONEY SUPPLY PIPELINE COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FRED Rates, GDP & Money Supply Pipeline")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch full available history for all series (use on first run).",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
