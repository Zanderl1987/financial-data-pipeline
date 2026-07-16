#!/usr/bin/env python3
"""
BLS Expansion Pipeline — Import/Export Prices, Employment Cost Index, Productivity.

Extends the BLS pipeline with three new tables from the Bureau of Labor Statistics:

  Table 1: bls_import_export_prices (MXP survey — Import/Export Price Indexes)
    - All imports and exports, fuels, ex-fuels, agricultural
    - End-use categories: capital goods, industrial supplies, consumer goods, vehicles, food/feeds

  Table 2: bls_eci (Employment Cost Index — quarterly)
    - Civilian workers, private industry, state/local government
    - Wages & salaries, benefits, total compensation
    - Seasonally adjusted quarterly indexes + 12-month percent changes

  Table 3: bls_productivity (Productivity & Costs — PRS survey)
    - Nonfarm business sector: labor productivity, unit labor costs, hourly compensation, output, hours
    - Manufacturing sector: labor productivity, unit labor costs, hourly compensation, output, hours
    - Multifactor productivity: private nonfarm business + private business sectors

Uses API v2 if BLS_API_KEY is in .env (higher limits), else v1 (no key, free).
Register free at https://data.bls.gov/registrationEngine/ to get a v2 key.

CLI:
  python bls_expansion_pipeline.py             # incremental (last 2 calendar years)
  python bls_expansion_pipeline.py --backfill  # full history back to 1990

Outputs:
  storage/raw/bls/import_export/bls_import_export_prices_{mode}_{YYYYMMDD}.parquet
  storage/raw/bls/eci/bls_eci_{mode}_{YYYYMMDD}.parquet
  storage/raw/bls/productivity/bls_productivity_{mode}_{YYYYMMDD}.parquet
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
BATCH_SIZE = 50 if BLS_API_KEY else 25


# ---------------------------------------------------------------------------
# Table 1: Import/Export Price Indexes (MXP survey)
# ---------------------------------------------------------------------------

IMPORT_EXPORT_SERIES = {
    # Aggregate imports/exports
    "EIUIR":          ("All Imports",                           "Index 2000=100"),
    "EIUIR10":        ("Imports Fuels & Lubricants",            "Index 2000=100"),
    "EIUIREXFUELS":   ("All Imports Excluding Fuels",           "Index 2000=100"),
    "EIUIQ":          ("All Exports",                           "Index 2000=100"),
    "EIUIQAG":        ("Exports Agricultural Commodities",      "Index 2000=100"),
    "EIUIQEXAG":      ("Exports Nonagricultural Commodities",   "Index 2000=100"),
    # Import end-use categories
    "IR":             ("Imports All Commodities (End Use)",     "Index 2000=100"),
    "IR1":            ("Imports Capital Goods",                  "Index 2000=100"),
    "IR2EXCOM":       ("Imports Capital Goods Excl Computers",  "Index 2000=100"),
    "IR3":            ("Imports Industrial Supplies",           "Index 2000=100"),
    "IR31":           ("Imports Industrial Supplies Excl Petroleum", "Index 2000=100"),
    "IR4":            ("Imports Consumer Goods Excl Autos",     "Index 2000=100"),
    "IR400":          ("Imports Apparel Footwear Household",    "Index 2000=100"),
    "IR5":            ("Imports Automotive Vehicles",           "Index 2000=100"),
    "IR6":            ("Imports Foods Feeds Beverages",         "Index 2000=100"),
    # Export end-use categories
    "IQ":             ("Exports All Commodities (End Use)",     "Index 2000=100"),
    "IQ1":            ("Exports Capital Goods",                  "Index 2000=100"),
    "IQ2":            ("Exports Industrial Supplies",           "Index 2000=100"),
    "IQ21":           ("Exports Industrial Supplies Excl Petroleum", "Index 2000=100"),
    "IQ3":            ("Exports Foods Feeds Beverages",         "Index 2000=100"),
    "IQ4":            ("Exports Consumer Goods",                "Index 2000=100"),
    "IQ5":            ("Exports Automotive Vehicles",           "Index 2000=100"),
}


# ---------------------------------------------------------------------------
# Table 2: Employment Cost Index (ECI — quarterly)
# ---------------------------------------------------------------------------

ECI_SERIES = {
    # Civilian workers — seasonally adjusted quarterly indexes
    "CIS1010000000000Q": ("ECI Civilian Wages & Salaries SA",     "Index Dec 2005=100"),
    "CIS1020000000000Q": ("ECI Civilian Benefits SA",             "Index Dec 2005=100"),
    "CIS1030000000000Q": ("ECI Civilian Compensation SA",        "Index Dec 2005=100"),
    # Civilian workers — not seasonally adjusted 12-month pct change
    "CIU1010000000000A": ("ECI Civilian Compensation YoY NSA",   "Percent Change"),
    "CIU1020000000000A": ("ECI Civilian Wages & Salaries YoY NSA", "Percent Change"),
    "CIU1030000000000A": ("ECI Civilian Benefits YoY NSA",       "Percent Change"),
    # Private industry workers — SA quarterly indexes
    "CIS2010000000000Q": ("ECI Private Wages & Salaries SA",     "Index Dec 2005=100"),
    "CIS2020000000000Q": ("ECI Private Benefits SA",             "Index Dec 2005=100"),
    "CIS2030000000000Q": ("ECI Private Compensation SA",        "Index Dec 2005=100"),
    # Private industry — NSA YoY
    "CIU2010000000000A": ("ECI Private Compensation YoY NSA",   "Percent Change"),
    "CIU2020000000000A": ("ECI Private Wages & Salaries YoY NSA", "Percent Change"),
    "CIU2030000000000A": ("ECI Private Benefits YoY NSA",       "Percent Change"),
    # State & local government — SA quarterly indexes
    "CIS3010000000000Q": ("ECI State/Local Wages & Salaries SA",  "Index Dec 2005=100"),
    "CIS3020000000000Q": ("ECI State/Local Benefits SA",          "Index Dec 2005=100"),
    "CIS3030000000000Q": ("ECI State/Local Compensation SA",     "Index Dec 2005=100"),
    # State & local — NSA YoY
    "CIU3010000000000A": ("ECI State/Local Compensation YoY NSA", "Percent Change"),
    "CIU3020000000000A": ("ECI State/Local Wages & Salaries YoY NSA", "Percent Change"),
    "CIU3030000000000A": ("ECI State/Local Benefits YoY NSA",    "Percent Change"),
    # Manufacturing — SA quarterly indexes
    "CIS4010000000000Q": ("ECI Manufacturing Wages & Salaries SA", "Index Dec 2005=100"),
    "CIS4020000000000Q": ("ECI Manufacturing Benefits SA",        "Index Dec 2005=100"),
    "CIS4030000000000Q": ("ECI Manufacturing Compensation SA",   "Index Dec 2005=100"),
    # Manufacturing — NSA YoY
    "CIU4010000000000A": ("ECI Manufacturing Compensation YoY NSA", "Percent Change"),
    "CIU4020000000000A": ("ECI Manufacturing Wages & Salaries YoY NSA", "Percent Change"),
    "CIU4030000000000A": ("ECI Manufacturing Benefits YoY NSA",  "Percent Change"),
    # Nonmanufacturing — SA quarterly indexes
    "CIS5010000000000Q": ("ECI Nonmanufacturing Wages & Salaries SA", "Index Dec 2005=100"),
    "CIS5020000000000Q": ("ECI Nonmanufacturing Benefits SA",        "Index Dec 2005=100"),
    "CIS5030000000000Q": ("ECI Nonmanufacturing Compensation SA",   "Index Dec 2005=100"),
    # Nonmanufacturing — NSA YoY
    "CIU5010000000000A": ("ECI Nonmanufacturing Compensation YoY NSA", "Percent Change"),
    "CIU5020000000000A": ("ECI Nonmanufacturing Wages & Salaries YoY NSA", "Percent Change"),
    "CIU5030000000000A": ("ECI Nonmanufacturing Benefits YoY NSA",  "Percent Change"),
    # Construction — SA quarterly indexes
    "CIS6010000000000Q": ("ECI Construction Wages & Salaries SA", "Index Dec 2005=100"),
    "CIS6020000000000Q": ("ECI Construction Benefits SA",        "Index Dec 2005=100"),
    "CIS6030000000000Q": ("ECI Construction Compensation SA",   "Index Dec 2005=100"),
    # Construction — NSA YoY
    "CIU6010000000000A": ("ECI Construction Compensation YoY NSA", "Percent Change"),
    "CIU6020000000000A": ("ECI Construction Wages & Salaries YoY NSA", "Percent Change"),
    "CIU6030000000000A": ("ECI Construction Benefits YoY NSA",  "Percent Change"),
    # Trade/Transportation/Utilities — SA quarterly indexes
    "CIS7010000000000Q": ("ECI Trade/Transport/Util Wages & Salaries SA", "Index Dec 2005=100"),
    "CIS7020000000000Q": ("ECI Trade/Transport/Util Benefits SA",        "Index Dec 2005=100"),
    "CIS7030000000000Q": ("ECI Trade/Transport/Util Compensation SA",   "Index Dec 2005=100"),
    # Trade/Transportation/Utilities — NSA YoY
    "CIU7010000000000A": ("ECI Trade/Transport/Util Compensation YoY NSA", "Percent Change"),
    "CIU7020000000000A": ("ECI Trade/Transport/Util Wages & Salaries YoY NSA", "Percent Change"),
    "CIU7030000000000A": ("ECI Trade/Transport/Util Benefits YoY NSA",  "Percent Change"),
}


# ---------------------------------------------------------------------------
# Table 3: Productivity & Costs (PRS survey — quarterly)
# ---------------------------------------------------------------------------

PRODUCTIVITY_SERIES = {
    # Nonfarm business sector — percent change at annual rate, SA
    "PRS85006092": ("Nonfarm Labor Productivity",              "Pct Chg Annual Rate SA"),
    "PRS85006042": ("Nonfarm Real Value-Added Output",          "Pct Chg Annual Rate SA"),
    "PRS85006032": ("Nonfarm Hours Worked",                    "Pct Chg Annual Rate SA"),
    "PRS85006102": ("Nonfarm Hourly Compensation",             "Pct Chg Annual Rate SA"),
    "PRS85006112": ("Nonfarm Unit Labor Costs",                "Pct Chg Annual Rate SA"),
    "PRS85006152": ("Nonfarm Unit Nonlabor Costs",             "Pct Chg Annual Rate SA"),
    "PRS85006172": ("Nonfarm Real Hourly Compensation",        "Pct Chg Annual Rate SA"),
    # Manufacturing sector — percent change at annual rate, SA
    "PRS30006092": ("Manufacturing Labor Productivity",        "Pct Chg Annual Rate SA"),
    "PRS30006212": ("Manufacturing Real Sectoral Output",      "Pct Chg Annual Rate SA"),
    "PRS30006032": ("Manufacturing Hours Worked",              "Pct Chg Annual Rate SA"),
    "PRS30006102": ("Manufacturing Hourly Compensation",       "Pct Chg Annual Rate SA"),
    "PRS30006112": ("Manufacturing Unit Labor Costs",          "Pct Chg Annual Rate SA"),
    # Multifactor productivity — annual, index 2017=100, NSA
    "MPU4910012":  ("MFP Private Nonfarm Business TFP",        "Index 2017=100"),
    "MPU4910022":  ("MFP Private Nonfarm Labor Productivity",  "Index 2017=100"),
    "MPU4910032":  ("MFP Private Nonfarm Capital Productivity","Index 2017=100"),
    "MPU4910042":  ("MFP Private Nonfarm Combined Inputs",     "Index 2017=100"),
    "MPU4910052":  ("MFP Private Nonfarm Labor Input",         "Index 2017=100"),
    "MPU4910062":  ("MFP Private Nonfarm Capital Services",    "Index 2017=100"),
    "MPU4910072":  ("MFP Private Nonfarm Capital Intensity",   "Index 2017=100"),
    "MPU4920012":  ("MFP Private Business TFP",                "Index 2017=100"),
    "MPU4920022":  ("MFP Private Business Labor Productivity", "Index 2017=100"),
    "MPU4920032":  ("MFP Private Business Capital Productivity","Index 2017=100"),
    "MPU4920042":  ("MFP Private Business Combined Inputs",    "Index 2017=100"),
    "MPU4920052":  ("MFP Private Business Labor Input",        "Index 2017=100"),
    "MPU4920062":  ("MFP Private Business Capital Services",   "Index 2017=100"),
    "MPU4920072":  ("MFP Private Business Capital Intensity",  "Index 2017=100"),
}


# ---------------------------------------------------------------------------
# Table configs
# ---------------------------------------------------------------------------

TABLE_CONFIGS = {
    "bls_import_export_prices": (IMPORT_EXPORT_SERIES, os.path.join(BASE_DIR, "import_export")),
    "bls_eci":                  (ECI_SERIES,          os.path.join(BASE_DIR, "eci")),
    "bls_productivity":         (PRODUCTIVITY_SERIES,  os.path.join(BASE_DIR, "productivity")),
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
                quarter = int(period[1:])
                if quarter > 4:
                    continue  # Q05 = annual average — skip
                month = (quarter - 1) * 3 + 1
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
    parser = argparse.ArgumentParser(description="BLS expansion pipeline: Import/Export Prices, ECI, Productivity")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full history back to 1990")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    current_year = now.year
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"
    # MXP data goes back to 1993; ECI to 1947; Productivity to 1947
    start_year = 1990 if args.backfill else current_year - 2

    print(f"BLS Expansion Pipeline  mode={mode}  start={start_year}")
    print(f"API: {'v2 (key present)' if BLS_API_KEY else 'v1 (no key -- add BLS_API_KEY to .env for higher limits)'}\n")

    # BLS API accepts max 20-year spans per request (v2/key), 10-year (v1/no key); chunk for backfill
    year_chunks = []
    SPAN = 20 if BLS_API_KEY else 10
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

    print("--- BLS EXPANSION PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
