#!/usr/bin/env python3
"""
BEA (Bureau of Economic Analysis) Pipeline.

Fetches US National Income and Product Account (NIPA) data:
  - GDP — real and nominal components (quarterly + annual)
  - Personal income — wages, transfers, disposable income, savings rate (quarterly + monthly)
  - Corporate profits — by industry (annual)

Requires BEA_API_KEY (free — register at https://apps.bea.gov/api/signup/).

CLI:
  python bea_pipeline.py             # last 5 years
  python bea_pipeline.py --backfill  # full history (1929/1947+)

Outputs:
  storage/raw/bea/gdp/bea_gdp_{mode}_{YYYYMMDD}.parquet
  storage/raw/bea/income/bea_income_{mode}_{YYYYMMDD}.parquet
  storage/raw/bea/profits/bea_profits_{mode}_{YYYYMMDD}.parquet
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

BEA_API_KEY      = os.environ.get("BEA_API_KEY", "")
BASE_URL         = "https://apps.bea.gov/api/data"
BASE_DIR         = os.path.join("storage", "raw", "bea")
REQUEST_INTERVAL = 0.7
MAX_RETRIES      = 3

# (table_name, frequency, description)
GDP_TABLES = [
    ("T10101", "Q", "GDP and components, percent change, quarterly SAAR"),
    ("T10105", "Q", "GDP and components, current dollars, quarterly SAAR"),
    ("T10106", "A", "GDP and components, current dollars, annual"),
]

INCOME_TABLES = [
    ("T20100", "Q", "Personal income and its disposition, quarterly"),
    ("T20200", "M", "Personal income and outlays, monthly"),
]

PROFITS_TABLES = [
    ("T60700A", "A", "Corporate profits by industry, annual"),
]


def _get(params):
    params["UserID"]       = BEA_API_KEY
    params["ResultFormat"] = "JSON"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=60)
            if resp.status_code == 200:
                body = resp.json()
                results = body.get("BEAAPI", {}).get("Results")
                if results:
                    return results
                err = body.get("BEAAPI", {}).get("Error")
                if err:
                    print(f"  BEA error: {err}")
                return None
            if resp.status_code == 429:
                time.sleep(60 * attempt)
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:200]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(20 * attempt)
    return None


def _period_to_date(period):
    """Convert BEA TimePeriod string to YYYY-MM-DD."""
    if not period:
        return None
    if "Q" in period:
        yr, q = period.split("Q")
        return f"{yr}-{(int(q) - 1) * 3 + 1:02d}-01"
    if len(period) == 4:
        return f"{period}-01-01"
    if len(period) == 6:
        return f"{period[:4]}-{period[4:6]}-01"
    return period


def fetch_nipa(table_id, frequency, year_start, year_end):
    years = "ALL" if not year_start else f"{year_start},{year_end}"
    result = _get({
        "method":      "GetData",
        "DataSetName": "NIPA",
        "TableName":   table_id,
        "Frequency":   frequency,
        "Year":        years,
    })
    time.sleep(REQUEST_INTERVAL)
    if not result:
        return pd.DataFrame()

    data_list = result.get("Data", [])
    if not data_list:
        return pd.DataFrame()

    rows = []
    for item in data_list:
        val_str = (item.get("DataValue") or "").replace(",", "")
        try:
            value = float(val_str)
        except (ValueError, TypeError):
            value = None
        rows.append({
            "table_id":    table_id,
            "line_number": item.get("LineNumber"),
            "line_name":   item.get("LineDescription"),
            "period":      item.get("TimePeriod"),
            "date":        _period_to_date(item.get("TimePeriod")),
            "value":       value,
            "unit":        item.get("METRIC_NAME", ""),
        })
    return pd.DataFrame(rows)


def run_group(tables, subdir, label, year_start, year_end, fetched_at, today_str, mode):
    os.makedirs(os.path.join(BASE_DIR, subdir), exist_ok=True)
    print(f"[{label}]")
    frames = []
    for table_id, freq, desc in tables:
        print(f"  {table_id} ({freq}): {desc}")
        df = fetch_nipa(table_id, freq, year_start, year_end)
        if not df.empty:
            frames.append(df)
            print(f"    {len(df):,} rows")
        else:
            print(f"    No data returned")

    if not frames:
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["fetched_at"] = fetched_at
    path = write_partitioned(combined, os.path.join(BASE_DIR, subdir),
                             f"bea_{subdir}_{mode}_{today_str}.parquet")
    print(f"  -> {path}  ({len(combined):,} rows)\n")


def main():
    if not BEA_API_KEY:
        print("BEA_API_KEY not set in .env")
        print("Register free at https://apps.bea.gov/api/signup/")
        return

    parser = argparse.ArgumentParser(description="BEA national accounts (GDP, income, profits)")
    parser.add_argument("--backfill", action="store_true",
                        help="Full history (1929/1947+ depending on table)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"
    year_start = "" if args.backfill else str(now.year - 5)
    year_end   = str(now.year)

    print(f"BEA Pipeline  mode={mode}  years={'all' if args.backfill else f'{year_start}-{year_end}'}\n")

    run_group(GDP_TABLES,     "gdp",     "bea_gdp",     year_start, year_end, fetched_at, today_str, mode)
    run_group(INCOME_TABLES,  "income",  "bea_income",  year_start, year_end, fetched_at, today_str, mode)
    run_group(PROFITS_TABLES, "profits", "bea_profits", year_start, year_end, fetched_at, today_str, mode)

    print("--- BEA PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
