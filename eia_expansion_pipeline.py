"""
EIA Expansion Pipeline — electricity generation, retail sales, nuclear outages,
coal production/trade, international energy, and state-level SEDS.

Supplements eia_pipeline.py (petroleum stocks, natgas storage, crude production,
refinery activity, crude trade) and gas_price_pipeline.py (spot + retail gas prices).
Uses the same EIA Open Data API v2 — same key as the existing EIA pipelines.
Add EIA_API_KEY to .env.

Outputs:
  storage/raw/eia/electricity_generation/**/*.parquet  (CATALOG: eia_electricity_generation)
  storage/raw/eia/electricity_sales/**/*.parquet        (CATALOG: eia_electricity_sales)
  storage/raw/eia/nuclear_outages/**/*.parquet          (CATALOG: eia_nuclear_outages)
  storage/raw/eia/coal_production/**/*.parquet          (CATALOG: eia_coal_production)
  storage/raw/eia/coal_trade/**/*.parquet               (CATALOG: eia_coal_trade)
  storage/raw/eia/international/**/*.parquet            (CATALOG: eia_international)
  storage/raw/eia/seds/**/*.parquet                     (CATALOG: eia_seds)

Usage:
  python eia_expansion_pipeline.py              # incremental (last 6 months / 2 years)
  python eia_expansion_pipeline.py --backfill   # full history from EIA
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
ELEC_GEN_DIR  = os.path.join(EIA_DIR, "electricity_generation")
ELEC_SALES_DIR = os.path.join(EIA_DIR, "electricity_sales")
NUCLEAR_DIR   = os.path.join(EIA_DIR, "nuclear_outages")
COAL_PROD_DIR = os.path.join(EIA_DIR, "coal_production")
COAL_TRADE_DIR = os.path.join(EIA_DIR, "coal_trade")
INTL_DIR      = os.path.join(EIA_DIR, "international")
SEDS_DIR      = os.path.join(EIA_DIR, "seds")

REQUEST_INTERVAL = 0.25   # 240 req/min -- EIA suspends keys on abuse
MAX_RETRIES      = 3
BACKOFF_SECONDS  = 60
PAGE_SIZE        = 5000   # EIA v2 max rows per request

INCREMENTAL_MONTHS = 6    # default lookback for monthly data
INCREMENTAL_YEARS  = 2    # default lookback for annual data

# ---------------------------------------------------------------------------
# Electricity generation by fuel type (route: electricity/electric-power-operational-data)
# Units: megawatthours; frequency: monthly
# Sector 99 = Total Electric Power; stateid US = national total
# ---------------------------------------------------------------------------
ELEC_GEN_FUELS: dict[str, str] = {
    "COW": "Coal",
    "NG":  "Natural Gas",
    "OOG": "Other Gases",
    "OIL": "Petroleum",
    "NUC": "Nuclear",
    "HYC": "Hydroelectric",
    "SUN": "Solar",
    "WND": "Wind",
    "GEO": "Geothermal",
    "WB":  "Wood/Biomass",
}

ELEC_GEN_STATES = ["US"]  # national total; expand to individual states if needed

# ---------------------------------------------------------------------------
# Electricity retail sales (route: electricity/retail-sales)
# Units: sales=million kWh, revenue=million $, price=cents/kWh, customers=count
# Frequency: monthly
# ---------------------------------------------------------------------------
ELEC_SALES_SECTORS: dict[str, str] = {
    "ALL": "All Sectors",
    "RES": "Residential",
    "COM": "Commercial",
    "IND": "Industrial",
    "TRA": "Transportation",
}

ELEC_SALES_STATES = [
    "US",   # national total
    "CAL", "TEX", "NY", "FL", "PA", "IL", "OH", "GA", "NC", "MI",  # top 10 by pop
    "NJ", "VA", "WA", "AZ", "MA", "TN", "IN", "MO", "MD", "WI",
    "CO", "MN", "SC", "AL", "LA", "KY", "OR", "OK", "CT", "UT",
]

# ---------------------------------------------------------------------------
# Nuclear outages (route: nuclear-outages/us-nuclear-outages)
# Frequency: daily; no facets (US aggregate)
# ---------------------------------------------------------------------------
NUCLEAR_DATA_FIELDS = ["capacity", "outage-mwg", "percent"]

# ---------------------------------------------------------------------------
# Coal production (route: coal/aggregate-production)
# Units: short tons (production), short tons/hr (productivity), count (employees)
# Frequency: annual
# ---------------------------------------------------------------------------
COAL_PROD_RANKS: dict[str, str] = {
    "ANT": "Anthracite",
    "BIT": "Bituminous",
    "SUB": "Subbituminous",
    "LIG": "Lignite",
}

COAL_PROD_TYPES: dict[str, str] = {
    "surface":    "Surface",
    "underground": "Underground",
    "refuse":     "Refuse",
}

# ---------------------------------------------------------------------------
# Coal exports/imports (route: coal/exports-imports-quantity-price)
# Units: short tons (quantity), dollars per short ton (price), thousand $ (value)
# Frequency: monthly or annual
# ---------------------------------------------------------------------------
COAL_TRADE_TOP_DESTINATIONS = [
    "IND", "JPN", "KOR", "Netherlands", "Brazil", "China",
    "Taiwan", "Vietnam", "Morocco", "Egypt",
]

# ---------------------------------------------------------------------------
# International energy (route: international)
# Frequency: annual; data[]=value
# ---------------------------------------------------------------------------
INTL_ACTIVITIES: dict[str, str] = {
    "1": "Production",
    "2": "Consumption",
    "3": "Imports",
    "4": "Exports",
}

INTL_PRODUCTS: dict[str, str] = {
    "44":  "Total Energy",
    "53":  "Petroleum & Other Liquids",
    "57":  "Natural Gas (dry)",
    "56":  "Coal",
    "6":   "Nuclear Electric Power",
    "73":  "Hydroelectric Power",
    "72":  "Other Renewables",
    "4008": "CO2 Emissions",
}

INTL_COUNTRIES: dict[str, str] = {
    "USA": "United States",
    "CHN": "China",
    "IND": "India",
    "RUS": "Russia",
    "JPN": "Japan",
    "DEU": "Germany",
    "GBR": "United Kingdom",
    "CAN": "Canada",
    "BRA": "Brazil",
    "KOR": "South Korea",
    "FRA": "France",
    "SAU": "Saudi Arabia",
    "MEX": "Mexico",
    "IDN": "Indonesia",
    "AUS": "Australia",
}

INTL_UNITS: dict[str, str] = {
    "QBTU":  "Quadrillion Btu",
    "TBPD":  "Thousand Barrels Per Day",
    "BKWH":  "Billion Kilowatthours",
    "MMTCD": "Million Metric Tonnes CO2",
}

# ---------------------------------------------------------------------------
# State Energy Data System -- SEDS (route: seds)
# Frequency: annual; ~3 year lag
# ---------------------------------------------------------------------------
SEDS_STATES = [
    "US",
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL",
    "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
    "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI",
    "WY",
]


# ---------------------------------------------------------------------------
# HTTP helpers (identical to eia_pipeline.py)
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
    """Fetch all pages from an EIA v2 data route."""
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
# 1. Electricity generation by fuel type (monthly, EIA-923)
# ---------------------------------------------------------------------------

def fetch_electricity_generation(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching monthly electricity generation by fuel (EIA electric-power-operational-data)...")
    rows = _fetch_paginated(
        "electricity/electric-power-operational-data/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "generation",
            "frequency":          "monthly",
            "facets[sectorid][]": "99",      # Total Electric Power sector
            "facets[location][]": "US",      # national total
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={"facets[fueltypeid][]": list(ELEC_GEN_FUELS.keys())},
        start_date=start_date,
        label="elec_gen",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "fueltypeid": "fuel_code",
                             "generation": "value"})
    if "value" not in df.columns:
        print(f"  Warning: no 'value' column in EIA response (columns: {list(df.columns)[:10]})")
        return pd.DataFrame()
    df["date"]       = pd.to_datetime(df["date"], errors="coerce")
    df["value"]      = pd.to_numeric(df["value"], errors="coerce")
    df["fuel_name"]  = df["fuel_code"].map(ELEC_GEN_FUELS)
    df["state"]      = df.get("location", "US")
    df["sector"]     = "Total Electric Power"
    df["units"]      = "Megawatthours"
    df["frequency"]  = "monthly"
    df["source"]     = "EIA"
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()

    keep = ["date", "fuel_code", "fuel_name", "state", "sector",
            "value", "units", "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["fuel_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2. Electricity retail sales by state/sector (monthly)
# ---------------------------------------------------------------------------

def fetch_electricity_sales(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching monthly electricity retail sales (EIA electricity/retail-sales)...")
    rows = _fetch_paginated(
        "electricity/retail-sales/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "frequency":          "monthly",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={
            "data[]":             ["sales", "revenue", "price"],
            "facets[stateid][]":  ELEC_SALES_STATES,
            "facets[sectorid][]": list(ELEC_SALES_SECTORS.keys()),
        },
        start_date=start_date,
        label="elec_sales",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "sectorid": "sector_code"})
    # Wide response: sales/revenue/price come back as separate columns, not a
    # single "value" column -- "sales" (volume, MkWh) is the headline metric.
    if "sales" not in df.columns:
        print(f"  Warning: no 'sales' column in EIA retail-sales response (columns: {list(df.columns)[:10]})")
        return pd.DataFrame()
    df["date"]        = pd.to_datetime(df["date"], errors="coerce")
    df["value"]       = pd.to_numeric(df["sales"], errors="coerce")
    df["revenue"]     = pd.to_numeric(df.get("revenue"), errors="coerce")
    df["price"]       = pd.to_numeric(df.get("price"), errors="coerce")
    df["sector_name"] = df["sector_code"].map(ELEC_SALES_SECTORS)
    df["state_name"]  = df.get("stateDescription", "")
    df["units"]       = "Million Kilowatthours"
    df["frequency"]   = "monthly"
    df["source"]      = "EIA"
    df["fetched_at"]  = datetime.datetime.utcnow().isoformat()

    keep = ["date", "stateid", "state_name", "sector_code", "sector_name",
            "value", "revenue", "price", "units", "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["stateid", "sector_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3. Nuclear outages (daily, US aggregate)
# ---------------------------------------------------------------------------

def fetch_nuclear_outages(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching daily US nuclear outages (EIA nuclear-outages/us-nuclear-outages)...")
    rows = _fetch_paginated(
        "nuclear-outages/us-nuclear-outages/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "frequency":          "daily",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={
            "data[]": ["capacity", "outage", "percentOutage"],
        },
        start_date=start_date,
        label="nuclear",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date"})
    # This dataset returns capacity/outage/percentOutage as separate columns per
    # row (not the usual single "value" column) -- "outage" (MW offline) is the
    # headline metric, so it maps to "value"; capacity/percent kept alongside.
    if "outage" not in df.columns:
        print(f"  Warning: no 'outage' column in EIA nuclear response (columns: {list(df.columns)[:10]})")
        return pd.DataFrame()
    df["date"]            = pd.to_datetime(df["date"], errors="coerce")
    df["value"]            = pd.to_numeric(df["outage"], errors="coerce")
    df["capacity_mw"]      = pd.to_numeric(df.get("capacity"), errors="coerce")
    df["percent_outage"]   = pd.to_numeric(df.get("percentOutage"), errors="coerce")
    df["state"]      = "US"
    df["frequency"]  = "daily"
    df["source"]     = "EIA"
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()

    keep = ["date", "state", "value", "capacity_mw", "percent_outage",
            "units", "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4. Coal production by rank/type (annual)
# ---------------------------------------------------------------------------

def fetch_coal_production(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching annual coal production by rank (EIA coal/aggregate-production)...")
    rows = _fetch_paginated(
        "coal/aggregate-production/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "production",
            "frequency":          "annual",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={"facets[coalRankId][]": list(COAL_PROD_RANKS.keys())},
        start_date=start_date,
        label="coal_prod",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "coalRankId": "rank_code",
                             "production": "value"})
    if "value" not in df.columns:
        print(f"  Warning: no 'value' column in EIA coal-production response (columns: {list(df.columns)[:10]})")
        return pd.DataFrame()
    df["date"]       = pd.to_datetime(df["date"], errors="coerce")
    df["value"]      = pd.to_numeric(df["value"], errors="coerce")
    df["rank_name"]  = df["rank_code"].map(COAL_PROD_RANKS)
    df["mine_type"]  = df.get("mineTypeId", "")
    df["state"]      = df.get("stateRegionId", "")
    df["units"]      = "Short Tons"
    df["frequency"]  = "annual"
    df["source"]     = "EIA"
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()

    keep = ["date", "rank_code", "rank_name", "mine_type", "state",
            "value", "units", "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["rank_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5. Coal exports/imports (annual, quantity + price)
# ---------------------------------------------------------------------------

def fetch_coal_trade(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching annual coal trade flows (EIA coal/exports-imports-quantity-price)...")
    rows = _fetch_paginated(
        "coal/exports-imports-quantity-price/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "frequency":          "annual",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={
            "data[]": ["quantity", "price"],
        },
        start_date=start_date,
        label="coal_trade",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date"})
    # Wide response: quantity/price come back as separate columns, not a single
    # "value" column -- "quantity" (short tons) is the headline metric. There's
    # no separate destination/origin pair either, just one countryId plus an
    # exportImportType flag distinguishing the flow direction.
    if "quantity" not in df.columns:
        print(f"  Warning: no 'quantity' column in EIA coal-trade response (columns: {list(df.columns)[:10]})")
        return pd.DataFrame()
    df["date"]        = pd.to_datetime(df["date"], errors="coerce")
    df["value"]        = pd.to_numeric(df["quantity"], errors="coerce")
    df["price"]        = pd.to_numeric(df.get("price"), errors="coerce")
    df["flow_type"]     = df.get("exportImportType", "")
    df["country"]       = df.get("countryDescription", df.get("countryId", ""))
    df["coal_rank"]     = df.get("coalRankId", "")
    df["units"]         = "Short Tons"
    df["frequency"]      = "annual"
    df["source"]         = "EIA"
    df["fetched_at"]     = datetime.datetime.utcnow().isoformat()

    keep = ["date", "flow_type", "country", "coal_rank",
            "value", "price", "units", "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["flow_type", "country", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 6. International energy data (annual, production/consumption/imports/exports)
# ---------------------------------------------------------------------------

def fetch_international(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching annual international energy data (EIA international)...")

    intl_params: dict[str, list[str]] = {
        "facets[activityId][]":      list(INTL_ACTIVITIES.keys()),
        "facets[productId][]":       list(INTL_PRODUCTS.keys()),
        "facets[countryRegionId][]": list(INTL_COUNTRIES.keys()),
    }

    rows = _fetch_paginated(
        "international/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "annual",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params=intl_params,
        start_date=start_date,
        label="international",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "activityId": "activity_code",
                             "productId": "product_code", "countryRegionId": "country_code"})
    if "value" not in df.columns:
        print(f"  Warning: no 'value' column in EIA international response (columns: {list(df.columns)[:10]})")
        return pd.DataFrame()
    df["date"]           = pd.to_datetime(df["date"], errors="coerce")
    df["value"]          = pd.to_numeric(df["value"], errors="coerce")
    df["activity_name"]  = df["activity_code"].map(INTL_ACTIVITIES)
    df["product_name"]   = df["product_code"].map(INTL_PRODUCTS)
    df["country_name"]   = df["country_code"].map(INTL_COUNTRIES)
    df["units"]          = df.get("units", "QBTU")
    df["frequency"]      = "annual"
    df["source"]         = "EIA"
    df["fetched_at"]     = datetime.datetime.utcnow().isoformat()

    keep = ["date", "country_code", "country_name", "activity_code", "activity_name",
            "product_code", "product_name", "value", "units", "frequency",
            "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["country_code", "product_code", "activity_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 7. State Energy Data System (annual, production + consumption by fuel/sector)
# ---------------------------------------------------------------------------

def fetch_seds(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching annual state-level SEDS data (EIA seds)...")
    rows = _fetch_paginated(
        "seds/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "annual",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={
            "facets[stateId][]":  SEDS_STATES,
        },
        start_date=start_date,
        label="seds",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # This dataset's real facets are only seriesId/stateId (no fuelId/sectorId --
    # those don't exist on this endpoint, confirmed live 2026-08-01). seriesId is
    # EIA's 5-char MSN mnemonic (e.g. "ABICB"); positions 1-2/3-4 are a fuel/sector
    # code pair by EIA convention, but decoding them accurately needs EIA's full
    # published MSN reference table, which isn't reproduced here -- fuel_code/
    # sector_code below are a best-effort raw prefix split (grouping key), not an
    # authoritative decode. seriesDescription carries the real human-readable label.
    df = df.rename(columns={"period": "date", "stateId": "state_code",
                             "seriesId": "series_id", "seriesDescription": "series_description"})
    if "value" not in df.columns:
        print(f"  Warning: no 'value' column in EIA SEDS response (columns: {list(df.columns)[:10]})")
        return pd.DataFrame()
    df["date"]        = pd.to_datetime(df["date"], errors="coerce")
    df["value"]       = pd.to_numeric(df["value"], errors="coerce")
    df["fuel_code"]   = df["series_id"].str[:2]
    df["sector_code"] = df["series_id"].str[2:4]
    df["units"]       = df.get("unit", "")
    df["frequency"]   = "annual"
    df["source"]      = "EIA"
    df["fetched_at"]  = datetime.datetime.utcnow().isoformat()

    keep = ["date", "state_code", "fuel_code", "sector_code", "series_id",
            "series_description", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["state_code", "fuel_code", "sector_code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill: bool = False) -> None:
    for d in (ELEC_GEN_DIR, ELEC_SALES_DIR, NUCLEAR_DIR, COAL_PROD_DIR,
              COAL_TRADE_DIR, INTL_DIR, SEDS_DIR):
        os.makedirs(d, exist_ok=True)

    now   = datetime.datetime.utcnow()
    today = now.strftime("%Y%m%d")

    if backfill:
        start_date = None
        mode_tag   = "backfill"
        print("Mode: BACKFILL (full history from EIA)")
    else:
        cutoff_monthly = now - datetime.timedelta(days=INCREMENTAL_MONTHS * 30)
        cutoff_annual  = now - datetime.timedelta(days=INCREMENTAL_YEARS * 365)
        mode_tag = "incremental"
        print(f"Mode: INCREMENTAL (monthly from {cutoff_monthly.strftime('%Y-%m-%d')}, annual from {cutoff_annual.strftime('%Y-%m-%d')})")

    # ── 1. Electricity generation by fuel (monthly) ──────────────────────────
    if backfill:
        gen_start = None
    else:
        gen_start = cutoff_monthly.strftime("%Y-%m-%d")

    df_gen = fetch_electricity_generation(gen_start)
    if not df_gen.empty:
        path = write_partitioned(
            df_gen, ELEC_GEN_DIR,
            f"eia_electricity_generation_{mode_tag}_{today}.parquet",
        )
        n_fuels = df_gen["fuel_code"].nunique()
        d_min   = df_gen["date"].min().strftime("%Y-%m-%d")
        d_max   = df_gen["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_gen):,} rows | {n_fuels} fuel types | {d_min} to {d_max}")
    else:
        print("[!] No electricity generation data returned.")

    time.sleep(REQUEST_INTERVAL)

    # ── 2. Electricity retail sales (monthly) ────────────────────────────────
    if backfill:
        sales_start = None
    else:
        sales_start = cutoff_monthly.strftime("%Y-%m-%d")

    df_sales = fetch_electricity_sales(sales_start)
    if not df_sales.empty:
        path = write_partitioned(
            df_sales, ELEC_SALES_DIR,
            f"eia_electricity_sales_{mode_tag}_{today}.parquet",
        )
        n_states = df_sales["stateid"].nunique() if "stateid" in df_sales.columns else "?"
        d_min    = df_sales["date"].min().strftime("%Y-%m-%d")
        d_max    = df_sales["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_sales):,} rows | {n_states} states | {d_min} to {d_max}")
    else:
        print("[!] No electricity sales data returned.")

    time.sleep(REQUEST_INTERVAL)

    # ── 3. Nuclear outages (daily) ──────────────────────────────────────────
    if backfill:
        nuc_start = None
    else:
        nuc_start = cutoff_monthly.strftime("%Y-%m-%d")

    df_nuc = fetch_nuclear_outages(nuc_start)
    if not df_nuc.empty:
        path = write_partitioned(
            df_nuc, NUCLEAR_DIR,
            f"eia_nuclear_outages_{mode_tag}_{today}.parquet",
        )
        d_min = df_nuc["date"].min().strftime("%Y-%m-%d")
        d_max = df_nuc["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_nuc):,} rows | US aggregate | {d_min} to {d_max}")
    else:
        print("[!] No nuclear outage data returned.")

    time.sleep(REQUEST_INTERVAL)

    # ── 4. Coal production (annual) ─────────────────────────────────────────
    if backfill:
        coal_prod_start = None
    else:
        coal_prod_start = str(now.year - INCREMENTAL_YEARS)

    df_coal_prod = fetch_coal_production(coal_prod_start)
    if not df_coal_prod.empty:
        path = write_partitioned(
            df_coal_prod, COAL_PROD_DIR,
            f"eia_coal_production_{mode_tag}_{today}.parquet",
        )
        n_ranks = df_coal_prod["rank_code"].nunique()
        d_min   = df_coal_prod["date"].min().strftime("%Y-%m-%d")
        d_max   = df_coal_prod["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_coal_prod):,} rows | {n_ranks} coal ranks | {d_min} to {d_max}")
    else:
        print("[!] No coal production data returned.")

    time.sleep(REQUEST_INTERVAL)

    # ── 5. Coal trade (annual) ──────────────────────────────────────────────
    if backfill:
        coal_trade_start = None
    else:
        coal_trade_start = str(now.year - INCREMENTAL_YEARS)

    df_coal_trade = fetch_coal_trade(coal_trade_start)
    if not df_coal_trade.empty:
        path = write_partitioned(
            df_coal_trade, COAL_TRADE_DIR,
            f"eia_coal_trade_{mode_tag}_{today}.parquet",
        )
        n_dest = df_coal_trade["destination"].nunique() if "destination" in df_coal_trade.columns else "?"
        d_min  = df_coal_trade["date"].min().strftime("%Y-%m-%d")
        d_max  = df_coal_trade["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_coal_trade):,} rows | {n_dest} trade partners | {d_min} to {d_max}")
    else:
        print("[!] No coal trade data returned.")

    time.sleep(REQUEST_INTERVAL)

    # ── 6. International energy (annual) ────────────────────────────────────
    if backfill:
        intl_start = None
    else:
        intl_start = str(now.year - INCREMENTAL_YEARS)

    df_intl = fetch_international(intl_start)
    if not df_intl.empty:
        path = write_partitioned(
            df_intl, INTL_DIR,
            f"eia_international_{mode_tag}_{today}.parquet",
        )
        n_countries = df_intl["country_code"].nunique()
        n_products  = df_intl["product_code"].nunique()
        d_min       = df_intl["date"].min().strftime("%Y-%m-%d")
        d_max       = df_intl["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_intl):,} rows | {n_countries} countries | {n_products} products | {d_min} to {d_max}")
    else:
        print("[!] No international energy data returned.")

    time.sleep(REQUEST_INTERVAL)

    # ── 7. State-level SEDS (annual) ────────────────────────────────────────
    if backfill:
        seds_start = None
    else:
        seds_start = str(now.year - INCREMENTAL_YEARS)

    df_seds = fetch_seds(seds_start)
    if not df_seds.empty:
        path = write_partitioned(
            df_seds, SEDS_DIR,
            f"eia_seds_{mode_tag}_{today}.parquet",
        )
        n_states = df_seds["state_code"].nunique()
        d_min    = df_seds["date"].min().strftime("%Y-%m-%d")
        d_max    = df_seds["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_seds):,} rows | {n_states} states | {d_min} to {d_max}")
    else:
        print("[!] No SEDS data returned.")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="EIA expansion pipeline -- electricity, nuclear, coal, international, SEDS"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=f"Fetch full available history. Default: last {INCREMENTAL_MONTHS} months (monthly) / {INCREMENTAL_YEARS} years (annual).",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
