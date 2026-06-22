#!/usr/bin/env python3
"""
World Bank Pipeline — Global Macroeconomic Indicators.

Pulls from the World Bank Open Data API (api.worldbank.org/v2).
No API key required. Returns annual data for 200+ countries.

Covers GDP, growth, inflation, trade, employment, debt, population,
poverty, financial development, and environmental/energy indicators
for major economies and key country groups.

CLI:
  python world_bank_pipeline.py             # fetch recent 10 years
  python world_bank_pipeline.py --backfill  # full history from 1960

Output:
  storage/raw/world_bank/world_bank_macro_{mode}_{YYYYMMDD}.parquet
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

BASE_URL = "https://api.worldbank.org/v2"
BASE_DIR = os.path.join("storage", "raw", "world_bank")
REQUEST_INTERVAL = 0.3
MAX_RETRIES = 3
PAGE_SIZE = 1000

# ---------------------------------------------------------------------------
# Countries / economies to pull
# ---------------------------------------------------------------------------

COUNTRIES = [
    # G7 + major economies
    "US", "CN", "JP", "DE", "GB", "FR", "IN", "CA", "AU", "KR",
    "BR", "MX", "RU", "IT", "ES", "NL", "CH", "SE", "NO", "SG",
    "HK", "TW", "SA", "ZA", "NG", "ID", "TR", "AR", "PL", "BE",
    # Country groups / aggregates
    "WLD",   # World
    "EUU",   # European Union
    "OED",   # OECD members
    "EAP",   # East Asia & Pacific
    "ECA",   # Europe & Central Asia
    "LAC",   # Latin America & Caribbean
    "MNA",   # Middle East & North Africa
    "SAS",   # South Asia
    "SSA",   # Sub-Saharan Africa
    "NAC",   # North America
    "HIC",   # High income
    "MIC",   # Middle income
    "LIC",   # Low income
]

# ---------------------------------------------------------------------------
# Indicator catalog — (indicator_code, human name, category)
# ---------------------------------------------------------------------------

INDICATORS = [
    # --- GDP & Growth ---
    ("NY.GDP.MKTP.CD",    "GDP Current USD",                "gdp"),
    ("NY.GDP.MKTP.KD",    "GDP Constant 2015 USD",          "gdp"),
    ("NY.GDP.MKTP.KD.ZG", "GDP Growth Annual Pct",          "gdp"),
    ("NY.GDP.PCAP.CD",    "GDP Per Capita Current USD",     "gdp"),
    ("NY.GDP.PCAP.KD.ZG", "GDP Per Capita Growth Pct",     "gdp"),
    ("NV.AGR.TOTL.ZS",    "Agriculture Value Added Pct GDP","gdp"),
    ("NV.IND.TOTL.ZS",    "Industry Value Added Pct GDP",  "gdp"),
    ("NV.SRV.TOTL.ZS",    "Services Value Added Pct GDP",  "gdp"),
    ("NE.CON.PRVT.ZS",    "Household Consumption Pct GDP", "gdp"),
    ("NE.GDI.TOTL.ZS",    "Gross Capital Formation Pct GDP","gdp"),
    # --- Inflation & Prices ---
    ("FP.CPI.TOTL.ZG",    "Inflation CPI Annual Pct",       "inflation"),
    ("FP.CPI.TOTL",       "CPI Index 2010=100",             "inflation"),
    ("NY.GDP.DEFL.KD.ZG", "GDP Deflator Annual Pct",        "inflation"),
    # --- Trade ---
    ("NE.EXP.GNFS.ZS",    "Exports Pct GDP",                "trade"),
    ("NE.IMP.GNFS.ZS",    "Imports Pct GDP",                "trade"),
    ("NE.EXP.GNFS.CD",    "Exports Current USD",            "trade"),
    ("NE.IMP.GNFS.CD",    "Imports Current USD",            "trade"),
    ("BN.CAB.XOKA.CD",    "Current Account Balance USD",    "trade"),
    ("BN.CAB.XOKA.GD.ZS", "Current Account Balance Pct GDP","trade"),
    ("BX.KLT.DINV.CD.WD", "FDI Net Inflows USD",            "trade"),
    ("BM.KLT.DINV.CD.WD", "FDI Net Outflows USD",           "trade"),
    # --- Labor ---
    ("SL.UEM.TOTL.ZS",    "Unemployment Total Pct",         "labor"),
    ("SL.UEM.TOTL.NE.ZS", "Unemployment NE Pct",           "labor"),
    ("SL.TLF.CACT.ZS",    "Labor Force Participation Pct",  "labor"),
    ("SL.EMP.TOTL.SP.ZS", "Employment to Population Pct",  "labor"),
    ("SL.GDP.PCAP.EM.KD", "GDP Per Worker Constant USD",   "labor"),
    # --- Fiscal & Government ---
    ("GC.DOD.TOTL.GD.ZS", "Central Govt Debt Pct GDP",     "fiscal"),
    ("GC.REV.TOTL.GD.ZS", "Revenue Including Grants Pct GDP","fiscal"),
    ("GC.XPN.TOTL.GD.ZS", "Expenditure Pct GDP",           "fiscal"),
    ("GC.BAL.CASH.GD.ZS", "Cash Surplus Deficit Pct GDP",  "fiscal"),
    # --- Financial ---
    ("FR.INR.RINR",        "Real Interest Rate Pct",        "financial"),
    ("FR.INR.LNDP",        "Interest Rate Spread",          "financial"),
    ("CM.MKT.LCAP.GD.ZS",  "Market Capitalization Pct GDP", "financial"),
    ("CM.MKT.TRNR",        "Stocks Traded Turnover Pct",   "financial"),
    ("FS.AST.DOMS.GD.ZS",  "Domestic Credit Pct GDP",      "financial"),
    ("FD.AST.PRVT.GD.ZS",  "Private Sector Credit Pct GDP","financial"),
    # --- Population & Development ---
    ("SP.POP.TOTL",        "Population Total",              "demographics"),
    ("SP.POP.GROW",        "Population Growth Annual Pct",  "demographics"),
    ("SP.URB.TOTL.IN.ZS",  "Urban Population Pct",         "demographics"),
    ("NY.GNP.PCAP.CD",     "GNI Per Capita USD",           "demographics"),
    ("SI.POV.GINI",        "Gini Coefficient",             "demographics"),
    ("SI.POV.NAHC",        "Poverty Headcount Ratio",      "demographics"),
    # --- Energy & Environment ---
    ("EG.USE.PCAP.KG.OE",  "Energy Use Per Capita kg",     "energy"),
    ("EG.ELC.ACCS.ZS",     "Access to Electricity Pct",   "energy"),
    ("EN.ATM.CO2E.PC",     "CO2 Emissions Per Capita MT",  "energy"),
    ("EG.FEC.RNEW.ZS",     "Renewable Energy Pct Total",   "energy"),
    # --- Technology ---
    ("IT.NET.USER.ZS",     "Internet Users Pct Population","technology"),
    ("IT.CEL.SETS.P2",     "Mobile Subscriptions Per 100", "technology"),
    ("GB.XPD.RSDV.GD.ZS",  "R&D Expenditure Pct GDP",     "technology"),
]


def get_with_backoff(url, params=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 30 * attempt
                print(f"  429. Waiting {wait}s.")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(10 * attempt)
    return None


def fetch_indicator(indicator_code, countries, start_year, end_year):
    """Fetch one indicator for all specified countries in one paginated call."""
    country_str = ";".join(countries)
    url = f"{BASE_URL}/country/{country_str}/indicator/{indicator_code}"
    params = {
        "format": "json",
        "per_page": PAGE_SIZE,
        "date": f"{start_year}:{end_year}",
        "page": 1,
    }

    all_rows = []
    while True:
        data = get_with_backoff(url, params)
        if not data or len(data) < 2:
            break
        meta = data[0] or {}
        records = data[1] or []
        if not records:
            break

        for r in records:
            val = r.get("value")
            if val is None:
                continue
            all_rows.append({
                "country_code": r.get("countryiso3code") or r.get("country", {}).get("id", ""),
                "country_name": r.get("country", {}).get("value", ""),
                "indicator":    indicator_code,
                "date":         r.get("date"),
                "value":        float(val),
            })

        total_pages = meta.get("pages", 1)
        if params["page"] >= total_pages:
            break
        params["page"] += 1
        time.sleep(REQUEST_INTERVAL)

    return all_rows


def main():
    parser = argparse.ArgumentParser(description="World Bank global macro pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full history from 1960")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"
    end_year = now.year
    start_year = 1960 if args.backfill else end_year - 10

    print(f"World Bank Pipeline  mode={mode}  years={start_year}-{end_year}")
    print(f"  {len(COUNTRIES)} countries/groups  x  {len(INDICATORS)} indicators")
    print(f"  (No API key required)\n")

    os.makedirs(BASE_DIR, exist_ok=True)

    all_rows = []
    # Fetch by indicator, batching all countries per request (WB supports multi-country)
    total = len(INDICATORS)
    for i, (code, name, category) in enumerate(INDICATORS, 1):
        print(f"  [{i:02d}/{total}] {code} — {name}...")
        rows = fetch_indicator(code, COUNTRIES, start_year, end_year)
        for row in rows:
            row["indicator_name"] = name
            row["category"] = category
        all_rows.extend(rows)
        time.sleep(REQUEST_INTERVAL)

    if not all_rows:
        print("No data returned.")
        return

    df = pd.DataFrame(all_rows)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    df["fetched_at"] = now.isoformat()
    df = df.sort_values(["indicator", "country_code", "date"])

    path = write_partitioned(
        df, BASE_DIR,
        f"world_bank_macro_{mode}_{today_str}.parquet",
    )
    print(f"\n-> {path}")
    print(f"   {len(df):,} rows  |  {df['indicator'].nunique()} indicators  "
          f"|  {df['country_code'].nunique()} countries")
    print("\n--- WORLD BANK PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
