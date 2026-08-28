#!/usr/bin/env python3
"""
USGS Data Series 140 — Helium Historical Statistics Pipeline.

Downloads the static DS-140 Excel workbook (annual US + world helium
statistics back to 1935, metric tons of helium content; unit values in
dollars per metric ton) and reshapes it to a long tidy table:

    https://d9-wret.s3.us-west-2.amazonaws.com/assets/palladium/production/
        s3fs-public/media/files/ds140-helium-2022.xlsx

The file is updated only occasionally (last revision September 2023), so the
pipeline skips re-download AND re-parse when the SHA-256 of the workbook is
unchanged from the previous run.

No API key required.

CLI:
  python usgs_ds140_pipeline.py             # download if changed, parse
  python usgs_ds140_pipeline.py --force     # re-parse even if hash unchanged

Output:
  storage/raw/usgs_ds140/usgs_ds140_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import hashlib
import io
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR = os.path.join("storage", "raw", "usgs_ds140")
HASH_FILE = os.path.join(BASE_DIR, ".ds140_sha256.txt")
MAX_RETRIES = 3
BACKOFF_SECONDS = 30
HEADERS = {"User-Agent": "Mozilla/5.0 financial-data-pipeline/1.0"}

DS140_URL = ("https://d9-wret.s3.us-west-2.amazonaws.com/assets/palladium/"
             "production/s3fs-public/media/files/ds140-helium-2022.xlsx")

# Workbook columns -> metric slugs. Unit-value columns carry their own units.
METRIC_MAP: dict[str, tuple[str, str]] = {
    "Production":         ("production",           "metric tons helium"),
    "Shipments":          ("sales",                "metric tons helium"),
    "Imports":            ("imports",              "metric tons helium"),
    "Exports":            ("exports",              "metric tons helium"),
    "Stocks":             ("stocks",               "metric tons helium"),
    "Apparent consumption": ("apparent_consumption", "metric tons helium"),
    "Unit value ($/t)":   ("unit_value_nominal",   "dollars per metric ton"),
    "Unit value (98$/t)": ("unit_value_real_1998", "dollars per metric ton"),
    "World production":   ("world_production",     "metric tons helium"),
}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get_bytes(url: str) -> bytes | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=120)
            if r.status_code == 200 and len(r.content) > 1_000:
                return r.content
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(BACKOFF_SECONDS * attempt)
            else:
                print(f"    HTTP {r.status_code}")
                return None
        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_SECONDS)
            else:
                print(f"    Request error: {e}")
    return None


# ---------------------------------------------------------------------------
# Workbook parser
# ---------------------------------------------------------------------------

def _parse_ds140(content: bytes) -> pd.DataFrame:
    """
    Parse the single-sheet DS-140 workbook. Header row is the one whose first
    cell is 'Year' (title/footnote rows surround it); data runs until the
    first row whose first cell is not a plausible year.
    """
    xl = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    raw = xl.parse(xl.sheet_names[0], header=None)

    header_idx = None
    for idx in range(min(15, len(raw))):
        if str(raw.iloc[idx, 0]).strip() == "Year":
            header_idx = idx
            break
    if header_idx is None:
        print("    Could not locate 'Year' header row")
        return pd.DataFrame()

    headers = [str(c).strip() if pd.notna(c) else ""
               for c in raw.iloc[header_idx]]
    records = []
    for row_idx in range(header_idx + 1, len(raw)):
        cell0 = raw.iloc[row_idx, 0]
        try:
            obs_year = int(float(cell0))
        except (TypeError, ValueError):
            break   # footnote rows begin after the last data year
        if not 1900 <= obs_year <= 2100:
            break
        for col_idx, header in enumerate(headers[1:], start=1):
            mapped = METRIC_MAP.get(header)
            if not mapped or col_idx >= len(headers):
                continue
            cell = raw.iloc[row_idx, col_idx]
            if cell is None or (isinstance(cell, float) and pd.isna(cell)):
                continue
            try:
                val = float(str(cell).replace(",", "").split()[0])
            except (ValueError, TypeError, IndexError):
                continue
            records.append({
                "obs_year": obs_year,
                "metric":   mapped[0],
                "value":    val,
                "unit":     mapped[1],
            })

    return pd.DataFrame(records) if records else pd.DataFrame()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(force: bool = False) -> None:
    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    print("USGS DS-140 Helium Historical Statistics Pipeline\n")

    os.makedirs(BASE_DIR, exist_ok=True)

    content = _get_bytes(DS140_URL)
    if content is None:
        print("Download failed.")
        return

    sha = hashlib.sha256(content).hexdigest()
    if not force and os.path.exists(HASH_FILE):
        with open(HASH_FILE, encoding="utf-8") as f:
            prev = f.read().strip()
        if prev == sha:
            print(f"Workbook unchanged since last run (sha256 {sha[:12]}...) - skipping.")
            return

    df = _parse_ds140(content)
    if df.empty:
        print("No parseable data in workbook.")
        return

    df["source"]     = "USGS Data Series 140"
    df["fetched_at"] = now.isoformat()
    df = (
        df.drop_duplicates(subset=["obs_year", "metric"])
        .sort_values(["obs_year", "metric"])
        .reset_index(drop=True)
    )

    path = write_partitioned(df, BASE_DIR,
                             f"usgs_ds140_{today_str}.parquet")
    with open(HASH_FILE, "w", encoding="utf-8") as f:
        f.write(sha)

    print(f"-> {path}")
    print(f"   {len(df):,} rows | years {df['obs_year'].min()}-{df['obs_year'].max()} "
          f"| metrics: {df['metric'].nunique()}")

    print("\n--- USGS DS-140 HELIUM PIPELINE COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="USGS Data Series 140 helium historical statistics (static annual workbook)"
    )
    parser.add_argument("--force", action="store_true",
                        help="Re-download and re-parse even if the workbook hash is unchanged")
    args = parser.parse_args()
    main(force=args.force)
