"""
US International Trade Pipeline — Monthly agricultural imports and exports by HTS chapter.

Uses the US Census Bureau International Trade time-series API.
Register free at https://api.census.gov/data/key_signup.html to get a key.
Add CENSUS_API_KEY to your .env file.

Covers major agricultural HTS chapters (world totals):
  Chapter 10 -- Cereals (wheat, corn, rice, barley)
  Chapter 12 -- Oilseeds (soybeans, rapeseed, sunflower)
  Chapter 15 -- Animal/vegetable fats and oils (palm oil, soybean oil)
  Chapter 23 -- Food industry residues and feed (soybean meal, distillers grains)
  Chapter 31 -- Fertilizers (urea, ammonia, potash, DAP)

Backfill fetches December year-to-date totals per year (= annual trade volumes).
Incremental fetches monthly values for the last 24 months.

Outputs:
  storage/raw/trade/imports/us_imports_hs_{mode}_{YYYYMMDD}.parquet  (CATALOG: us_imports_hs)
  storage/raw/trade/exports/us_exports_hs_{mode}_{YYYYMMDD}.parquet  (CATALOG: us_exports_hs)

Usage:
  python trade_pipeline.py              # incremental (last 24 months, monthly)
  python trade_pipeline.py --backfill   # full history from 2010 (annual totals)
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

CENSUS_API_KEY = os.getenv("CENSUS_API_KEY", "")
IMPORTS_BASE = "https://api.census.gov/data/timeseries/intltrade/imports/hs"
EXPORTS_BASE = "https://api.census.gov/data/timeseries/intltrade/exports/hs"
TRADE_DIR = os.path.join("storage", "raw", "trade")

REQUEST_INTERVAL = 0.3
MAX_RETRIES = 3
BACKOFF_SECONDS = 30
BACKFILL_START_YEAR = 2010
INCREMENTAL_MONTHS = 24

# World total country code (all trading partners combined)
WORLD_CTY = "0000"

# Agricultural HTS 2-digit chapters and their descriptions
AG_CHAPTERS: dict[str, str] = {
    "10": "Cereals",
    "12": "Oilseeds",
    "15": "Fats and oils",
    "23": "Feed and residues",
    "31": "Fertilizers",
}

# Variables to retrieve from the Census imports API
IMPORT_VARS = ",".join([
    "GEN_VAL_MO",    # general imports value, current month ($)
    "GEN_VAL_YR",    # general imports value, year-to-date ($)
    "GEN_QY1_MO",    # general imports quantity, current month
    "GEN_QY1_YR",    # general imports quantity, year-to-date
    "I_COMMODITY_LDESC",  # commodity long description
])

# Variables to retrieve from the Census exports API
EXPORT_VARS = ",".join([
    "ALL_VAL_MO",    # all exports value, current month ($)
    "ALL_VAL_YR",    # all exports value, year-to-date ($)
    "QTY_1_MO",      # export quantity, current month
    "QTY_1_YR",      # export quantity, year-to-date
    "E_COMMODITY_LDESC",  # commodity long description
])


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_with_backoff(url: str, params: dict) -> list | None:
    """GET a Census API endpoint; returns the parsed JSON array or None."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception as e:
                    print(f"  JSON parse error: {e}")
                    return None
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 rate limit -- backing off {wait}s")
                time.sleep(wait)
            elif r.status_code == 204:
                return []   # no content
            else:
                print(f"  HTTP {r.status_code}: {r.text[:200]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts.")
    return None


def _json_to_df(data: list) -> pd.DataFrame:
    """Convert Census API array response (header row + data rows) to DataFrame."""
    if not data or len(data) < 2:
        return pd.DataFrame()
    headers = [h.lower() for h in data[0]]
    return pd.DataFrame(data[1:], columns=headers)


# ---------------------------------------------------------------------------
# Date range helpers
# ---------------------------------------------------------------------------

