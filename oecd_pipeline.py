#!/usr/bin/env python3
"""
OECD Macro Indicators Pipeline (revised 2026-07-29).

Uses the OECD SDMX 2.1 REST API via CSV format (the old stats.oecd.org/SDMX-JSON
endpoint was decommissioned Jan 2025).

Two dataflows:
  - OECD.SDD.STES/DSD_KEI@DF_KEI (Key Economic Indicators) for most series
  - OECD.SDD.TPS/DSD_LFS@DF_IALFS_UNE_M (Labour Force Survey) for unemployment

Keyless — no API key required.

Indicators:
  UNEMP     Unemployment rate (%, monthly, SA)
  CP        CPI inflation (YoY %, monthly)
  MANM      Industrial production index (2015=100, monthly, SA)
  IRLT      Long-term interest rate (%, monthly)
  IR3TIB    Short-term interest rate (%, monthly)
  B1GQ_Q    GDP growth (YoY %, quarterly, SA)
  CC        Consumer confidence (normal=100, monthly, SA)
  CA_GDP    Current account balance (% of GDP, quarterly)

Countries: USA GBR DEU FRA JPN CAN ITA ESP NLD CHE SWE AUS KOR CHN

CLI:
  python oecd_pipeline.py             # last 5 years
  python oecd_pipeline.py --backfill  # full available history

Outputs:
  storage/raw/oecd/oecd_macro_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import io
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_KEI       = "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_KEI@DF_KEI,4.0"
BASE_LFS       = "https://sdmx.oecd.org/public/rest/data/OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0"
OECD_DIR       = os.path.join("storage", "raw", "oecd")
REQUEST_DELAY  = 1.5
MAX_RETRIES    = 3

COUNTRIES = ["USA", "GBR", "DEU", "FRA", "JPN", "CAN", "ITA", "ESP", "NLD", "CHE", "SWE", "AUS", "KOR", "CHN"]

INDICATORS = [
    {
        "id": "LRHUTTTT",
        "description": "Unemployment Rate",
        "unit": "Percent",
        "base_url": BASE_LFS,
        "dims": lambda c: f"{c}.UNE_LF_M.PT_LF_SUB._Z.N._T.Y_GE15._Z.M",
        "freq": "M",
    },
    {
        "id": "CP01000",
        "description": "CPI Total YoY",
        "unit": "Percent change",
        "base_url": BASE_KEI,
        "dims": lambda c: f"{c}.M.CP.GR._Z._Z.GY",
        "freq": "M",
    },
    {
        "id": "PRMNTO01",
        "description": "Industrial Production Index",
        "unit": "Index 2015=100",
        "base_url": BASE_KEI,
        "dims": lambda c: f"{c}.M.MANM.IX._Z.Y._Z",
        "freq": "M",
    },
    {
        "id": "IRLT",
        "description": "Long-Term Interest Rate 10Y Govt Bond",
        "unit": "Percent per annum",
        "base_url": BASE_KEI,
        "dims": lambda c: f"{c}.M.IRLT.PA._Z._Z._Z",
        "freq": "M",
    },
    {
        "id": "IR3TBB01",
        "description": "Short-Term Interest Rate 3M Interbank",
        "unit": "Percent per annum",
        "base_url": BASE_KEI,
        "dims": lambda c: f"{c}.M.IR3TIB.PA._Z._Z._Z",
        "freq": "M",
    },
    {
        "id": "NAEXKP01",
        "description": "GDP Growth Rate Quarterly Volume",
        "unit": "Percent change",
        "base_url": BASE_KEI,
        "dims": lambda c: f"{c}.Q.B1GQ_Q.GR._T.Y.GY",
        "freq": "Q",
    },
    {
        "id": "CCRETT01",
        "description": "Consumer Confidence Index",
        "unit": "Normal=100",
        "base_url": BASE_KEI,
        "dims": lambda c: f"{c}.M.CC.XDC_USD._Z._Z._Z",
        "freq": "M",
    },
    {
        "id": "BPBLTD02",
        "description": "Current Account Balance Pct GDP",
        "unit": "Percent of GDP",
        "base_url": BASE_KEI,
        "dims": lambda c: f"{c}.Q.CA_GDP.PT_B1GQ._T.Y._Z",
        "freq": "Q",
    },
]


def _fetch_csv(url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"    Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            print(f"    HTTP {resp.status_code}: {resp.text[:120]}")
            return None
        except requests.RequestException as exc:
            print(f"    Request error (attempt {attempt}): {exc}")
            time.sleep(20 * attempt)
    return None


def _parse_csv(csv_text, indicator_id, description, unit):
    if not csv_text:
        return pd.DataFrame()
    try:
        df = pd.read_csv(io.StringIO(csv_text))
    except Exception as exc:
        print(f"    CSV parse error: {exc}")
        return pd.DataFrame()

    obs_col = "OBS_VALUE"
    if obs_col not in df.columns:
        return pd.DataFrame()

    data = df[df[obs_col].notna()].copy()
    if data.empty:
        return pd.DataFrame()

    country_col = "REF_AREA"
    time_col = "TIME_PERIOD"
    if country_col not in data.columns or time_col not in data.columns:
        return pd.DataFrame()

    def _normalize_date(period):
        if "-Q" in str(period):
            yr, q = str(period).split("-Q")
            return f"{yr}-{(int(q) - 1) * 3 + 1:02d}-01"
        if len(str(period)) == 4:
            return f"{period}-01-01"
        if len(str(period)) == 7 and "-" in str(period):
            return f"{period}-01"
        return str(period)

    data["country_code"] = data[country_col]
    data["indicator"] = indicator_id
    data["description"] = description
    data["date"] = data[time_col].apply(_normalize_date)
    data["value"] = pd.to_numeric(data[obs_col], errors="coerce")
    data["unit"] = unit

    result = data[["country_code", "indicator", "description", "date", "value", "unit"]].dropna(subset=["value"]).copy()
    result["value"] = result["value"].astype(float)
    return result.reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description="OECD macro indicators pipeline (keyless)")
    parser.add_argument("--backfill", action="store_true", help="Fetch full available history")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode = "backfill" if args.backfill else "incremental"
    start_period = "1960" if args.backfill else f"{now.year - 5}"

    print(f"OECD Pipeline  mode={mode}  start={start_period}")
    print(f"Countries: {', '.join(COUNTRIES)}\n")
    os.makedirs(OECD_DIR, exist_ok=True)

    all_frames = []
    for ind in INDICATORS:
        iid = ind["id"]
        desc = ind["description"]
        print(f"  [{iid}] {desc}...")

        country_frames = []
        for country in COUNTRIES:
            dims = ind["dims"](country)
            url = f"{ind['base_url']}/{dims}?startPeriod={start_period}&format=csvfilewithlabels"
            csv_text = _fetch_csv(url)
            df = _parse_csv(csv_text, iid, desc, ind["unit"])
            if not df.empty:
                country_frames.append(df)
            time.sleep(REQUEST_DELAY)

        if country_frames:
            combined = pd.concat(country_frames, ignore_index=True)
            all_frames.append(combined)
            print(f"    {len(combined):,} rows, {combined['country_code'].nunique()} countries")
        else:
            print(f"    No data returned")

    if not all_frames:
        print("\nNo data returned.")
        return

    combined = (
        pd.concat(all_frames, ignore_index=True)
        .drop_duplicates(subset=["country_code", "indicator", "date"])
        .sort_values(["indicator", "country_code", "date"])
        .reset_index(drop=True)
    )
    combined["fetched_at"] = fetched_at

    path = write_partitioned(combined, OECD_DIR, f"oecd_macro_{mode}_{today_str}.parquet")
    print(f"\n-> {path}  ({len(combined):,} rows, {combined['indicator'].nunique()} indicators, "
          f"{combined['country_code'].nunique()} countries)")
    print("\n--- OECD PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
