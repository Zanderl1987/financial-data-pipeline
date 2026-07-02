#!/usr/bin/env python3
"""
FAO FAOSTAT Pipeline — Global Food & Agriculture Statistics.

No API key required. Tries the FAOSTAT REST API first; falls back to
bulk ZIP download if the API is unavailable (fenixservices.fao.org
occasionally returns 521 Cloudflare errors).

Fetches:
  - Production: crop production quantities and harvested area by major country
  - Prices: producer prices in USD per tonne by commodity and country

Major crops tracked: wheat, maize, rice, soybeans, cotton, sugar cane,
cocoa, coffee, palm oil, natural rubber, sunflower seed, rapeseed/canola.

CLI:
  python fao_pipeline.py             # current + 2 prior years (fast)
  python fao_pipeline.py --backfill  # full history from 2000

Outputs:
  storage/raw/fao/production/fao_production_{mode}_{YYYYMMDD}.parquet
  storage/raw/fao/prices/fao_prices_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import io
import os
import time
import zipfile

import pandas as pd
import requests
from storage_utils import write_partitioned

# Try the newer API gateway; fenixservices.fao.org is unstable
FAO_API_URLS = [
    "https://fenixservices.fao.org/faostat/api/v1/en/data",
    "https://faostat3.fao.org/api/v1/en/data",
]
BULK_BASE = "https://bulks-faostat.fao.org/production"
BASE_DIR      = os.path.join("storage", "raw", "fao")
PROD_DIR      = os.path.join(BASE_DIR, "production")
PRICES_DIR    = os.path.join(BASE_DIR, "prices")
REQUEST_INTERVAL = 1.0
MAX_RETRIES      = 3
BACKOFF_SECONDS  = 30

# FAOSTAT area codes — major producing countries + world aggregate
AREAS = {
    "5000": "World",
    "231":  "United States",
    "44":   "China",
    "21":   "Brazil",
    "100":  "India",
    "35":   "Canada",
    "11":   "Argentina",
    "68":   "European Union",
    "185":  "Russian Federation",
    "36":   "Australia",
}

# FAOSTAT item codes — key agricultural commodities
ITEMS_PRODUCTION = {
    "15":  "Wheat",
    "27":  "Rice paddy",
    "44":  "Barley",
    "56":  "Maize",
    "108": "Oats",
    "236": "Soybeans",
    "261": "Sunflower seed",
    "270": "Rapeseed",
    "328": "Seed cotton",
    "156": "Sugar cane",
    "157": "Sugar beet",
    "661": "Cocoa beans",
    "656": "Coffee green",
    "254": "Groundnuts",
    "292": "Oil palm fruit",
    "826": "Rubber natural",
    "401": "Potatoes",
    "574": "Cassava",
    "27":  "Rice paddy",
}

ITEMS_PRICES = {
    "15":  "Wheat",
    "27":  "Rice paddy",
    "56":  "Maize",
    "236": "Soybeans",
    "328": "Seed cotton",
    "156": "Sugar cane",
    "661": "Cocoa beans",
    "656": "Coffee green",
    "826": "Rubber natural",
    "261": "Sunflower seed",
    "270": "Rapeseed",
    "292": "Oil palm fruit",
}

# Element codes
ELEM_PRODUCTION = "5312"  # Production (tonnes)
ELEM_AREA       = "5510"  # Area harvested (ha)
ELEM_PRICE_USD  = "5532"  # Producer price (USD/tonne)


def fao_request(domain, area_codes, item_codes, element_codes, year_range):
    """Try FAOSTAT REST API (multiple base URLs); return parsed DataFrame or None."""
    params = {
        "area":         ",".join(area_codes),
        "item":         ",".join(item_codes),
        "element":      ",".join(element_codes),
        "year":         ",".join(str(y) for y in year_range),
        "show_codes":   "true",
        "show_unit":    "true",
        "show_flags":   "true",
        "null_values":  "false",
        "output_type":  "objects",
    }

    for base_url in FAO_API_URLS:
        url = f"{base_url}/{domain}/"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = requests.get(url, params=params, timeout=60)
                if r.status_code == 200:
                    payload = r.json()
                    data = payload.get("data", [])
                    if not data:
                        break
                    return pd.DataFrame(data)
                if r.status_code == 429:
                    wait = BACKOFF_SECONDS * attempt
                    print(f"  429 rate limit. Backing off {wait}s.")
                    time.sleep(wait)
                else:
                    print(f"  HTTP {r.status_code} from {base_url}: {r.text[:120]}")
                    break
            except requests.RequestException as e:
                print(f"  Request error (attempt {attempt}): {e}")
                time.sleep(BACKOFF_SECONDS)
    return None


def fetch_bulk_zip(zip_name, area_filter, item_filter, element_filter, year_filter):
    """
    Fallback: download a FAOSTAT bulk ZIP, read the CSV inside, and filter.
    zip_name e.g. 'Prices_E_All_Data.zip' or 'Production_Crops_Livestock_E_All_Data.zip'
    """
    url = f"{BULK_BASE}/{zip_name}"
    print(f"  Bulk fallback: {url}")
    try:
        r = requests.get(url, timeout=300, stream=True)
        if r.status_code != 200:
            print(f"  Bulk download failed: HTTP {r.status_code}")
            return None
        total = int(r.headers.get("content-length", 0))
        print(f"  Downloading {total/1e6:.1f} MB...")
        content = b"".join(r.iter_content(chunk_size=1 << 20))
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            # CSV is usually the largest file in the ZIP
            csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
            csv_name = max(csv_names, key=lambda n: zf.getinfo(n).file_size)
            print(f"  Reading {csv_name}...")
            with zf.open(csv_name) as f:
                df = pd.read_csv(f, encoding="latin-1", low_memory=False)
        # Normalize column names
        df.columns = [c.strip() for c in df.columns]
        # Filter to our subset
        if "Area Code" in df.columns and area_filter:
            df = df[df["Area Code"].astype(str).isin(area_filter)]
        if "Item Code" in df.columns and item_filter:
            df = df[df["Item Code"].astype(str).isin(item_filter)]
        if "Element Code" in df.columns and element_filter:
            df = df[df["Element Code"].astype(str).isin(element_filter)]
        # Pivot from wide (Year columns Y1961…Y2024) to long format
        year_cols = [c for c in df.columns if c.startswith("Y") and c[1:].isdigit()]
        if not year_cols:
            return None
        id_cols = [c for c in df.columns if c not in year_cols and not c.endswith("F")]
        df_long = df[id_cols + year_cols].melt(
            id_vars=id_cols, var_name="year_col", value_name="value"
        )
        df_long["year"] = df_long["year_col"].str[1:].astype(int)
        df_long = df_long.drop(columns=["year_col"])
        if year_filter:
            df_long = df_long[df_long["year"].isin(year_filter)]
        df_long["value"] = pd.to_numeric(df_long["value"], errors="coerce")
        df_long = df_long.dropna(subset=["value"])
        return df_long if not df_long.empty else None
    except Exception as e:
        print(f"  Bulk download error: {e}")
        return None


def normalize_df(df, fetched_at):
    """Lowercase columns, rename FAO standard columns, add fetched_at."""
    df.columns = [c.lower().replace(" ", "_") for c in df.columns]
    rename_map = {
        "area_code":    "area_code",
        "area":         "country",
        "item_code":    "item_code",
        "item":         "commodity",
        "element_code": "element_code",
        "element":      "element",
        # Renamed from "year": DuckDB's hive_partitioning=True treats "year"
        # as a reserved virtual column (from storage/raw/.../year=YYYY/), and
        # silently overwrites a same-named physical column with the fetch
        # year instead of the real observation year.
        "year":         "obs_year",
        "unit":         "unit",
        "value":        "value",
        "flag":         "flag",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    df["value"] = pd.to_numeric(df.get("value", pd.Series(dtype=float)), errors="coerce")
    df = df.dropna(subset=["value"])
    df["fetched_at"] = fetched_at
    return df


def main():
    parser = argparse.ArgumentParser(description="FAO FAOSTAT data pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full history from 2000 (slow)")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"
    current_year = now.year

    if args.backfill:
        year_range = list(range(2000, current_year + 1))
    else:
        year_range = list(range(current_year - 2, current_year + 1))

    print(f"FAO FAOSTAT Pipeline  mode={mode}  years={year_range[0]}-{year_range[-1]}\n")
    os.makedirs(PROD_DIR, exist_ok=True)
    os.makedirs(PRICES_DIR, exist_ok=True)

    area_codes = list(AREAS.keys())
    year_set   = set(year_range)

    # --- Production + area harvested ---
    print("[fao_production] Fetching crop production quantities and area...")
    prod_items = list(set(ITEMS_PRODUCTION.keys()))
    prod_df = fao_request(
        domain="QCL",
        area_codes=area_codes,
        item_codes=prod_items,
        element_codes=[ELEM_PRODUCTION, ELEM_AREA],
        year_range=year_range,
    )

    if prod_df is None or prod_df.empty:
        print("  REST API unavailable — trying bulk ZIP fallback...")
        prod_df = fetch_bulk_zip(
            "Production_Crops_Livestock_E_All_Data.zip",
            area_filter=set(area_codes),
            item_filter=set(prod_items),
            element_filter={ELEM_PRODUCTION, ELEM_AREA},
            year_filter=year_set if not args.backfill else None,
        )
        if prod_df is not None and not prod_df.empty:
            # Bulk CSV uses different column names — map to FAO API style
            col_map = {
                "Area Code":    "Area Code",
                "Area":         "Area",
                "Item Code":    "Item Code",
                "Item":         "Item",
                "Element Code": "Element Code",
                "Element":      "Element",
                "Unit":         "Unit",
                "year":         "Year",
                "value":        "Value",
            }
            prod_df = prod_df.rename(columns={v: k for k, v in col_map.items() if v in prod_df.columns})

    if prod_df is not None and not prod_df.empty:
        prod_df = normalize_df(prod_df, now.isoformat())
        prod_path = write_partitioned(
            prod_df, PROD_DIR,
            f"fao_production_{mode}_{today_str}.parquet",
        )
        print(f"  -> {prod_path}  ({len(prod_df):,} rows)")
    else:
        print("  No production data returned from API or bulk fallback.")

    time.sleep(REQUEST_INTERVAL)

    # --- Producer prices ---
    print("\n[fao_prices] Fetching producer prices (USD/tonne)...")
    price_items = list(set(ITEMS_PRICES.keys()))
    price_df = fao_request(
        domain="PP",
        area_codes=area_codes,
        item_codes=price_items,
        element_codes=[ELEM_PRICE_USD],
        year_range=year_range,
    )

    if price_df is None or price_df.empty:
        print("  REST API unavailable — trying bulk ZIP fallback...")
        price_df = fetch_bulk_zip(
            "Prices_E_All_Data.zip",
            area_filter=set(area_codes),
            item_filter=set(price_items),
            element_filter={ELEM_PRICE_USD},
            year_filter=year_set if not args.backfill else None,
        )

    if price_df is not None and not price_df.empty:
        price_df = normalize_df(price_df, now.isoformat())
        price_path = write_partitioned(
            price_df, PRICES_DIR,
            f"fao_prices_{mode}_{today_str}.parquet",
        )
        print(f"  -> {price_path}  ({len(price_df):,} rows)")
    else:
        print("  No price data returned from API or bulk fallback.")

    print("\n--- FAO PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