def _backfill_periods(start_year: int, end_year: int) -> list[tuple[int, int]]:
    """Return (year, 12) pairs — December = annual YTD total for each year."""
    now = datetime.datetime.utcnow()
    periods = []
    for y in range(start_year, end_year + 1):
        month = now.month - 1 if y == now.year else 12
        if month < 1:
            month = 12
            y -= 1
        periods.append((y, month))
    return periods


def _incremental_periods(n_months: int) -> list[tuple[int, int]]:
    """Return the last n_months (year, month) pairs ending last month."""
    now = datetime.datetime.utcnow()
    end_year, end_month = now.year, now.month - 1
    if end_month == 0:
        end_year -= 1
        end_month = 12
    periods = []
    y, m = end_year, end_month
    for _ in range(n_months):
        periods.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(periods))


# ---------------------------------------------------------------------------
# Fetch one (year, month) for all chapters — imports
# ---------------------------------------------------------------------------

def _fetch_imports_period(year: int, month: int) -> pd.DataFrame:
    frames = []
    for chapter, chapter_name in AG_CHAPTERS.items():
        params = {
            "get": IMPORT_VARS,
            "YEAR": str(year),
            "MONTH": f"{month:02d}",
            "COMM_LVL": "HS2",
            "I_COMMODITY": chapter,
            "CTY_CODE": WORLD_CTY,
            "key": CENSUS_API_KEY,
        }
        data = _get_with_backoff(IMPORTS_BASE, params)
        if data:
            df = _json_to_df(data)
            if not df.empty:
                df["hs2_code"] = chapter
                df["hs2_desc"] = chapter_name
                frames.append(df)
        time.sleep(REQUEST_INTERVAL)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_imports(periods: list[tuple[int, int]]) -> pd.DataFrame:
    """Fetch import data for a list of (year, month) periods."""
    frames = []
    for year, month in periods:
        print(f"  imports {year}-{month:02d}...", end=" ", flush=True)
        df = _fetch_imports_period(year, month)
        if df.empty:
            print("no data")
        else:
            frames.append(df)
            print(f"{len(df)} rows")
    if not frames:
        return pd.DataFrame()
    return _clean_imports(pd.concat(frames, ignore_index=True))


