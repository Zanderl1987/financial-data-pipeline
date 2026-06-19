import requests
import pandas as pd
import datetime
import time
import os
import argparse
from dotenv import load_dotenv

load_dotenv()

EIA_API_KEY = os.environ["EIA_API_KEY"]
EIA_BASE = "https://api.eia.gov/v2"
OUTPUT_DIR = os.path.join("storage", "raw", "gas_prices")

# EIA doesn't publish a hard rate limit but suspends keys on abuse — be conservative
REQUEST_INTERVAL = 0.25  # 240 req/min
MAX_RETRIES = 3
BACKOFF_SECONDS = 60
PAGE_SIZE = 5000  # EIA v2 max rows per JSON request

# ---------------------------------------------------------------------------
# Daily spot prices — wholesale prices at major trading hubs
# EIA route: petroleum/pri/spt
# These are the closest thing to a "daily gas price" available for free.
# TradingEconomics has daily retail prices (2005-present) but requires a paid
# subscription; EIA spot prices cover the same market-clearing level.
# ---------------------------------------------------------------------------
SPOT_SERIES = {
    "EER_EPMRU_PF4_Y35NY_DPG":    "Gasoline Regular Conv., NY Harbor",
    "EER_EPMRU_PF4_RGC_DPG":      "Gasoline Regular Conv., Gulf Coast",
    "EER_EPMRR_PF4_Y05LA_DPG":    "Gasoline Regular RBOB, Los Angeles",
    "EER_EPD2F_PF4_Y35NY_DPG":    "No. 2 Heating Oil, NY Harbor",
    "EER_EPD2DXL0_PF4_Y35NY_DPG": "ULS No. 2 Diesel, NY Harbor",
    "EER_EPD2DXL0_PF4_RGC_DPG":   "ULS No. 2 Diesel, Gulf Coast",
    "EER_EPD2DC_PF4_Y05LA_DPG":   "ULS No. 2 Diesel, Los Angeles",
    "EER_EPJK_PF4_RGC_DPG":       "Kerosene-Type Jet Fuel, Gulf Coast",
    "EER_EPLLPA_PF4_Y44MB_DPG":   "Propane, Mont Belvieu TX",
}

# ---------------------------------------------------------------------------
# Weekly retail pump prices — what consumers actually pay, by grade and region
# EIA route: petroleum/pri/gnd
# ---------------------------------------------------------------------------
RETAIL_PRODUCTS = [
    "EPMR",   # Regular (all formulations)
    "EPMM",   # Midgrade (all formulations)
    "EPMP",   # Premium (all formulations)
    "EPD2D",  # No. 2 Diesel, on-highway
]

RETAIL_DUOAREAS = [
    "NUS",   # U.S. National Average
    "R10",   # East Coast (PADD 1)
    "R1X",   # New England (PADD 1A)
    "R1Y",   # Central Atlantic (PADD 1B)
    "R1Z",   # Lower Atlantic (PADD 1C)
    "R20",   # Midwest (PADD 2)
    "R30",   # Gulf Coast (PADD 3)
    "R40",   # Rocky Mountain (PADD 4)
    "R50",   # West Coast (PADD 5)
]

PRODUCT_NAMES = {
    "EPMR":  "Regular Gasoline",
    "EPMM":  "Midgrade Gasoline",
    "EPMP":  "Premium Gasoline",
    "EPD2D": "No. 2 Diesel (On-Highway)",
}

DUOAREA_NAMES = {
    "NUS": "U.S. National Average",
    "R10": "East Coast (PADD 1)",
    "R1X": "New England (PADD 1A)",
    "R1Y": "Central Atlantic (PADD 1B)",
    "R1Z": "Lower Atlantic (PADD 1C)",
    "R20": "Midwest (PADD 2)",
    "R30": "Gulf Coast (PADD 3)",
    "R40": "Rocky Mountain (PADD 4)",
    "R50": "West Coast (PADD 5)",
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
                print(f"  429 from EIA — backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code}: {r.text[:200]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts.")
    return None


def fetch_paginated(route, base_params, list_params=None, start_date=None, description=""):
    """
    Fetch all pages from an EIA v2 data route.
    list_params: dict of {key: [values]} for repeated query params (e.g. multiple facet values).
    """
    url = f"{EIA_BASE}/{route}"
    all_rows = []
    offset = 0

    while True:
        # Build as list of tuples so requests handles repeated keys correctly
        params = list(base_params.items())
        if list_params:
            for key, values in list_params.items():
                for v in values:
                    params.append((key, v))
        if start_date:
            params.append(("start", start_date))
        params.append(("offset", str(offset)))
        params.append(("length", str(PAGE_SIZE)))

        r = get_with_backoff(url, params)
        if not r:
            break

        payload = r.json()
        resp = payload.get("response", {})
        data = resp.get("data", [])
        total = int(resp.get("total", 0))

        all_rows.extend(data)
        fetched = offset + len(data)

        if description:
            print(f"  {description}: {fetched}/{total} rows...", end="\r")

        if len(data) < PAGE_SIZE or fetched >= total:
            break

        offset += PAGE_SIZE
        time.sleep(REQUEST_INTERVAL)

    if description:
        print(f"  {description}: {len(all_rows)} rows fetched.   ")
    return all_rows


# ---------------------------------------------------------------------------
# Spot prices (daily, wholesale)
# ---------------------------------------------------------------------------

def fetch_spot_prices(start_date=None):
    print("Fetching daily spot prices (EIA petroleum/pri/spt)...")
    base = {
        "api_key": EIA_API_KEY,
        "data[]": "value",
        "frequency": "daily",
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
    }
    list_params = {"facets[series][]": list(SPOT_SERIES.keys())}

    rows = fetch_paginated(
        "petroleum/pri/spt/data/",
        base,
        list_params=list_params,
        start_date=start_date,
        description="spot",
    )
    if not rows:
        return None

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "value": "price_usd_gallon"})
    df["date"] = pd.to_datetime(df["date"])
    df["price_usd_gallon"] = pd.to_numeric(df["price_usd_gallon"], errors="coerce")
    df["series_name"] = df["series"].map(SPOT_SERIES)
    df["price_type"] = "spot"
    df["frequency"] = "daily"
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()

    wanted = [
        "date", "series", "series_name", "price_usd_gallon",
        "price_type", "frequency", "duoarea", "area-name",
        "product", "product-name", "process", "units", "fetched_at",
    ]
    cols = [c for c in wanted if c in df.columns]
    return df[cols].dropna(subset=["price_usd_gallon"]).sort_values(["series", "date"])


