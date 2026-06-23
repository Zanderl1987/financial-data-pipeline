"""
USDA NASS Pipeline — Crop production statistics and fertilizer prices.

Uses the USDA National Agricultural Statistics Service QuickStats API.
Register free at https://quickstats.nass.usda.gov/api to get your key.
Add USDA_NASS_API_KEY to your .env file.

Outputs:
  storage/raw/usda/crops/usda_crops_{mode}_{YYYYMMDD}.parquet      (CATALOG: usda_crops)
  storage/raw/usda/fertilizers/usda_fertilizers_{mode}_{YYYYMMDD}.parquet (CATALOG: usda_fertilizers)

Usage:
  python usda_pipeline.py              # incremental (last 5 years)
  python usda_pipeline.py --backfill   # full history from 2000
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

USDA_API_KEY   = os.getenv("USDA_NASS_API_KEY", "")
USDA_API_KEY_2 = os.getenv("USDA_NASS_API_KEY_2", "")
USDA_BASE = "https://quickstats.nass.usda.gov/api/api_GET/"
USDA_DIR = os.path.join("storage", "raw", "usda")

REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3
BACKOFF_SECONDS = 30
BACKFILL_START_YEAR = 2000
INCREMENTAL_YEARS = 5

# Major US field crops — annual national production statistics
FIELD_CROPS = [
    "CORN",
    "SOYBEANS",
    "WHEAT",
    "COTTON",
    "RICE",
    "SORGHUM",
    "BARLEY",
    "OATS",
]

# Fertilizer input cost commodities tracked in USDA Prices Paid survey
FERTILIZER_COMMODITIES = [
    "ANHYDROUS AMMONIA",
    "DAP",
    "UREA",
    "POTASH",
]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_with_backoff(params: dict) -> dict | None:
    keys = [k for k in [USDA_API_KEY, USDA_API_KEY_2] if k]
    key_idx = 0
    params["key"] = keys[key_idx]
    attempt = 0
    while attempt < MAX_RETRIES:
        attempt += 1
        try:
            r = requests.get(USDA_BASE, params=params, timeout=60)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception as e:
                    print(f"  JSON parse error: {e}")
                    return None
            if r.status_code == 401 and key_idx + 1 < len(keys):
                key_idx += 1
                params["key"] = keys[key_idx]
                print(f"  401 unauthorized -- switching to backup API key")
                attempt -= 1  # don't count key rotation as a retry
                continue
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 rate limit -- backing off {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code}: {r.text[:200]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts.")
    return None


def _api_error(data: dict) -> str | None:
    """Return error message if the API returned an error response, else None."""
    if isinstance(data, dict) and "error" in data:
        errors = data["error"]
        return str(errors[0]) if errors else "unknown error"
    return None


# ---------------------------------------------------------------------------
# Crop production fetch
# ---------------------------------------------------------------------------

def fetch_crops(start_year: int, end_year: int) -> pd.DataFrame:
    """Fetch annual national crop production statistics for all field crops."""
    frames = []
    for crop in FIELD_CROPS:
        params = {
            "key": USDA_API_KEY,
            "source_desc": "SURVEY",
            "sector_desc": "CROPS",
            "group_desc": "FIELD CROPS",
            "commodity_desc": crop,
            "agg_level_desc": "NATIONAL",
            "freq_desc": "ANNUAL",
            "year__GE": str(start_year),
            "year__LE": str(end_year),
            "format": "JSON",
        }
        print(f"  {crop}...", end=" ", flush=True)
        data = _get_with_backoff(params)
        if data is None:
            print("request failed")
        elif _api_error(data):
            print(f"API error: {_api_error(data)}")
        elif "data" in data and data["data"]:
            frames.append(pd.DataFrame(data["data"]))
            print(f"{len(data['data'])} records")
        else:
            print("no data")
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        return pd.DataFrame()
    return _clean_crops(pd.concat(frames, ignore_index=True))


def _clean_crops(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.lower().strip() for c in df.columns]

    rename = {
        "commodity_desc":    "commodity",
        "statisticcat_desc": "stat_category",
        "short_desc":        "description",
        "unit_desc":         "unit",
        "agg_level_desc":    "agg_level",
        "reference_period_desc": "period",
        "value":             "value_raw",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    # USDA uses commas in numbers and "(D)"/"(NA)" for suppressed cells
    if "value_raw" in df.columns:
        df["value"] = pd.to_numeric(
            df["value_raw"].astype(str).str.replace(",", "", regex=False).str.strip(),
            errors="coerce",
        )

    if "year" in df.columns:
        df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
        df["date"] = pd.to_datetime(
            df["year"].dropna().astype(int).astype(str) + "-01-01",
            errors="coerce",
        )

    keep = ["commodity", "stat_category", "description", "unit", "year", "date", "period", "agg_level", "value"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["value"])
    df["source"] = "USDA NASS QuickStats"
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df.sort_values(["commodity", "year"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Fertilizer price fetch
# ---------------------------------------------------------------------------

def fetch_fertilizers(start_year: int, end_year: int) -> pd.DataFrame:
    """Fetch monthly fertilizer prices paid by farmers (USDA Prices Paid survey)."""
    frames = []
    for fert in FERTILIZER_COMMODITIES:
        params = {
            "key": USDA_API_KEY,
            "source_desc": "SURVEY",
            "sector_desc": "ECONOMICS",
            "group_desc": "PRICES PAID",
            "commodity_desc": fert,
            "agg_level_desc": "NATIONAL",
            "year__GE": str(start_year),
            "year__LE": str(end_year),
            "format": "JSON",
        }
        print(f"  {fert}...", end=" ", flush=True)
        data = _get_with_backoff(params)
        if data is None:
            print("request failed")
        elif _api_error(data):
            print(f"API error: {_api_error(data)}")
        elif "data" in data and data["data"]:
            frames.append(pd.DataFrame(data["data"]))
            print(f"{len(data['data'])} records")
        else:
            print("no data")
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        return pd.DataFrame()
    return _clean_fertilizers(pd.concat(frames, ignore_index=True))


_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    "YEAR": 6,  # Annual survey — anchor to mid-year
}


def _clean_fertilizers(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.lower().strip() for c in df.columns]

    rename = {
        "commodity_desc":    "commodity",
        "statisticcat_desc": "stat_category",
        "short_desc":        "description",
        "unit_desc":         "unit",
        "reference_period_desc": "period",
        "freq_desc":         "frequency",
        "value":             "value_raw",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    if "value_raw" in df.columns:
        df["value"] = pd.to_numeric(
            df["value_raw"].astype(str).str.replace(",", "", regex=False).str.strip(),
            errors="coerce",
        )

    if "year" in df.columns and "period" in df.columns:
        df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
        df["month_num"] = df["period"].str.strip().str.upper().map(_MONTH_MAP).fillna(1).astype(int)
        df["date"] = pd.to_datetime(
            df["year"].dropna().astype(int).astype(str) + "-"
            + df["month_num"].astype(str).str.zfill(2) + "-01",
            errors="coerce",
        )

    keep = ["commodity", "stat_category", "description", "unit", "year", "date", "period", "frequency", "value"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["value"])
    df["source"] = "USDA NASS QuickStats"
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df.sort_values(["commodity", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill: bool = False) -> None:
    if not USDA_API_KEY and not USDA_API_KEY_2:
        print("ERROR: No USDA API key found (USDA_NASS_API_KEY or USDA_NASS_API_KEY_2).")
        print("  Register free at https://quickstats.nass.usda.gov/api")
        return

    os.makedirs(os.path.join(USDA_DIR, "crops"), exist_ok=True)
    os.makedirs(os.path.join(USDA_DIR, "fertilizers"), exist_ok=True)

    now = datetime.datetime.utcnow()
    today = now.strftime("%Y%m%d")

    if backfill:
        start_year = BACKFILL_START_YEAR
        mode_tag = "backfill"
        print(f"Mode: BACKFILL ({start_year}-{now.year})")
    else:
        start_year = now.year - INCREMENTAL_YEARS
        mode_tag = "incremental"
        print(f"Mode: INCREMENTAL ({start_year}-{now.year})")

    # ── Crops ──────────────────────────────────────────────────────────────────
    print(f"\n--- Field Crops ({', '.join(FIELD_CROPS)}) ---")
    df_crops = fetch_crops(start_year, now.year)
    if not df_crops.empty:
        path = write_partitioned(
            df_crops,
            os.path.join(USDA_DIR, "crops"),
            f"usda_crops_{mode_tag}_{today}.parquet",
        )
        commodities = df_crops["commodity"].nunique() if "commodity" in df_crops.columns else "?"
        print(f"\n[+] {path}")
        print(f"    {len(df_crops):,} rows | {commodities} commodities")
    else:
        print("\n[!] No crop data returned. Check USDA_NASS_API_KEY and API availability.")

    # ── Fertilizers ────────────────────────────────────────────────────────────
    print(f"\n--- Fertilizer Prices ({', '.join(FERTILIZER_COMMODITIES)}) ---")
    df_fert = fetch_fertilizers(start_year, now.year)
    if not df_fert.empty:
        path = write_partitioned(
            df_fert,
            os.path.join(USDA_DIR, "fertilizers"),
            f"usda_fertilizers_{mode_tag}_{today}.parquet",
        )
        ferts = df_fert["commodity"].nunique() if "commodity" in df_fert.columns else "?"
        date_min = df_fert["date"].min().strftime("%Y-%m") if "date" in df_fert.columns else "?"
        date_max = df_fert["date"].max().strftime("%Y-%m") if "date" in df_fert.columns else "?"
        print(f"\n[+] {path}")
        print(f"    {len(df_fert):,} rows | {ferts} fertilizers | {date_min} to {date_max}")
    else:
        print("\n[!] No fertilizer data returned.")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="USDA NASS pipeline -- crop production statistics and fertilizer prices"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=f"Fetch full history from {BACKFILL_START_YEAR}. Default: last {INCREMENTAL_YEARS} years.",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