def _clean_imports(df: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "gen_val_mo": "value_mo_usd",
        "gen_val_yr": "value_ytd_usd",
        "gen_qy1_mo": "qty_mo",
        "gen_qy1_yr": "qty_ytd",
        "i_commodity": "hs2_code",
        "i_commodity_ldesc": "commodity_desc",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    for col in ["value_mo_usd", "value_ytd_usd", "qty_mo", "qty_ytd"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "year" in df.columns and "month" in df.columns:
        df["date"] = pd.to_datetime(
            df["year"].astype(str) + "-" + df["month"].astype(str).str.zfill(2) + "-01",
            errors="coerce",
        )
    df["direction"] = "imports"
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    df = df.dropna(subset=["date"])
    return df.sort_values(["hs2_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Fetch one (year, month) for all chapters — exports
# ---------------------------------------------------------------------------

def _fetch_exports_period(year: int, month: int) -> pd.DataFrame:
    frames = []
    for chapter, chapter_name in AG_CHAPTERS.items():
        params = {
            "get": EXPORT_VARS,
            "YEAR": str(year),
            "MONTH": f"{month:02d}",
            "COMM_LVL": "HS2",
            "E_COMMODITY": chapter,
            "CTY_CODE": WORLD_CTY,
            "key": CENSUS_API_KEY,
        }
        data = _get_with_backoff(EXPORTS_BASE, params)
        if data:
            df = _json_to_df(data)
            if not df.empty:
                df["hs2_code"] = chapter
                df["hs2_desc"] = chapter_name
                frames.append(df)
        time.sleep(REQUEST_INTERVAL)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_exports(periods: list[tuple[int, int]]) -> pd.DataFrame:
    """Fetch export data for a list of (year, month) periods."""
    frames = []
    for year, month in periods:
        print(f"  exports {year}-{month:02d}...", end=" ", flush=True)
        df = _fetch_exports_period(year, month)
        if df.empty:
            print("no data")
        else:
            frames.append(df)
            print(f"{len(df)} rows")
    if not frames:
        return pd.DataFrame()
    return _clean_exports(pd.concat(frames, ignore_index=True))


def _clean_exports(df: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "all_val_mo": "value_mo_usd",
        "all_val_yr": "value_ytd_usd",
        "qty_1_mo":   "qty_mo",
        "qty_1_yr":   "qty_ytd",
        "e_commodity": "hs2_code",
        "e_commodity_ldesc": "commodity_desc",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    for col in ["value_mo_usd", "value_ytd_usd", "qty_mo", "qty_ytd"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "year" in df.columns and "month" in df.columns:
        df["date"] = pd.to_datetime(
            df["year"].astype(str) + "-" + df["month"].astype(str).str.zfill(2) + "-01",
            errors="coerce",
        )
    df["direction"] = "exports"
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    df = df.dropna(subset=["date"])
    return df.sort_values(["hs2_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill: bool = False) -> None:
    if not CENSUS_API_KEY:
        print("ERROR: CENSUS_API_KEY not set.")
        print("  Register free at https://api.census.gov/data/key_signup.html")
        print("  Then add CENSUS_API_KEY=<your_key> to your .env file.")
        return

    os.makedirs(os.path.join(TRADE_DIR, "imports"), exist_ok=True)
    os.makedirs(os.path.join(TRADE_DIR, "exports"), exist_ok=True)

    now = datetime.datetime.utcnow()
    today = now.strftime("%Y%m%d")

    if backfill:
        periods = _backfill_periods(BACKFILL_START_YEAR, now.year)
        mode_tag = "backfill"
        print(f"Mode: BACKFILL ({BACKFILL_START_YEAR}-{now.year}, annual totals via December YTD)")
    else:
        periods = _incremental_periods(INCREMENTAL_MONTHS)
        mode_tag = "incremental"
        print(f"Mode: INCREMENTAL (last {INCREMENTAL_MONTHS} months)")

    chapters_str = ", ".join(f"{k} ({v})" for k, v in AG_CHAPTERS.items())
    print(f"Chapters: {chapters_str}")
    print(f"Periods:  {len(periods)}")
    print()

    # ── Imports ────────────────────────────────────────────────────────────────
    print("--- US Agricultural Imports ---")
    df_imports = fetch_imports(periods)
    if not df_imports.empty:
        path = write_partitioned(
            df_imports,
            os.path.join(TRADE_DIR, "imports"),
            f"us_imports_hs_{mode_tag}_{today}.parquet",
        )
        date_min = df_imports["date"].min().strftime("%Y-%m") if "date" in df_imports.columns else "?"
        date_max = df_imports["date"].max().strftime("%Y-%m") if "date" in df_imports.columns else "?"
        print(f"\n[+] {path}")
        print(f"    {len(df_imports):,} rows | {date_min} to {date_max}")
    else:
        print("\n[!] No import data returned. Check CENSUS_API_KEY and API availability.")

    print()

    # ── Exports ────────────────────────────────────────────────────────────────
    print("--- US Agricultural Exports ---")
    df_exports = fetch_exports(periods)
    if not df_exports.empty:
        path = write_partitioned(
            df_exports,
            os.path.join(TRADE_DIR, "exports"),
            f"us_exports_hs_{mode_tag}_{today}.parquet",
        )
        date_min = df_exports["date"].min().strftime("%Y-%m") if "date" in df_exports.columns else "?"
        date_max = df_exports["date"].max().strftime("%Y-%m") if "date" in df_exports.columns else "?"
        print(f"\n[+] {path}")
        print(f"    {len(df_exports):,} rows | {date_min} to {date_max}")
    else:
        print("\n[!] No export data returned.")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="US International Trade pipeline -- agricultural imports and exports by HTS chapter"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=f"Fetch annual totals from {BACKFILL_START_YEAR}. Default: last {INCREMENTAL_MONTHS} months.",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