# ---------------------------------------------------------------------------
# Retail prices (weekly, consumer pump price by grade × region)
# ---------------------------------------------------------------------------

def fetch_retail_prices(start_date=None):
    print("Fetching weekly retail prices (EIA petroleum/pri/gnd)...")
    base = {
        "api_key": EIA_API_KEY,
        "data[]": "value",
        "frequency": "weekly",
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
    }
    list_params = {
        "facets[product][]": RETAIL_PRODUCTS,
        "facets[duoarea][]": RETAIL_DUOAREAS,
    }

    rows = fetch_paginated(
        "petroleum/pri/gnd/data/",
        base,
        list_params=list_params,
        start_date=start_date,
        description="retail",
    )
    if not rows:
        return None

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "value": "price_usd_gallon"})
    df["date"] = pd.to_datetime(df["date"])
    df["price_usd_gallon"] = pd.to_numeric(df["price_usd_gallon"], errors="coerce")

    # Enrich with human-readable names; fall back to API-provided fields if available
    df["product_name"] = df["product"].map(PRODUCT_NAMES)
    if "product-name" in df.columns:
        df["product_name"] = df["product_name"].fillna(df["product-name"])
    df["region_name"] = df["duoarea"].map(DUOAREA_NAMES)
    if "area-name" in df.columns:
        df["region_name"] = df["region_name"].fillna(df["area-name"])

    df["price_type"] = "retail"
    df["frequency"] = "weekly"
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()

    wanted = [
        "date", "duoarea", "region_name", "product", "product_name",
        "price_usd_gallon", "price_type", "frequency",
        "process", "units", "fetched_at",
    ]
    cols = [c for c in wanted if c in df.columns]
    return df[cols].dropna(subset=["price_usd_gallon"]).sort_values(["product", "duoarea", "date"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill=False):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if backfill:
        start_date = None
        mode_tag = "backfill"
        print("Mode: BACKFILL (full history from EIA)")
    else:
        start_date = (datetime.datetime.utcnow() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
        mode_tag = "incremental"
        print(f"Mode: INCREMENTAL (from {start_date})")

    today = datetime.datetime.utcnow().strftime("%Y%m%d")

    spot_df = fetch_spot_prices(start_date)
    time.sleep(REQUEST_INTERVAL)
    retail_df = fetch_retail_prices(start_date)

    if spot_df is not None and not spot_df.empty:
        path = os.path.join(OUTPUT_DIR, f"gas_prices_spot_daily_{mode_tag}_{today}.parquet")
        spot_df.to_parquet(path, index=False, compression="snappy")
        n_series = spot_df["series"].nunique() if "series" in spot_df.columns else "?"
        print(f"\nSpot  → {path}")
        print(f"       {len(spot_df):,} rows, {n_series} series")
    else:
        print("\nSpot prices: no data returned (check EIA_API_KEY and series IDs).")

    if retail_df is not None and not retail_df.empty:
        path = os.path.join(OUTPUT_DIR, f"gas_prices_retail_weekly_{mode_tag}_{today}.parquet")
        retail_df.to_parquet(path, index=False, compression="snappy")
        combos = retail_df.groupby(["duoarea", "product"]).ngroups if "duoarea" in retail_df.columns else "?"
        print(f"Retail → {path}")
        print(f"       {len(retail_df):,} rows, {combos} grade×region combinations")
    else:
        print("Retail prices: no data returned (check EIA_API_KEY).")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="EIA gas price pipeline — daily spot prices + weekly retail by grade/region"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch full available history (use on first run). Default: last 90 days.",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
