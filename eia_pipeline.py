"""
EIA Expanded Pipeline — weekly petroleum inventories, weekly natural gas storage,
monthly crude oil production by state.

Supplements gas_price_pipeline.py (spot + retail gas/diesel prices).
Uses the EIA Open Data API v2 — same key as the gas pipeline.
Add EIA_API_KEY to .env.

Outputs:
  storage/raw/eia/petroleum_stocks/**/*.parquet    (CATALOG: eia_petroleum_stocks)
  storage/raw/eia/natgas_storage/**/*.parquet      (CATALOG: eia_natgas_storage)
  storage/raw/eia/crude_production/**/*.parquet    (CATALOG: eia_crude_production)
  storage/raw/eia/refinery_activity/**/*.parquet   (CATALOG: eia_refinery_activity)
  storage/raw/eia/crude_trade/**/*.parquet         (CATALOG: eia_crude_trade)

Usage:
  python eia_pipeline.py              # incremental (last 6 months)
  python eia_pipeline.py --backfill   # full history from EIA
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
STOCKS_DIR    = os.path.join(EIA_DIR, "petroleum_stocks")
NATGAS_DIR    = os.path.join(EIA_DIR, "natgas_storage")
CRUDE_DIR     = os.path.join(EIA_DIR, "crude_production")
REFINERY_DIR  = os.path.join(EIA_DIR, "refinery_activity")
TRADE_DIR     = os.path.join(EIA_DIR, "crude_trade")

REQUEST_INTERVAL = 0.25   # 240 req/min — EIA suspends keys on abuse
MAX_RETRIES      = 3
BACKOFF_SECONDS  = 60
PAGE_SIZE        = 5000   # EIA v2 max rows per request

INCREMENTAL_MONTHS = 6

# ---------------------------------------------------------------------------
# Petroleum weekly stock series (route: petroleum/stoc/wstk)
# Units: thousand barrels; frequency: weekly (Friday)
# ---------------------------------------------------------------------------
STOCK_SERIES: dict[str, str] = {
    "WCRSTUS1": "Crude Oil Inventories (Excl. SPR)",
    "WCESTP40": "Crude Oil, Cushing OK (WTI Delivery Point)",
    "WTTSTUS1": "Motor Gasoline, Total",
    "WDISTUS1": "Distillate Fuel Oil, Total",
    "WKJXUS1A": "Kerosene-Type Jet Fuel",
    "WRESTUS1": "Residual Fuel Oil",
    "WPRSTUS1": "Propane/Propylene",
}

# ---------------------------------------------------------------------------
# Natural gas underground storage (route: natural-gas/stor/sum)
# Units: BCF; frequency: weekly (Friday)
# Facets: process=SWO (working gas storage net change), duoarea codes
# ---------------------------------------------------------------------------
NATGAS_DUOAREAS: dict[str, str] = {
    "R48": "Lower 48 States",
    "R31": "East Region",
    "R32": "Midwest Region",
    "R33": "South Central Region",
    "R34": "Mountain Region",
    "R35": "Pacific Region",
}

# ---------------------------------------------------------------------------
# Crude oil production series (route: petroleum/crd/crpdn)
# Units: thousand barrels per day; frequency: monthly
# ---------------------------------------------------------------------------
CRUDE_SERIES: dict[str, str] = {
    "MCRFPUS2": "US Total Field Production",
    "MCRFPTX2": "Texas",
    "MCRFPND2": "North Dakota",
    "MCRFPNM2": "New Mexico",
    "MCRFPAK2": "Alaska",
    "MCRFPCO2": "Colorado",
    "MCRFPCA2": "California",
    "MCRFPOK2": "Oklahoma",
    "MCRFPWY2": "Wyoming",
}

# ---------------------------------------------------------------------------
# Refinery activity (route: petroleum/pnp/wiup)
# Units: thousand barrels per day (inputs), percent (utilization); weekly
# ---------------------------------------------------------------------------
REFINERY_SERIES: dict[str, str] = {
    "WCRRIUS2": "US Refiner Net Input of Crude Oil",
    "WGIRIUS2": "US Gross Inputs into Refineries",
    "WPULEUS3": "US Percent Utilization of Refinery Operable Capacity",
}

# ---------------------------------------------------------------------------
# Crude oil imports/exports (route: petroleum/move/wkly)
# Units: thousand barrels per day; weekly
# ---------------------------------------------------------------------------
CRUDE_TRADE_SERIES: dict[str, str] = {
    "WCEIMUS2": "US Commercial Crude Oil Imports (Excl. SPR)",
    "WCREXUS2": "US Crude Oil Exports",
}


# ---------------------------------------------------------------------------
# HTTP helpers
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
# Petroleum stocks (weekly)
# ---------------------------------------------------------------------------

def fetch_petroleum_stocks(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching weekly petroleum stocks (EIA petroleum/stoc/wstk)...")
    rows = _fetch_paginated(
        "petroleum/stoc/wstk/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "weekly",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={"facets[series][]": list(STOCK_SERIES.keys())},
        start_date=start_date,
        label="stocks",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "series": "series_id"})
    df["date"]        = pd.to_datetime(df["date"], errors="coerce")
    df["value"]       = pd.to_numeric(df["value"], errors="coerce")
    df["series_name"] = df["series_id"].map(STOCK_SERIES)
    df["units"]       = df.get("units", "Thousand Barrels")
    df["frequency"]   = "weekly"
    df["source"]      = "EIA"
    df["fetched_at"]  = datetime.datetime.utcnow().isoformat()

    keep = ["series_id", "series_name", "date", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["series_id", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Natural gas storage (weekly)
# ---------------------------------------------------------------------------

def fetch_natgas_storage(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching weekly natural gas storage (EIA natural-gas/stor/wkly)...")
    rows = _fetch_paginated(
        "natural-gas/stor/wkly/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "facets[process][]":  "SWO",      # total working gas (excl. salt/non-salt split)
            "facets[product][]":  "EPG0",     # natural gas
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={"facets[duoarea][]": list(NATGAS_DUOAREAS.keys())},
        start_date=start_date,
        label="natgas",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date"})
    df["date"]        = pd.to_datetime(df["date"], errors="coerce")
    df["value"]       = pd.to_numeric(df["value"], errors="coerce")
    df["region_name"] = df["duoarea"].map(NATGAS_DUOAREAS) if "duoarea" in df.columns else ""
    if "area-name" in df.columns:
        df["region_name"] = df["region_name"].fillna(df["area-name"])
    df["units"]      = "BCF"
    df["frequency"]  = "weekly"
    df["source"]     = "EIA"
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()

    keep = ["duoarea", "region_name", "date", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["duoarea", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Crude oil production (monthly)
# ---------------------------------------------------------------------------

def fetch_crude_production(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching monthly crude oil production (EIA petroleum/crd/crpdn)...")
    rows = _fetch_paginated(
        "petroleum/crd/crpdn/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "monthly",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={"facets[series][]": list(CRUDE_SERIES.keys())},
        start_date=start_date,
        label="production",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "series": "series_id"})
    df["date"]        = pd.to_datetime(df["date"], errors="coerce")
    df["value"]       = pd.to_numeric(df["value"], errors="coerce")
    df["series_name"] = df["series_id"].map(CRUDE_SERIES)
    df["units"]       = "Thousand Barrels per Day"
    df["frequency"]   = "monthly"
    df["source"]      = "EIA"
    df["fetched_at"]  = datetime.datetime.utcnow().isoformat()

    keep = ["series_id", "series_name", "date", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["series_id", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Refinery activity (weekly)
# ---------------------------------------------------------------------------

def fetch_refinery_activity(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching weekly refinery activity (EIA petroleum/pnp/wiup)...")
    rows = _fetch_paginated(
        "petroleum/pnp/wiup/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "weekly",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={"facets[series][]": list(REFINERY_SERIES.keys())},
        start_date=start_date,
        label="refinery",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "series": "series_id"})
    df["date"]        = pd.to_datetime(df["date"], errors="coerce")
    df["value"]       = pd.to_numeric(df["value"], errors="coerce")
    df["series_name"] = df["series_id"].map(REFINERY_SERIES)
    df["units"]       = df.get("units", "")
    df["frequency"]   = "weekly"
    df["source"]      = "EIA"
    df["fetched_at"]  = datetime.datetime.utcnow().isoformat()

    keep = ["series_id", "series_name", "date", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["series_id", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Crude oil imports/exports (weekly)
# ---------------------------------------------------------------------------

def fetch_crude_trade(start_date: str | None = None) -> pd.DataFrame:
    print("Fetching weekly crude oil imports/exports (EIA petroleum/move/wkly)...")
    rows = _fetch_paginated(
        "petroleum/move/wkly/data/",
        base_params={
            "api_key":            EIA_API_KEY,
            "data[]":             "value",
            "frequency":          "weekly",
            "sort[0][column]":    "period",
            "sort[0][direction]": "asc",
        },
        list_params={"facets[series][]": list(CRUDE_TRADE_SERIES.keys())},
        start_date=start_date,
        label="crude_trade",
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"period": "date", "series": "series_id"})
    df["date"]        = pd.to_datetime(df["date"], errors="coerce")
    df["value"]       = pd.to_numeric(df["value"], errors="coerce")
    df["series_name"] = df["series_id"].map(CRUDE_TRADE_SERIES)
    df["units"]       = df.get("units", "Thousand Barrels per Day")
    df["frequency"]   = "weekly"
    df["source"]      = "EIA"
    df["fetched_at"]  = datetime.datetime.utcnow().isoformat()

    keep = ["series_id", "series_name", "date", "value", "units",
            "frequency", "source", "fetched_at"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].dropna(subset=["date", "value"])
    return df.sort_values(["series_id", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill: bool = False) -> None:
    for d in (STOCKS_DIR, NATGAS_DIR, CRUDE_DIR, REFINERY_DIR, TRADE_DIR):
        os.makedirs(d, exist_ok=True)

    now   = datetime.datetime.utcnow()
    today = now.strftime("%Y%m%d")

    if backfill:
        start_date = None
        mode_tag   = "backfill"
        print("Mode: BACKFILL (full history from EIA)")
    else:
        cutoff     = now - datetime.timedelta(days=INCREMENTAL_MONTHS * 30)
        start_date = cutoff.strftime("%Y-%m-%d")
        mode_tag   = "incremental"
        print(f"Mode: INCREMENTAL (from {start_date})")

    # ── Petroleum stocks ───────────────────────────────────────────────────────
    df_stocks = fetch_petroleum_stocks(start_date)
    if not df_stocks.empty:
        path = write_partitioned(
            df_stocks, STOCKS_DIR,
            f"eia_petroleum_stocks_{mode_tag}_{today}.parquet",
        )
        n_series = df_stocks["series_id"].nunique()
        d_min    = df_stocks["date"].min().strftime("%Y-%m-%d")
        d_max    = df_stocks["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_stocks):,} rows | {n_series} series | {d_min} to {d_max}")
    else:
        print("[!] No petroleum stock data returned.")

    time.sleep(REQUEST_INTERVAL)

    # ── Natural gas storage ────────────────────────────────────────────────────
    df_natgas = fetch_natgas_storage(start_date)
    if not df_natgas.empty:
        path = write_partitioned(
            df_natgas, NATGAS_DIR,
            f"eia_natgas_storage_{mode_tag}_{today}.parquet",
        )
        n_regions = df_natgas["duoarea"].nunique() if "duoarea" in df_natgas.columns else "?"
        d_min     = df_natgas["date"].min().strftime("%Y-%m-%d")
        d_max     = df_natgas["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_natgas):,} rows | {n_regions} regions | {d_min} to {d_max}")
    else:
        print("[!] No natural gas storage data returned.")

    time.sleep(REQUEST_INTERVAL)

    # ── Crude oil production ───────────────────────────────────────────────────
    df_crude = fetch_crude_production(start_date)
    if not df_crude.empty:
        path = write_partitioned(
            df_crude, CRUDE_DIR,
            f"eia_crude_production_{mode_tag}_{today}.parquet",
        )
        n_series = df_crude["series_id"].nunique()
        d_min    = df_crude["date"].min().strftime("%Y-%m-%d")
        d_max    = df_crude["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_crude):,} rows | {n_series} states/regions | {d_min} to {d_max}")
    else:
        print("[!] No crude production data returned.")

    time.sleep(REQUEST_INTERVAL)

    # ── Refinery activity ──────────────────────────────────────────────────────
    df_refinery = fetch_refinery_activity(start_date)
    if not df_refinery.empty:
        path = write_partitioned(
            df_refinery, REFINERY_DIR,
            f"eia_refinery_activity_{mode_tag}_{today}.parquet",
        )
        n_series = df_refinery["series_id"].nunique()
        d_min    = df_refinery["date"].min().strftime("%Y-%m-%d")
        d_max    = df_refinery["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_refinery):,} rows | {n_series} series | {d_min} to {d_max}")
    else:
        print("[!] No refinery activity data returned.")

    time.sleep(REQUEST_INTERVAL)

    # ── Crude oil imports/exports ──────────────────────────────────────────────
    df_trade = fetch_crude_trade(start_date)
    if not df_trade.empty:
        path = write_partitioned(
            df_trade, TRADE_DIR,
            f"eia_crude_trade_{mode_tag}_{today}.parquet",
        )
        n_series = df_trade["series_id"].nunique()
        d_min    = df_trade["date"].min().strftime("%Y-%m-%d")
        d_max    = df_trade["date"].max().strftime("%Y-%m-%d")
        print(f"[+] {path}")
        print(f"    {len(df_trade):,} rows | {n_series} series | {d_min} to {d_max}")
    else:
        print("[!] No crude trade data returned.")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="EIA expanded pipeline -- petroleum inventories, natural gas storage, crude production"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=f"Fetch full available history. Default: last {INCREMENTAL_MONTHS} months.",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
