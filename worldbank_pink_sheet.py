#!/usr/bin/env python3
"""
World Bank Pink Sheet Pipeline — Monthly Commodity Prices.

Downloads the World Bank Commodity Price Data (Pink Sheet) Excel workbook
and parses the "Monthly Prices" tab, which covers ~70 commodities back to 1960.

No API key required. Tries multiple known URL patterns and falls back to the
World Bank Data API (source 96 — GEM Commodities) if Excel download fails.

CLI:
  python worldbank_pink_sheet.py             # download + parse latest Pink Sheet
  python worldbank_pink_sheet.py --backfill  # same (all history always included)

Output:
  storage/raw/worldbank_pink/wb_commodities_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import io
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR    = os.path.join("storage", "raw", "worldbank_pink")
MAX_RETRIES = 3
REQUEST_INTERVAL = 0.5

# Known Pink Sheet Excel URLs — try in order (most recent first)
# Note: URL hash changes monthly; update the first entry each month
# Find updated URL at: https://www.worldbank.org/en/research/commodity-markets
PINK_SHEET_URLS = [
    # June 2026 edition
    "https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/CMO-Historical-Data-Monthly.xlsx",
    # 2025 edition fallback
    "https://thedocs.worldbank.org/en/doc/18675f1d1639c7a34d463f59263ba0a2-0050012025/original/CMO-Historical-Data-Monthly.xlsx",
    # 2024 edition fallback
    "https://thedocs.worldbank.org/en/doc/5d903e848db1d1b83e0ec8f744e55570-0350012021/original/CMO-Historical-Data-Monthly.xlsx",
]

# World Bank API fallback (GEM Commodities, source 96)
WB_API_BASE = "https://api.worldbank.org/v2"
WB_COMMODITY_INDICATORS = {
    "PMAIZMTUSA": ("Maize",           "USD/MT"),
    "PWHEAMTUSA": ("Wheat US HRW",    "USD/MT"),
    "PSOYBUSDM":  ("Soybeans",        "USD/MT"),
    "PCOTTINDUSDM": ("Cotton",        "USD/kg"),
    "PSUGAISAUSDM": ("Sugar",         "USD/kg"),
    "PCOFFOTMUSDM": ("Coffee Arabica","USD/kg"),
    "PCOAOAUSDM":   ("Cocoa",         "USD/MT"),
    "DCOILWTICO":   ("WTI Crude Oil", "USD/barrel"),
    "PNGASJPUSDM":  ("Natural Gas Japan LNG", "USD/MMBtu"),
    "PCOALAUUSDM":  ("Coal Australian", "USD/MT"),
    "PCOPPUSDM":    ("Copper",         "USD/MT"),
    "PALUMUSDM":    ("Aluminum",       "USD/MT"),
    "PNICKUSDM":    ("Nickel",         "USD/MT"),
    "GOLDPMGBD228NLBM": ("Gold",       "USD/troy oz"),
    "PSILVERUSDM":  ("Silver",         "USD/troy oz"),
}


def download_pink_sheet():
    """Attempt to download the Pink Sheet Excel from known URLs. Returns bytes or None."""
    headers = {"User-Agent": "Mozilla/5.0 financial-data-pipeline/1.0"}
    for url in PINK_SHEET_URLS:
        try:
            print(f"  Trying: {url}")
            r = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 10_000:
                print(f"  Downloaded {len(r.content):,} bytes.")
                return r.content
            print(f"  HTTP {r.status_code} or empty response.")
        except requests.RequestException as e:
            print(f"  Error: {e}")
        time.sleep(REQUEST_INTERVAL)
    return None


def parse_pink_sheet(content):
    """
    Parse the 'Monthly Prices' sheet from Pink Sheet Excel bytes.

    Pink Sheet layout (as of 2026):
      Rows 0-3 : title / metadata text
      Row 4    : commodity names (NaN in col 0)
      Row 5    : units in parentheses  (NaN in col 0)
      Row 6+   : data, col 0 = "1960M01" style date strings
    """
    try:
        xl = pd.ExcelFile(io.BytesIO(content))
        sheet_name = next(
            (n for n in xl.sheet_names if "month" in n.lower() and "price" in n.lower()),
            None,
        ) or next((n for n in xl.sheet_names if "month" in n.lower()), xl.sheet_names[0])
        print(f"  Parsing sheet: {sheet_name}")

        raw = xl.parse(sheet_name, header=None, dtype=str)

        # Locate the commodity-name header row: first row where col-0 is NaN/empty
        # and col-1 has a non-empty string (commodity name starts in col 1)
        header_row = 4  # default for the known 2026 format
        for r in range(min(10, len(raw))):
            c0 = str(raw.iloc[r, 0]).strip()
            c1 = str(raw.iloc[r, 1]).strip() if raw.shape[1] > 1 else ""
            if (c0 in ("", "nan")) and c1 not in ("", "nan"):
                header_row = r
                break

        # Row after header row is the units row — skip it (skiprows=[header_row+1])
        skip = [header_row + 1]
        df = xl.parse(sheet_name, header=header_row, index_col=0, skiprows=skip)

        # Date index is "1960M01" format
        df.index = pd.to_datetime(
            df.index.astype(str).str.strip(),
            format="%YM%m",
            errors="coerce",
        )
        df = df[df.index.notna()]
        df.index.name = "date"

        # Melt to long format
        df = df.reset_index()
        df = df.melt(id_vars=["date"], var_name="commodity", value_name="value")
        df["commodity"] = df["commodity"].astype(str).str.strip()
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna(subset=["value", "date"])
        df = df[df["commodity"].str.len() > 0]
        df = df[~df["commodity"].str.startswith("Unnamed")]
        return df

    except Exception as e:
        print(f"  Excel parse error: {e}")
        return None


def fetch_wb_api_fallback(now):
    """Fallback: fetch commodity prices via World Bank Data API."""
    print("\n  Falling back to World Bank Data API for commodity prices...")
    frames = []
    for indicator_id, (name, unit) in WB_COMMODITY_INDICATORS.items():
        url = f"{WB_API_BASE}/country/WLD/indicator/{indicator_id}"
        params = {"format": "json", "mrv": 200, "frequency": "M", "per_page": 500}
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code != 200:
                continue
            payload = r.json()
            if len(payload) < 2 or not payload[1]:
                continue
            rows = [
                {
                    "date":      entry.get("date"),
                    "commodity": name,
                    "value":     entry.get("value"),
                    "unit":      unit,
                    "series_id": indicator_id,
                }
                for entry in payload[1]
                if entry.get("value") is not None
            ]
            if rows:
                frames.append(pd.DataFrame(rows))
            time.sleep(REQUEST_INTERVAL)
        except requests.RequestException:
            pass

    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "value"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def main():
    parser = argparse.ArgumentParser(description="World Bank Pink Sheet commodity prices")
    parser.add_argument("--backfill", action="store_true",
                        help="Same as default — full history is always included")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"

    print(f"World Bank Pink Sheet Pipeline  mode={mode}\n")
    os.makedirs(BASE_DIR, exist_ok=True)

    # Try Pink Sheet Excel first
    print("[wb_pink_sheet] Attempting Excel download...")
    content = download_pink_sheet()
    df = None

    if content:
        df = parse_pink_sheet(content)
        if df is not None and not df.empty:
            df["source"] = "World Bank Pink Sheet Excel"
            df["fetched_at"] = now.isoformat()
            print(f"  Parsed {len(df):,} rows, "
                  f"{df['commodity'].nunique()} commodities, "
                  f"dates {df['date'].min().date()} to {df['date'].max().date()}")

    # Fall back to WB API
    if df is None or df.empty:
        df = fetch_wb_api_fallback(now)
        if df is not None and not df.empty:
            df["source"] = "World Bank Data API"
            df["fetched_at"] = now.isoformat()

    if df is None or df.empty:
        print("\nNo data fetched from either source.")
        print("Update PINK_SHEET_URLS in worldbank_pink_sheet.py with the current Excel URL.")
        print("Find it at: https://www.worldbank.org/en/research/commodity-markets")
        return

    path = write_partitioned(
        df, BASE_DIR,
        f"wb_commodities_{mode}_{today_str}.parquet",
    )
    print(f"\n-> {path}  ({len(df):,} rows)")
    print("\n--- WORLD BANK PINK SHEET PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
