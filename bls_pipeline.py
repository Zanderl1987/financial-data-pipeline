#!/usr/bin/env python3
"""
BLS (Bureau of Labor Statistics) Pipeline.

Fetches comprehensive labor market data from the BLS Public Data API:
  - CPI — all items, core, shelter, energy, food and sub-components
  - PPI — finished goods, intermediate, crude, services, farm products
  - Employment — nonfarm payrolls by sector, avg hourly earnings, avg weekly hours
  - JOLTS — job openings, hires, quits, layoffs, total separations (levels + rates)
  - Unemployment — U-3 rate, U-6 broader measure, participation, long-term unemployed

Uses API v2 if BLS_API_KEY is in .env (higher limits), else v1 (no key, free).
Register free at https://data.bls.gov/registrationEngine/ to get a v2 key.

CLI:
  python bls_pipeline.py             # incremental (last 2 calendar years)
  python bls_pipeline.py --backfill  # full history back to 2000

Outputs:
  storage/raw/bls/cpi/bls_cpi_{mode}_{YYYYMMDD}.parquet
  storage/raw/bls/ppi/bls_ppi_{mode}_{YYYYMMDD}.parquet
  storage/raw/bls/employment/bls_employment_{mode}_{YYYYMMDD}.parquet
  storage/raw/bls/jolts/bls_jolts_{mode}_{YYYYMMDD}.parquet
  storage/raw/bls/unemployment/bls_unemployment_{mode}_{YYYYMMDD}.parquet
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

BLS_API_KEY = os.environ.get("BLS_API_KEY", "")
BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_URL = BLS_V2 if BLS_API_KEY else BLS_V1

BASE_DIR = os.path.join("storage", "raw", "bls")
REQUEST_INTERVAL = 1.5
MAX_RETRIES = 3
BATCH_SIZE = 50 if BLS_API_KEY else 25  # v2 supports 50 series/request, v1 supports 25

# ---------------------------------------------------------------------------
# Series catalogs — (human name, unit)
# ---------------------------------------------------------------------------

CPI_SERIES = {
    # CPI-U (Urban consumers, seasonally adjusted)
    "CUSR0000SA0":    ("CPI All Urban Consumers",            "Index 1982-84=100"),
    "CUSR0000SA0L1E": ("CPI Core Less Food & Energy",        "Index 1982-84=100"),
    "CUSR0000SAH1":   ("CPI Shelter",                        "Index 1982-84=100"),
    "CUSR0000SEHA":   ("CPI Rent of Primary Residence",      "Index 1982-84=100"),
    "CUSR0000SEHC":   ("CPI Owners Equivalent Rent",         "Index 1982-84=100"),
    "CUSR0000SAE1":   ("CPI Energy",                         "Index 1982-84=100"),
    "CUSR0000SAE2":   ("CPI Energy Commodities",             "Index 1982-84=100"),
    "CUSR0000SETB01": ("CPI Gasoline (All Types)",           "Index 1982-84=100"),
    "CUSR0000SAF1":   ("CPI Food",                           "Index 1982-84=100"),
    "CUSR0000SAF11":  ("CPI Food At Home",                   "Index 1982-84=100"),
    "CUSR0000SEFV":   ("CPI Food Away From Home",            "Index 1982-84=100"),
    "CUSR0000SAM":    ("CPI Medical Care",                   "Index 1982-84=100"),
    "CUSR0000SAT":    ("CPI Transportation",                 "Index 1982-84=100"),
    "CUSR0000SACE":   ("CPI New & Used Motor Vehicles",      "Index 1982-84=100"),
    "CUSR0000SAA":    ("CPI Apparel",                        "Index 1982-84=100"),
    "CUSR0000SARE":   ("CPI Recreation",                     "Index 1982-84=100"),
    "CUSR0000SASED":  ("CPI Education & Communication",      "Index 1982-84=100"),
    # CPI-U not seasonally adjusted (used for official YoY calculations)
    "CUUR0000SA0":    ("CPI-U All Items NSA",                "Index 1982-84=100"),
    "CUUR0000SA0L1E": ("CPI-U Core NSA",                    "Index 1982-84=100"),
}

PPI_SERIES = {
    "WPU00000000":   ("PPI All Commodities",                 "Index 1982=100"),
    "WPSFD4":        ("PPI Finished Goods",                  "Index 1982=100"),
    "WPSFD41":       ("PPI Finished Consumer Goods",         "Index 1982=100"),
    "WPSFD49":       ("PPI Capital Equipment",               "Index 1982=100"),
    "WPSID61":       ("PPI Intermediate Goods",              "Index 1982=100"),
    "WPSID62":       ("PPI Crude Goods",                     "Index 1982=100"),
    "WPSSOP3000":    ("PPI Services",                        "Index Nov 2009=100"),
    "WPSSOP3100":    ("PPI Trade Services",                  "Index Nov 2009=100"),
    "WPU101":        ("PPI Farm Products",                   "Index 1982=100"),
    "WPU06":         ("PPI Fuels & Related Products",        "Index 1982=100"),
    "WPU0561":       ("PPI Gasoline",                        "Index 1982=100"),
    "WPU102":        ("PPI Processed Foods & Feeds",         "Index 1982=100"),
    "WPS141":        ("PPI Metals & Metal Products",              "Index 1982=100"),
    "WPU114":        ("PPI Chemicals & Allied Products",          "Index 1982=100"),
    "WPU15":         ("PPI Rubber & Plastic Products",            "Index 1982=100"),
    "WPU11":         ("PPI Lumber & Wood Products",               "Index 1982=100"),
    # Supply-chain / input-cost extensions
    "PCU325211325211":  ("PPI Plastics Material & Resin Mfg",    "Index 2012=100"),
    "PCU325311325311A": ("PPI Nitrogenous Fertilizer Mfg",       "Index 2012=100"),
    "WPU0652013A":      ("PPI Synthetic Ammonia & Urea",         "Index 1982=100"),
    "WPU061":           ("PPI Industrial Chemicals",              "Index 1982=100"),
    "WPU10":            ("PPI Metals & Metal Products Detailed",  "Index 1982=100"),
    "PCU3334":          ("PPI Computer & Electronic Product Mfg", "Index 2012=100"),
    "WPU0571":          ("PPI Softwood Lumber",                   "Index 1982=100"),
    # Battery and automotive manufacturing — supply chain cost indices
    "PCU331110331110":  ("PPI Iron and Steel Mills",              "Index 2012=100"),
    "PCU331210331210":  ("PPI Steel Product Mfg Purchased Steel", "Index 2012=100"),
    "PCU335911335911":  ("PPI Storage Battery Mfg",               "Index 2012=100"),
    "PCU336111336111":  ("PPI Automobile Manufacturing",          "Index 2012=100"),
    "PCU3363":          ("PPI Motor Vehicle Parts Mfg",           "Index 2012=100"),
    "PCU334413334413":  ("PPI Semiconductor Device Mfg",          "Index 2012=100"),
}

EMPLOYMENT_SERIES = {
    # Nonfarm payrolls by sector (CES — Current Employment Statistics)
    "CES0000000001": ("Total Nonfarm Employment",            "Thousands of Persons"),
    "CES0500000001": ("Total Private Employment",            "Thousands of Persons"),
    "CES1000000001": ("Mining & Logging",                    "Thousands of Persons"),
    "CES2000000001": ("Construction",                        "Thousands of Persons"),
    "CES3000000001": ("Total Manufacturing",                 "Thousands of Persons"),
    "CES3100000001": ("Durable Goods Manufacturing",         "Thousands of Persons"),
    "CES3200000001": ("Nondurable Goods Manufacturing",      "Thousands of Persons"),
    "CES4000000001": ("Trade Transport & Utilities",         "Thousands of Persons"),
    "CES4100000001": ("Wholesale Trade",                     "Thousands of Persons"),
    "CES4142000001": ("Retail Trade",                        "Thousands of Persons"),
    "CES4300000001": ("Transportation & Warehousing",        "Thousands of Persons"),
    "CES5000000001": ("Information",                         "Thousands of Persons"),
    "CES5500000001": ("Financial Activities",                "Thousands of Persons"),
    "CES5552000001": ("Finance & Insurance",                 "Thousands of Persons"),
    "CES6000000001": ("Professional & Business Services",    "Thousands of Persons"),
    "CES6500000001": ("Education & Health Services",         "Thousands of Persons"),
    "CES7000000001": ("Leisure & Hospitality",               "Thousands of Persons"),
    "CES8000000001": ("Other Services",                      "Thousands of Persons"),
    "CES9000000001": ("Government",                          "Thousands of Persons"),
    "CES9091000001": ("Federal Government",                  "Thousands of Persons"),
    "CES9092000001": ("State Government",                    "Thousands of Persons"),
    "CES9093000001": ("Local Government",                    "Thousands of Persons"),
    # Average hourly earnings (private nonfarm)
    "CES0500000003": ("Avg Hourly Earnings Private",         "USD per Hour"),
    "CES0500000008": ("Avg Hourly Earnings Private YoY",     "Percent Change"),
    # Average weekly hours
    "CES0500000002": ("Avg Weekly Hours Private",            "Hours"),
    "CES3000000002": ("Avg Weekly Hours Manufacturing",      "Hours"),
    "CES3000000007": ("Avg Weekly Overtime Hours Mfg",       "Hours"),
}

JOLTS_SERIES = {
    # Total Nonfarm — levels (seasonally adjusted)
    "JTS000000000000000JOL": ("Job Openings Total Level",          "Thousands"),
    "JTS000000000000000HIL": ("Hires Total Level",                 "Thousands"),
    "JTS000000000000000TSL": ("Total Separations Level",           "Thousands"),
    "JTS000000000000000QUL": ("Quits Total Level",                 "Thousands"),
    "JTS000000000000000LDL": ("Layoffs & Discharges Level",        "Thousands"),
    # Total Nonfarm — rates (seasonally adjusted)
    "JTS000000000000000JOR": ("Job Openings Rate",                 "Percent"),
    "JTS000000000000000HIR": ("Hires Rate",                        "Percent"),
    "JTS000000000000000TSR": ("Total Separations Rate",            "Percent"),
    "JTS000000000000000QUR": ("Quits Rate",                        "Percent"),
    "JTS000000000000000LDR": ("Layoffs & Discharges Rate",         "Percent"),
    # By supersector — job openings level
    "JTS100000000000000JOL": ("Job Openings Manufacturing",        "Thousands"),
    "JTS230000000000000JOL": ("Job Openings Construction",         "Thousands"),
    "JTS400000000000000JOL": ("Job Openings Trade/Transport/Util", "Thousands"),
    "JTS510000000000000JOL": ("Job Openings Information",          "Thousands"),
    "JTS540099000000000JOL": ("Job Openings Prof & Bus Services",  "Thousands"),
    "JTS600000000000000JOL": ("Job Openings Ed & Health Services", "Thousands"),
    "JTS700000000000000JOL": ("Job Openings Leisure & Hospitality","Thousands"),
    "JTS900000000000000JOL": ("Job Openings Government",           "Thousands"),
}

UNEMPLOYMENT_SERIES = {
    # U-3 — official unemployment rate
    "LNS14000000": ("Unemployment Rate U-3",                "Percent"),
    "LNS13000000": ("Unemployed Persons Level",             "Thousands"),
    "LNS12000000": ("Civilian Employment Level",            "Thousands"),
    "LNS11000000": ("Civilian Labor Force",                 "Thousands"),
    # Participation
    "LNS11300000": ("Labor Force Participation Rate",       "Percent"),
    "LNS11300001": ("Labor Force Participation Rate Men",   "Percent"),
    "LNS11300002": ("Labor Force Participation Rate Women", "Percent"),
    # By demographic
    "LNS14000006": ("Unemployment Rate Men 20+",            "Percent"),
    "LNS14000003": ("Unemployment Rate Women 20+",          "Percent"),
    "LNS14000012": ("Unemployment Rate 16-19",              "Percent"),
    "LNS14000031": ("Unemployment Rate White",              "Percent"),
    "LNS14000006": ("Unemployment Rate Black or Afr Amer", "Percent"),
    "LNS14000009": ("Unemployment Rate Hispanic or Latino", "Percent"),
    # U-6 (broadest measure: discouraged + marginally attached + part-time econ)
    "LNS13327709": ("U-6 Broader Unemployment",             "Thousands"),
    # Duration
    "LNS13008636": ("Unemployed 27+ Weeks (Long-Term)",     "Thousands"),
    "LNS13008516": ("Unemployed Less Than 5 Weeks",         "Thousands"),
    # Employment-population ratio
    "LNS12300000": ("Employment-Population Ratio",          "Percent"),
}

TABLE_CONFIGS = {
    "bls_cpi":          (CPI_SERIES,          os.path.join(BASE_DIR, "cpi")),
    "bls_ppi":          (PPI_SERIES,          os.path.join(BASE_DIR, "ppi")),
    "bls_employment":   (EMPLOYMENT_SERIES,   os.path.join(BASE_DIR, "employment")),
    "bls_jolts":        (JOLTS_SERIES,        os.path.join(BASE_DIR, "jolts")),
    "bls_unemployment": (UNEMPLOYMENT_SERIES, os.path.join(BASE_DIR, "unemployment")),
}


def fetch_batch(series_ids, start_year, end_year):
    """POST a batch of BLS series IDs; return raw series list from API."""
    payload = {
        "seriesid": series_ids,
        "startyear": str(start_year),
        "endyear": str(end_year),
    }
    if BLS_API_KEY:
        payload["registrationkey"] = BLS_API_KEY
        payload["annualaverage"] = "false"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(BLS_URL, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") != "REQUEST_SUCCEEDED":
                    msgs = data.get("message", [])
                    print(f"  BLS API non-success: {msgs}")
                    return []
                return data.get("Results", {}).get("series", [])
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  429 rate limit. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
                return []
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(30 * attempt)
    return []


def parse_series(raw_series, catalog):
    """Convert BLS API response list to a DataFrame."""
    rows = []
    for s in raw_series:
        sid = s.get("seriesID", "")
        meta = catalog.get(sid)
        if not meta:
            continue
        name, unit = meta
        for obs in s.get("data", []):
            period = obs.get("period", "")
            year_str = obs.get("year", "")
            value_str = obs.get("value", "")
            try:
                value = float(value_str)
                year = int(year_str)
            except (ValueError, TypeError):
                continue
            # Convert BLS period codes to a date
            if period.startswith("M"):
                month = int(period[1:])
                if month > 12:
                    continue  # M13 = annual average — skip
                date_str = f"{year}-{month:02d}-01"
            elif period.startswith("Q"):
                month = (int(period[1:]) - 1) * 3 + 1
                date_str = f"{year}-{month:02d}-01"
            elif period == "A01":
                date_str = f"{year}-01-01"
            else:
                continue
            rows.append({
                "series_id": sid,
                "name":      name,
                "unit":      unit,
                "date":      date_str,
                "period":    period,
                "value":     value,
            })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="BLS labor market data pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full history back to 2000")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    current_year = now.year
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"
    start_year = 2000 if args.backfill else current_year - 2

    print(f"BLS Pipeline  mode={mode}  start={start_year}")
    print(f"API: {'v2 (key present)' if BLS_API_KEY else 'v1 (no key -- add BLS_API_KEY to .env for higher limits)'}\n")

    # BLS API accepts max 20-year spans per request; chunk for backfill
    year_chunks = []
    SPAN = 20
    y = start_year
    while y <= current_year:
        year_chunks.append((y, min(y + SPAN - 1, current_year)))
        y += SPAN

    for table_name, (catalog, output_dir) in TABLE_CONFIGS.items():
        os.makedirs(output_dir, exist_ok=True)
        series_list = list(catalog.keys())
        print(f"[{table_name}]  {len(series_list)} series, {len(year_chunks)} year chunk(s)...")

        all_frames = []
        for y_start, y_end in year_chunks:
            for batch_start in range(0, len(series_list), BATCH_SIZE):
                batch = series_list[batch_start:batch_start + BATCH_SIZE]
                raw = fetch_batch(batch, y_start, y_end)
                if raw:
                    df = parse_series(raw, catalog)
                    if not df.empty:
                        all_frames.append(df)
                time.sleep(REQUEST_INTERVAL)

        if not all_frames:
            print(f"  No data returned.\n")
            continue

        combined = (
            pd.concat(all_frames, ignore_index=True)
            .drop_duplicates(subset=["series_id", "date"])
            .sort_values(["series_id", "date"])
        )
        combined["fetched_at"] = now.isoformat()

        path = write_partitioned(
            combined, output_dir,
            f"{table_name}_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(combined):,} rows, {combined['series_id'].nunique()} series)\n")

    print("--- BLS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
