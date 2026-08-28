#!/usr/bin/env python3
"""
Treasury.gov daily par yield curve pipeline — keyless replacement source for
the dead Nasdaq Data Link USTREASURY/YIELD feed.

Writes to the SAME raw directory as the retired Nasdaq Data Link producer so
the existing `treasury_yield_curve` CATALOG glob picks it up unchanged:

  CATALOG table: treasury_yield_curve

Source:
  https://home.treasury.gov/resource-center/data-chart-center/interest-rates/
  daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve
      &field_tdr_date_value={year}&_format=csv
  One CSV per calendar year; daily par yields across all quoted tenors
  (1 Mo .. 30 Yr, including 1.5 Mo / 2 Mo / 4 Mo tenors Nasdaq Data Link
  never carried). History back to ~1990 via per-year URLs. Keyless.

CLI:
  python treasury_curve_pipeline.py                 # current year
  python treasury_curve_pipeline.py --backfill      # 1990 -> current year
  python treasury_curve_pipeline.py --start-year 2000 --end-year 2024

Outputs:
  storage/raw/nasdaq_data_link/yield_curve/year=YYYY/month=MM/treasury_yield_curve_{mode}_{date}.parquet
"""

import argparse
import datetime
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&_format=csv"
)
BASE_DIR = "storage/raw/nasdaq_data_link/yield_curve"
REQUEST_GAP = 1.0
RETRIES = 3
DEFAULT_START_YEAR = 1990

TENOR_COLS = {
    "Date":      "date",
    "1 Mo":      "1mo",
    "1.5 Month": "1_5mo",
    "2 Mo":      "2mo",
    "3 Mo":      "3mo",
    "4 Mo":      "4mo",
    "6 Mo":      "6mo",
    "1 Yr":      "1yr",
    "2 Yr":      "2yr",
    "3 Yr":      "3yr",
    "5 Yr":      "5yr",
    "7 Yr":      "7yr",
    "10 Yr":     "10yr",
    "20 Yr":     "20yr",
    "30 Yr":     "30yr",
}


def _fetch_year(year: int) -> pd.DataFrame:
    """Fetch one year of daily yield-curve CSV rows as a tidy frame."""
    url = BASE_URL.format(year=year)
    last_exc: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, timeout=90, headers={"User-Agent": "financial-data-pipeline/1.0"})
            resp.raise_for_status()
            break
        except Exception as exc:  # noqa: BLE001 - retry any transport error
            last_exc = exc
            time.sleep(2 * attempt)
    else:
        raise RuntimeError(f"{year}: failed after {RETRIES} attempts ({last_exc})")

    from io import StringIO

    df = pd.read_csv(StringIO(resp.text))
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={k: v for k, v in TENOR_COLS.items() if k in df.columns})
    if "date" not in df.columns:
        raise ValueError(f"{year}: no Date column in response")
    df["date"] = pd.to_datetime(df["date"], format="%m/%d/%Y", errors="coerce").dt.strftime("%Y-%m-%d")
    tenors = [v for v in TENOR_COLS.values() if v != "date"]
    for col in tenors:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date"])
    return df[["date"] + [c for c in tenors if c in df.columns]]


def run(mode: str, start_year: int, end_year: int, fetched_at: str) -> None:
    frames = []
    for year in range(start_year, end_year + 1):
        try:
            df = _fetch_year(year)
        except Exception as exc:
            print(f"  {year}: ERROR - {exc}")
            continue
        print(f"  {year}: {len(df):,} rows")
        if not df.empty:
            frames.append(df)
        time.sleep(REQUEST_GAP)

    if not frames:
        print("  No yield-curve data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["date"], keep="last")
    combined = combined.sort_values("date").reset_index(drop=True)
    combined["fetched_at"] = fetched_at
    path = write_partitioned(combined, BASE_DIR, f"treasury_yield_curve_{mode}_{fetched_at[:10].replace('-', '')}.parquet")
    print(f"  -> {path}  ({len(combined):,} rows)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Treasury.gov daily par yield curve (keyless)")
    parser.add_argument("--backfill", action="store_true", help=f"Full history from {DEFAULT_START_YEAR}")
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    end_year = args.end_year or now.year
    start_year = args.start_year or (DEFAULT_START_YEAR if args.backfill else now.year)
    mode = "backfill" if args.backfill else "incremental"
    fetched_at = now.isoformat()

    print(f"Treasury Yield Curve Pipeline  mode={mode}  years={start_year}-{end_year}")
    run(mode, start_year, end_year, fetched_at)
    print("--- TREASURY YIELD CURVE PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
