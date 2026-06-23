#!/usr/bin/env python3
"""
ECB (European Central Bank) Data Pipeline.

Fetches Eurozone financial and monetary data from the ECB Data Portal API.
Completely keyless — no API key required.

Series collected:
  Policy rates    ECB main refinancing rate, deposit facility rate, marginal lending rate
  Interbank rates Euribor 1M, 3M, 6M, 12M (monthly)
  Exchange rates  EUR/USD, EUR/GBP, EUR/JPY, EUR/CHF, EUR/CNY (daily)
  Inflation       HICP (Eurozone CPI) all-items and energy YoY (monthly)
  Yield curve     Eurozone 2Y, 5Y, 10Y, 20Y AAA govt bond yields (monthly)

CLI:
  python ecb_pipeline.py             # last 5 years
  python ecb_pipeline.py --backfill  # full available history (1999+)

Outputs:
  storage/raw/ecb/ecb_rates_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import io
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_URL        = "https://data-api.ecb.europa.eu/service/data"
ECB_DIR         = os.path.join("storage", "raw", "ecb")
REQUEST_INTERVAL = 1.0
MAX_RETRIES     = 3

# (flow, key, series_name, unit)
SERIES = [
    # ECB policy rates (monthly)
    ("FM", "M.U2.EUR.4F.KR.MRR_FR.LEV",    "ECB Main Refinancing Rate",    "Percent per annum"),
    ("FM", "M.U2.EUR.4F.KR.DFR.LEV",        "ECB Deposit Facility Rate",    "Percent per annum"),
    ("FM", "M.U2.EUR.4F.KR.MLF_RT.LEV",     "ECB Marginal Lending Rate",    "Percent per annum"),
    # Euribor (monthly average)
    ("FM", "M.U2.EUR.RT.MM.EURIBOR1MD_.HSTA",  "Euribor 1M",  "Percent per annum"),
    ("FM", "M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA",  "Euribor 3M",  "Percent per annum"),
    ("FM", "M.U2.EUR.RT.MM.EURIBOR6MD_.HSTA",  "Euribor 6M",  "Percent per annum"),
    ("FM", "M.U2.EUR.RT.MM.EURIBOR12MD_.HSTA", "Euribor 12M", "Percent per annum"),
    # Eurozone govt bond yields — AAA-rated (monthly, yield curve model)
    ("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y",  "Eurozone AAA Govt Bond 2Y",  "Percent per annum"),
    ("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_5Y",  "Eurozone AAA Govt Bond 5Y",  "Percent per annum"),
    ("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y", "Eurozone AAA Govt Bond 10Y", "Percent per annum"),
    ("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_20Y", "Eurozone AAA Govt Bond 20Y", "Percent per annum"),
    # EUR exchange rates (daily spot vs EUR base)
    ("EXR", "D.USD.EUR.SP00.A", "EUR/USD Exchange Rate", "USD per EUR"),
    ("EXR", "D.GBP.EUR.SP00.A", "EUR/GBP Exchange Rate", "GBP per EUR"),
    ("EXR", "D.JPY.EUR.SP00.A", "EUR/JPY Exchange Rate", "JPY per EUR"),
    ("EXR", "D.CHF.EUR.SP00.A", "EUR/CHF Exchange Rate", "CHF per EUR"),
    ("EXR", "D.CNY.EUR.SP00.A", "EUR/CNY Exchange Rate", "CNY per EUR"),
    # HICP inflation (monthly, Eurozone)
    ("ICP", "M.U2.N.000000.4.ANR", "HICP All Items YoY",  "Percent change"),
    ("ICP", "M.U2.N.050000.4.ANR", "HICP Energy YoY",     "Percent change"),
    ("ICP", "M.U2.N.010000.4.ANR", "HICP Food YoY",       "Percent change"),
]


def _get_csv(flow, key, start_period):
    url  = f"{BASE_URL}/{flow}/{key}"
    params = {
        "format":      "csvdata",
        "startPeriod": start_period,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=60)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 429:
                time.sleep(60 * attempt)
            elif resp.status_code in (404, 400):
                print(f"  {resp.status_code} for {flow}/{key}")
                return None
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(15 * attempt)
    return None


def _parse_period(period_str):
    """Convert ECB period string (2024-Q1, 2024-01, 2024-01-15) to YYYY-MM-DD."""
    if not period_str:
        return None
    p = period_str.strip()
    if "-Q" in p:
        yr, q = p.split("-Q")
        return f"{yr}-{(int(q) - 1) * 3 + 1:02d}-01"
    if len(p) == 7:  # 2024-01
        return f"{p}-01"
    return p  # already YYYY-MM-DD


def csv_to_df(csv_text, flow, key, series_name, unit, fetched_at):
    if not csv_text or len(csv_text.strip()) < 10:
        return pd.DataFrame()
    try:
        df_raw = pd.read_csv(io.StringIO(csv_text))
    except Exception as exc:
        print(f"  CSV parse error: {exc}")
        return pd.DataFrame()

    # ECB CSV columns: KEY, FREQ, ..., TIME_PERIOD, OBS_VALUE, ...
    if "TIME_PERIOD" not in df_raw.columns or "OBS_VALUE" not in df_raw.columns:
        return pd.DataFrame()

    df_raw = df_raw[["TIME_PERIOD", "OBS_VALUE"]].copy()
    df_raw = df_raw.dropna(subset=["OBS_VALUE"])
    df_raw["OBS_VALUE"] = pd.to_numeric(df_raw["OBS_VALUE"], errors="coerce")
    df_raw = df_raw.dropna(subset=["OBS_VALUE"])

    rows = []
    for _, row in df_raw.iterrows():
        date_str = _parse_period(str(row["TIME_PERIOD"]))
        rows.append({
            "series_id":   f"{flow}/{key}",
            "series_name": series_name,
            "flow":        flow,
            "key":         key,
            "date":        date_str,
            "value":       float(row["OBS_VALUE"]),
            "unit":        unit,
            "fetched_at":  fetched_at,
        })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="ECB data portal pipeline (keyless)")
    parser.add_argument("--backfill", action="store_true",
                        help="Full history since 1999 (ECB launch)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"
    start      = "1999-01" if args.backfill else f"{now.year - 5}-01"

    print(f"ECB Pipeline  mode={mode}  start={start}  series={len(SERIES)}")
    os.makedirs(ECB_DIR, exist_ok=True)

    all_frames = []
    for flow, key, series_name, unit in SERIES:
        short_name = series_name[:45]
        print(f"  [{flow}/{key[:20]}...]  {short_name}")
        csv_text = _get_csv(flow, key, start)
        time.sleep(REQUEST_INTERVAL)
        if csv_text:
            df = csv_to_df(csv_text, flow, key, series_name, unit, fetched_at)
            if not df.empty:
                all_frames.append(df)
                print(f"    {len(df):,} observations")
            else:
                print(f"    No data parsed")
        else:
            print(f"    Skipped (no data)")

    if not all_frames:
        print("\nNo data returned.")
        return

    combined = (
        pd.concat(all_frames, ignore_index=True)
        .drop_duplicates(subset=["series_id", "date"])
        .sort_values(["series_id", "date"])
        .reset_index(drop=True)
    )

    path = write_partitioned(combined, ECB_DIR, f"ecb_rates_{mode}_{today_str}.parquet")
    print(f"\n-> {path}  ({len(combined):,} rows, {combined['series_id'].nunique()} series)")
    print("\n--- ECB PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
