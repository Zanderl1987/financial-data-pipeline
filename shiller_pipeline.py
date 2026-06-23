#!/usr/bin/env python3
"""
Shiller CAPE Pipeline.

Downloads Robert Shiller's long-run S&P 500 valuation data from Yale:
  - Monthly price, dividends, earnings, CPI, 10-yr Treasury yield
  - CAPE (Cyclically Adjusted Price-Earnings) ratio back to January 1871

No API key required.

CLI:
  python shiller_pipeline.py             # always downloads full history
  python shiller_pipeline.py --backfill  # same (full file only, no incremental)

Output:
  storage/raw/shiller/year=YYYY/month=MM/shiller_cape_{date}.parquet
"""

import argparse
import datetime
import io

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR = "storage/raw/shiller"
DATA_URL = "http://www.econ.yale.edu/~shiller/data/ie_data.xls"


def _parse_date(val) -> str | None:
    """
    Convert Shiller date float (e.g. 1871.01, 2024.1) to YYYY-MM-DD.
    Month fraction: 1871.01 → Jan 1871, 2024.1 → Oct 2024.
    """
    try:
        f = float(val)
        year = int(f)
        frac = round(f - year, 3)
        # Shiller uses 2-digit month fraction: .01=Jan, .02=Feb, ..., .1=Oct, .11=Nov, .12=Dec
        month_str = f"{frac:.2f}".lstrip("0").lstrip(".") or "0"
        month = int(round(float(f"0.{month_str.replace('.','')[:2]}") * 100))
        if month < 1 or month > 12:
            month = max(1, min(12, month))
        return f"{year}-{month:02d}-01"
    except Exception:
        return None


def fetch() -> pd.DataFrame:
    print(f"  GET {DATA_URL}")
    resp = requests.get(DATA_URL, timeout=120)
    resp.raise_for_status()

    # Shiller's Excel has data starting on row 8 (0-indexed: skiprows=7)
    xls = pd.ExcelFile(io.BytesIO(resp.content))
    # Sheet name varies by version; try "Data" first
    sheet = "Data" if "Data" in xls.sheet_names else xls.sheet_names[0]
    raw = pd.read_excel(io.BytesIO(resp.content), sheet_name=sheet, skiprows=7, header=0)

    # Standardise column names — Shiller columns shift occasionally
    raw.columns = [str(c).strip() for c in raw.columns]

    # Drop rows where the date column is missing / non-numeric
    date_col = raw.columns[0]
    raw = raw[pd.to_numeric(raw[date_col], errors="coerce").notna()].copy()
    raw = raw[raw[date_col].astype(str).str.strip() != ""].copy()

    rows = []
    for _, r in raw.iterrows():
        date_str = _parse_date(r.iloc[0])
        if not date_str:
            continue

        def _f(idx):
            try:
                v = r.iloc[idx]
                return float(v) if pd.notna(v) and str(v).strip() not in ("", "nan") else None
            except Exception:
                return None

        rows.append({
            "date":          date_str,
            "price":         _f(1),
            "dividend":      _f(2),
            "earnings":      _f(3),
            "cpi":           _f(4),
            "gs10":          _f(6),   # col 5 is fractional date — skip
            "real_price":    _f(7),
            "real_dividend": _f(8),
            "real_earnings": _f(10),  # col 9 is total return price
            "cape":          _f(11),
        })

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "price"])
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Shiller CAPE long-run S&P 500 valuation")
    parser.add_argument("--backfill", action="store_true", help="No-op: always full history")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"

    print(f"Shiller CAPE Pipeline  mode={mode}\n")
    print("[shiller_cape]")

    import os
    os.makedirs(BASE_DIR, exist_ok=True)

    try:
        df = fetch()
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return

    if df.empty:
        print("  No data returned")
        return

    df["fetched_at"] = fetched_at
    print(f"  {len(df):,} rows  ({df['date'].min()} → {df['date'].max()})")
    path = write_partitioned(df, BASE_DIR, f"shiller_cape_{mode}_{today_str}.parquet")
    print(f"  -> {path}")

    print("\n--- SHILLER CAPE PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
