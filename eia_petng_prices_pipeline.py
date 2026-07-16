"""
EIA Petroleum & Natural Gas Prices Pipeline -- spot prices, futures,
refiner margins, supply/demand balance, natural gas consumption, and LNG.

Supplements eia_pipeline.py (petroleum stocks, natgas storage, crude production,
refinery activity, crude trade), gas_price_pipeline.py (spot + retail gas prices),
and eia_expansion_pipeline.py (electricity, nuclear, coal, international, SEDS).
Uses the same EIA Open Data API v2.

CLI:
  python eia_petng_prices_pipeline.py              # incremental (last 6 months)
  python eia_petng_prices_pipeline.py --backfill   # full history from EIA

Outputs:
  storage/raw/eia/petroleum_spot_prices/**/*.parquet
  storage/raw/eia/petroleum_futures/**/*.parquet
  storage/raw/eia/refiner_margins/**/*.parquet
  storage/raw/eia/petroleum_supply_demand/**/*.parquet
  storage/raw/eia/natural_gas_consumption/**/*.parquet
  storage/raw/eia/natural_gas_prices/**/*.parquet
  storage/raw/eia/natural_gas_production/**/*.parquet
  storage/raw/eia/lng_flows/**/*.parquet
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

EIA_API_KEY = os.environ["EIA_API_KEY"]
EIA_BASE = "https://api.eia.gov/v2"

EIA_DIR = os.path.join("storage", "raw", "eia")

DIRS = {
    "petroleum_spot_prices":       os.path.join(EIA_DIR, "petroleum_spot_prices"),
    "petroleum_futures":           os.path.join(EIA_DIR, "petroleum_futures"),
    "refiner_margins":             os.path.join(EIA_DIR, "refiner_margins"),
    "petroleum_supply_demand":     os.path.join(EIA_DIR, "petroleum_supply_demand"),
    "natural_gas_consumption":     os.path.join(EIA_DIR, "natural_gas_consumption"),
    "natural_gas_prices":          os.path.join(EIA_DIR, "natural_gas_prices"),
    "natural_gas_production":      os.path.join(EIA_DIR, "natural_gas_production"),
    "lng_flows":                   os.path.join(EIA_DIR, "lng_flows"),
}

REQUEST_INTERVAL = 0.25
MAX_RETRIES      = 3
BACKOFF_SECONDS  = 60
PAGE_SIZE        = 5000

INCREMENTAL_MONTHS = 6
INCREMENTAL_YEARS  = 2

# ---------------------------------------------------------------------------
# Petroleum spot prices (route: petroleum/pri/spt)
# Key products: WTI (EPCWTI), Brent (EPCBRENT), Gasoline (EPMRR,EPMRU),
# Heating Oil (EPD2F), Jet Fuel (EPJK), Propane (EPLLPA)
# ---------------------------------------------------------------------------
PET_SPPRODUCTS: dict[str, str] = {
    "EPCWTI":   "WTI Crude Oil",
    "EPCBRENT": "Brent Crude Oil",
    "EPMRR":    "Reformulated Regular Gasoline",
    "EPMRU":    "Conventional Regular Gasoline",
    "EPD2F":    "No 2 Heating Oil",
    "EPJK":     "Kerosene-Type Jet Fuel",
    "EPLLPA":   "Propane",
}

PET_SPDUOAREAS: dict[str, str] = {
    "YCUOK": "Cushing OK",
    "ZEU":   "Europe",
    "Y35NY": "New York City",
    "Y05LA": "Los Angeles",
    "RGC":   "Gulf Coast",
    "Y44MB": "Mont Belvieu TX",
}

# ---------------------------------------------------------------------------
# Petroleum futures (route: petroleum/pri/fut)
# NYMEX futures: EPC0 (crude), EPD2F (heating oil), EPMRR (gasoline)
# Process: PE1 (front month), PE2, PE3, PE4
# ---------------------------------------------------------------------------
PET_FUTPRODUCTS: dict[str, str] = {
    "EPC0":  "NYMEX WTI Crude",
    "EPD2F": "NYMEX Heating Oil",
    "EPMRR": "NYMEX RBOB Gasoline",
}

PET_FUTPROCESSES: dict[str, str] = {
    "PE1": "Front Month",
    "PE2": "Second Month",
    "PE3": "Third Month",
    "PE4": "Fourth Month",
}

# ---------------------------------------------------------------------------
# Refiner margins (route: petroleum/pri/refmg)
# Products: EPM0 (Total Gasoline), EPMR (Regular), EPMP (Premium), EPMM (Midgrade)
# Process: PBR (Bulk Sales), PTR (Through Company Outlets), PTG (Retail Sales)
# ---------------------------------------------------------------------------
PET_MARGINSERIES: dict[str, str] = {
    "EMA_EPM0_PBR_NUS_DPG": "Total Gasoline Bulk Sales",
    "EMA_EPMR_PBR_NUS_DPG": "Regular Gasoline Bulk Sales",
    "EMA_EPMP_PBR_NUS_DPG": "Premium Gasoline Bulk Sales",
    "EMA_EPMM_PBR_NUS_DPG": "Midgrade Gasoline Bulk Sales",
}

# ---------------------------------------------------------------------------
# Petroleum supply & disposition (route: petroleum/sum/snd)
# Process codes: EEX (Exports), IM0 (Imports), VPP (Product Supplied),
# SAE (Ending Stocks), YIR (Refinery Input), YPR (Refinery Production),
# SCG (Stock Change), VNR (Net Receipts)
# ---------------------------------------------------------------------------
PET_SNDPROCESS: dict[str, str] = {
    "EEX": "Exports",
    "IM0": "Imports",
    "VPP": "Product Supplied",
    "SAE": "Ending Stocks",
    "YIR": "Refinery and Blender Net Input",
    "YPR": "Refinery and Blender Net Production",
    "SCG": "Stock Change",
    "VNR": "Net Receipts by Pipeline, Tanker, Barge and Rail",
}

# ---------------------------------------------------------------------------
# Natural gas consumption by sector (route: natural-gas/cons/sum)
# Process codes: VRS (Residential), VCS (Commercial), VIN (Industrial)
# ---------------------------------------------------------------------------
NG_CONS_SECTORS: dict[str, str] = {
    "VRS": "Residential",
    "VCS": "Commercial",
    "VIN": "Industrial",
}

# ---------------------------------------------------------------------------
# Natural gas prices (route: natural-gas/pri/sum)
# Process codes: PRS (Residential), PCS (Commercial), PEU (Electric Utility),
# PIN (Industrial), PG1 (Citygate)
# ---------------------------------------------------------------------------
NG_PRICE_SERIES: dict[str, str] = {
    "N3010US3": "Residential Price",
    "N3020US3": "Commercial Price",
    "N3045US3": "Industrial Price",
    "N3050US3": "Electric Utility Price",
    "N9190US3": "Citygate Price",
}

# ---------------------------------------------------------------------------
# Natural gas production (route: natural-gas/prod/sum)
# Process codes: FGW (Gross Withdrawals), FGO (Withdrawals from Oil Wells),
# FGG (Withdrawals from Gas Wells)
# ---------------------------------------------------------------------------
NG_PROD_SERIES: dict[str, str] = {
    "N9010US2": "Gross Withdrawals",
    "N9012US2": "Withdrawals from Oil Wells",
    "N9011US2": "Withdrawals from Gas Wells",
}

# ---------------------------------------------------------------------------
# LNG exports (route: natural-gas/lng/exp)
# ---------------------------------------------------------------------------
NG_LNG_FACETS: dict[str, str] = {
    "PADD1": "East Coast",
    "PADD2": "Midwest",
    "PADD3": "Gulf Coast",
    "PADD4": "Rocky Mountain",
    "PADD5": "West Coast",
}


# ---------------------------------------------------------------------------
# HTTP helpers (identical to eia_expansion_pipeline.py)
# ---------------------------------------------------------------------------

def _get_with_backoff(url: str, params) -> requests.Response | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from EIA -- backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code}: {r.text[:200]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    print(f"  Giving up after {MAX_RETRIES} attempts.")
    return None


def _fetch_paginated(
    route: str,
    base_params: dict,
    list_params: dict | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    label: str = "",
) -> list[dict]:
    url = f"{EIA_BASE}/{route}"
    all_rows: list[dict] = []
    offset = 0

    while True:
        params = list(base_params.items())
        if list_params:
            for key, values in list_params.items():
                for v in values:
                    params.append((key, v))
        if start_date:
            params.append(("start", start_date))
        if end_date:
            params.append(("end", end_date))
        params.append(("offset", str(offset)))
        params.append(("length", str(PAGE_SIZE)))

        r = _get_with_backoff(url, params)
        if not r:
            break

        resp  = r.json().get("response", {})
        data  = resp.get("data", [])
        total = int(resp.get("total", 0))
        all_rows.extend(data)
        fetched = offset + len(data)

        if label:
            print(f"  {label}: {fetched}/{total} rows...", end="\r")

        if len(data) < PAGE_SIZE or fetched >= total:
            break
        offset += PAGE_SIZE
        time.sleep(REQUEST_INTERVAL)

    if label:
        print(f"  {label}: {len(all_rows)} rows.   ")
    return all_rows


# ---------------------------------------------------------------------------
# 1. Petroleum spot prices (daily)
# ---------------------------------------------------------------------------

def fetch_petroleum_spot_prices(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching petroleum spot prices (EIA petroleum/pri/spt)...")
    rows = _fetch_paginated(
        "petroleum/pri/spt/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "daily",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={
            "facets[product][]": list(PET_SPPRODUCTS.keys()),
        },
        start_date=start_date,
        label="pet_spot",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "duoarea": "location_code", "product": "product_code"})
    df["date"]         = pd.to_datetime(df["date"], errors="coerce")
    df["value"]        = pd.to_numeric(df["value"], errors="coerce")
    df["product_name"] = df["product_code"].map(PET_SPPRODUCTS)
    df["location_name"] = df["location_code"].map(PET_SPDUOAREAS)
    df["units"]        = "Dollars per Barrel"
    df["frequency"]    = "daily"
    df["source"]       = "EIA"
    df["fetched_at"]   = datetime.datetime.utcnow().isoformat()

    keep = ["date", "product_code", "product_name", "location_code", "location_name",
            "value", "units", "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["product_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2. Petroleum futures (daily)
# ---------------------------------------------------------------------------

def fetch_petroleum_futures(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching NYMEX petroleum futures (EIA petroleum/pri/fut)...")
    rows = _fetch_paginated(
        "petroleum/pri/fut/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "daily",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={
            "facets[product][]": list(PET_FUTPRODUCTS.keys()),
            "facets[process][]": ["PE1"],  # Front month only
        },
        start_date=start_date,
        label="pet_fut",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "product": "product_code"})
    df["date"]         = pd.to_datetime(df["date"], errors="coerce")
    df["value"]        = pd.to_numeric(df["value"], errors="coerce")
    df["product_name"] = df["product_code"].map(PET_FUTPRODUCTS)
    df["units"]        = "Dollars per Barrel"
    df["frequency"]    = "daily"
    df["source"]       = "EIA"
    df["fetched_at"]   = datetime.datetime.utcnow().isoformat()

    keep = ["date", "product_code", "product_name", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["product_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3. Refiner margins / crack spreads (monthly)
# ---------------------------------------------------------------------------

def fetch_refiner_margins(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching refiner margins (EIA petroleum/pri/refmg)...")
    rows = _fetch_paginated(
        "petroleum/pri/refmg/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "monthly",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={
            "facets[series][]": list(PET_MARGINSERIES.keys()),
            "facets[duoarea][]": ["NUS"],  # US average
        },
        start_date=start_date,
        label="ref_margin",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "series": "series_code"})
    df["date"]         = pd.to_datetime(df["date"], errors="coerce")
    df["value"]        = pd.to_numeric(df["value"], errors="coerce")
    df["series_name"]  = df["series_code"].map(PET_MARGINSERIES)
    df["units"]        = "Dollars per Barrel"
    df["frequency"]    = "monthly"
    df["source"]       = "EIA"
    df["fetched_at"]   = datetime.datetime.utcnow().isoformat()

    keep = ["date", "series_code", "series_name", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["series_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4. Petroleum supply & disposition (monthly)
# ---------------------------------------------------------------------------

def fetch_petroleum_supply_demand(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching petroleum supply & disposition (EIA petroleum/sum/snd)...")
    rows = _fetch_paginated(
        "petroleum/sum/snd/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "monthly",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={
            "facets[process][]": list(PET_SNDPROCESS.keys()),
            "facets[duoarea][]": ["NUS-Z00"],  # US national level
        },
        start_date=start_date,
        label="pet_snd",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "process": "process_code"})
    df["date"]         = pd.to_datetime(df["date"], errors="coerce")
    df["value"]        = pd.to_numeric(df["value"], errors="coerce")
    df["process_name"] = df["process_code"].map(PET_SNDPROCESS)
    df["units"]        = df.get("units", "Thousand Barrels per Day")
    df["frequency"]    = "monthly"
    df["source"]       = "EIA"
    df["fetched_at"]   = datetime.datetime.utcnow().isoformat()

    keep = ["date", "process_code", "process_name", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["process_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5. Natural gas consumption by sector (monthly)
# ---------------------------------------------------------------------------

def fetch_natural_gas_consumption(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching natural gas consumption by sector (EIA natural-gas/cons/sum)...")
    rows = _fetch_paginated(
        "natural-gas/cons/sum/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "monthly",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={
            "facets[process][]": list(NG_CONS_SECTORS.keys()),
            "facets[duoarea][]": ["NUS"],  # US national level
        },
        start_date=start_date,
        label="ng_cons",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "process": "sector_code"})
    df["date"]         = pd.to_datetime(df["date"], errors="coerce")
    df["value"]        = pd.to_numeric(df["value"], errors="coerce")
    df["sector_name"]  = df["sector_code"].map(NG_CONS_SECTORS)
    df["units"]        = df.get("units", "Million Cubic Feet")
    df["frequency"]    = "monthly"
    df["source"]       = "EIA"
    df["fetched_at"]   = datetime.datetime.utcnow().isoformat()

    keep = ["date", "sector_code", "sector_name", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["sector_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 6. Natural gas prices (monthly)
# ---------------------------------------------------------------------------

def fetch_natural_gas_prices(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching natural gas prices (EIA natural-gas/pri/sum)...")
    rows = _fetch_paginated(
        "natural-gas/pri/sum/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "monthly",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={
            "facets[series][]": list(NG_PRICE_SERIES.keys()),
            "facets[duoarea][]": ["NUS"],  # US national level
        },
        start_date=start_date,
        label="ng_price",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "series": "series_code"})
    df["date"]         = pd.to_datetime(df["date"], errors="coerce")
    df["value"]        = pd.to_numeric(df["value"], errors="coerce")
    df["series_name"]  = df["series_code"].map(NG_PRICE_SERIES)
    df["units"]        = df.get("units", "Dollars per Thousand Cubic Feet")
    df["frequency"]    = "monthly"
    df["source"]       = "EIA"
    df["fetched_at"]   = datetime.datetime.utcnow().isoformat()

    keep = ["date", "series_code", "series_name", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["series_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 7. Natural gas production (monthly)
# ---------------------------------------------------------------------------

def fetch_natural_gas_production(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching natural gas production (EIA natural-gas/prod/sum)...")
    rows = _fetch_paginated(
        "natural-gas/prod/sum/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "monthly",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={
            "facets[series][]": list(NG_PROD_SERIES.keys()),
            "facets[duoarea][]": ["NUS"],  # US national level
        },
        start_date=start_date,
        label="ng_prod",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "series": "series_code"})
    df["date"]         = pd.to_datetime(df["date"], errors="coerce")
    df["value"]        = pd.to_numeric(df["value"], errors="coerce")
    df["series_name"]  = df["series_code"].map(NG_PROD_SERIES)
    df["units"]        = df.get("units", "Million Cubic Feet")
    df["frequency"]    = "monthly"
    df["source"]       = "EIA"
    df["fetched_at"]   = datetime.datetime.utcnow().isoformat()

    keep = ["date", "series_code", "series_name", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["series_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 8. LNG exports (monthly)
# ---------------------------------------------------------------------------

def fetch_lng_exports(start_date: str | None = None) -> pd.DataFrame:
    """LNG route not available in EIA v2 API. Returns empty DataFrame."""
    print("Fetching LNG export volumes (EIA natural-gas/lng/exp)...")
    print("  [!] LNG route not available in EIA v2 API -- skipping.")
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill: bool = False) -> None:
    for d in DIRS.values():
        os.makedirs(d, exist_ok=True)

    now   = datetime.datetime.utcnow()
    today = now.strftime("%Y%m%d")

    # Backfill: 2000-01-01 → today (avoids timeout from unbounded queries)
    # Incremental: last N months
    BACKFILL_START = "2000-01-01"

    if backfill:
        start_date = BACKFILL_START
        mode_tag   = "backfill"
        print(f"Mode: BACKFILL (from {BACKFILL_START})")
    else:
        cutoff_monthly = now - datetime.timedelta(days=INCREMENTAL_MONTHS * 30)
        cutoff_annual  = now - datetime.timedelta(days=INCREMENTAL_YEARS * 365)
        start_date = cutoff_monthly.strftime("%Y-%m-%d")
        mode_tag = "incremental"
        print(f"Mode: INCREMENTAL (from {start_date})")

    # Use the appropriate cutoff for each dataset
    monthly_start = start_date

    # ── 1. Petroleum spot prices (daily) ──────────────────────────────────
    df = fetch_petroleum_spot_prices(monthly_start)
    if not df.empty:
        path = write_partitioned(df, DIRS["petroleum_spot_prices"],
                                 f"eia_petroleum_spot_prices_{mode_tag}_{today}.parquet")
        n_products = df["product_code"].nunique()
        print(f"[+] {path} | {len(df):,} rows | {n_products} products")
    else:
        print("[!] No petroleum spot price data returned.")
    time.sleep(REQUEST_INTERVAL)

    # ── 2. Petroleum futures (daily) ──────────────────────────────────────
    df = fetch_petroleum_futures(monthly_start)
    if not df.empty:
        path = write_partitioned(df, DIRS["petroleum_futures"],
                                 f"eia_petroleum_futures_{mode_tag}_{today}.parquet")
        n_products = df["product_code"].nunique()
        print(f"[+] {path} | {len(df):,} rows | {n_products} products")
    else:
        print("[!] No petroleum futures data returned.")
    time.sleep(REQUEST_INTERVAL)

    # ── 3. Refiner margins (monthly) ──────────────────────────────────────
    df = fetch_refiner_margins(monthly_start)
    if not df.empty:
        path = write_partitioned(df, DIRS["refiner_margins"],
                                 f"eia_refiner_margins_{mode_tag}_{today}.parquet")
        n_series = df["series_code"].nunique()
        print(f"[+] {path} | {len(df):,} rows | {n_series} margin series")
    else:
        print("[!] No refiner margin data returned.")
    time.sleep(REQUEST_INTERVAL)

    # ── 4. Petroleum supply & disposition (monthly) ───────────────────────
    df = fetch_petroleum_supply_demand(monthly_start)
    if not df.empty:
        path = write_partitioned(df, DIRS["petroleum_supply_demand"],
                                 f"eia_petroleum_supply_demand_{mode_tag}_{today}.parquet")
        n_proc = df["process_code"].nunique()
        print(f"[+] {path} | {len(df):,} rows | {n_proc} processes")
    else:
        print("[!] No petroleum S&D data returned.")
    time.sleep(REQUEST_INTERVAL)

    # ── 5. Natural gas consumption (monthly) ──────────────────────────────
    df = fetch_natural_gas_consumption(monthly_start)
    if not df.empty:
        path = write_partitioned(df, DIRS["natural_gas_consumption"],
                                 f"eia_natural_gas_consumption_{mode_tag}_{today}.parquet")
        n_sectors = df["sector_code"].nunique()
        print(f"[+] {path} | {len(df):,} rows | {n_sectors} sectors")
    else:
        print("[!] No natural gas consumption data returned.")
    time.sleep(REQUEST_INTERVAL)

    # ── 6. Natural gas prices (monthly) ───────────────────────────────────
    df = fetch_natural_gas_prices(monthly_start)
    if not df.empty:
        path = write_partitioned(df, DIRS["natural_gas_prices"],
                                 f"eia_natural_gas_prices_{mode_tag}_{today}.parquet")
        n_series = df["series_code"].nunique()
        print(f"[+] {path} | {len(df):,} rows | {n_series} price series")
    else:
        print("[!] No natural gas price data returned.")
    time.sleep(REQUEST_INTERVAL)

    # ── 7. Natural gas production (monthly) ───────────────────────────────
    df = fetch_natural_gas_production(monthly_start)
    if not df.empty:
        path = write_partitioned(df, DIRS["natural_gas_production"],
                                 f"eia_natural_gas_production_{mode_tag}_{today}.parquet")
        n_series = df["series_code"].nunique()
        print(f"[+] {path} | {len(df):,} rows | {n_series} production series")
    else:
        print("[!] No natural gas production data returned.")
    time.sleep(REQUEST_INTERVAL)

    # ── 8. LNG exports (monthly) ──────────────────────────────────────────
    # Route not available in EIA v2 API -- skipping
    print("  [!] LNG exports skipped (route not available in EIA v2 API)")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="EIA Petroleum & Natural Gas Prices Pipeline -- spot, futures, margins, S&D, NG prices"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=f"Fetch full available history. Default: last {INCREMENTAL_MONTHS} months.",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
