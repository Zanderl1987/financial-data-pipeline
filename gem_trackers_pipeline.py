#!/usr/bin/env python3
"""
Global Energy Monitor (GEM) Tracker Summary Tables Pipeline.

GEM publishes each tracker's summary tables as public Google Sheets linked
from the tracker's download-data page. Sheets are fetched via the keyless
gviz CSV export endpoint:

    https://docs.google.com/spreadsheets/d/<ID>/gviz/tq?tqx=out:csv

License: GEM data is free to use with attribution — cite
"Global Energy Monitor, <Tracker> <release>" in any derived work.

The download-data page is parsed LIVE at runtime to extract (link text ->
sheet ID) pairs, so the pipeline survives GEM's biannual ID rotation; a small
hardcoded dict of known-good IDs (verified 2026-08-24) is used as a fallback
when the page fetch fails. Adding another tracker later is just another entry
in TRACKER_PAGES.

Sheets carry a title/metadata banner above the tabular data; the parser scans
the first rows for the one with the most populated cells and treats its
labels as columns (the banner cell ends with the row-dimension name, e.g.
"Country/Area", "Combustion technology"). Year-like column labels populate
obs_year; stage labels ("Announced", "Total", "H1 2026") are kept verbatim in
column_label with a null obs_year.

CLI:
  python gem_trackers_pipeline.py             # all trackers, current sheets
  python gem_trackers_pipeline.py --backfill  # same fetch (sheets are snapshots)

Output:
  storage/raw/gem/gem_coal_summary_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import csv
import datetime
import io
import os
import re
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR = os.path.join("storage", "raw", "gem")
MAX_RETRIES = 3
BACKOFF_SECONDS = 30
REQUEST_INTERVAL = 2.0
# The download-data page returns empty content for default/script UAs.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
}

GCPT_DOWNLOAD_URL = ("https://globalenergymonitor.org/projects/"
                     "global-coal-plant-tracker/download-data/")
GVIZ_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"

TRACKER_PAGES: list[dict] = [
    {"source": "gem_gcpt", "page_url": GCPT_DOWNLOAD_URL,
     "table": "gem_coal_summary"},
]

FALLBACK_SHEETS: dict[str, str] = {
    "Newly Operating Coal Plants by Year (MW)":   "1j35F0WrRJ9dbIJhtRkm8fvPw0Vsf-JV6G95u7gT-DDw",
    "Retired Coal Plants (MW)":                   "1t3gO35bzcVI8ekq9318jBUq6nd7UADcut4gY3vjHZMM",
    "Planned Coal Plant Retirements (MW)":        "1E82_2I7n4__oFzDTWVuZwPstfr1tk4jn1kFw4E_gf5w",
    "Captive Coal Plants":                        "1xnFBS4W6MRF0qmTnn3SyacvTaYlkUKIXTJdGhX0lZpo",
    "Coal Plants by Combustion Technology":       "1d0NyUPGzXMqxR7OczQXcaHzen381AbMjQ3YEDmCAgj0",
    "Global Ownership of Coal Plants (MW)":       "1c4YVil_aLWLIApVKoePLJKiNvb4XO2qhrkupGPfT_QE",
}

_SHEET_LINK_RE = re.compile(
    r'<a[^>]+href="(https://docs\.google\.com/spreadsheets/d/[^"#?/]+)[^"]*"[^>]*>(.*?)</a>',
    re.S | re.I,
)
_RELEASE_RE = re.compile(r"updated\s+([A-Z][a-z]+ \d{4})")
_UNIT_RE = re.compile(r"Unit of measurement:\s*(.+?\([^)]*\))")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 60) -> bytes | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r.content
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


def discover_sheets(page_url: str) -> dict[str, str]:
    """Parse a tracker download-data page into {link text -> sheet ID}."""
    content = _get(page_url, timeout=90)
    if not content:
        return {}
    html = content.decode("utf-8", errors="replace")
    sheets: dict[str, str] = {}
    for m in _SHEET_LINK_RE.finditer(html):
        sheet_id = m.group(1).rstrip("/").split("/")[-1]
        text = re.sub(r"<[^>]+>", "", m.group(2))
        text = re.sub(r"\s*View\s+[\uf061a]?\s*$", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text and sheet_id:
            sheets[text] = sheet_id
    return sheets


def fetch_sheet(sheet_id: str) -> str | None:
    content = _get(GVIZ_URL.format(sheet_id=sheet_id))
    if content is None:
        return None
    return content.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Sheet parsing
# ---------------------------------------------------------------------------

def _find_header(rows: list[list[str]]) -> int:
    """Index of the most-populated row among the leading banner rows."""
    best_idx, best_count = 0, 0
    for idx, row in enumerate(rows[:6]):
        count = sum(1 for c in row if c.strip())
        if count > best_count:
            best_idx, best_count = idx, count
    if best_count < 3:
        return -1
    return best_idx


def _dimension_name(label_cell: str) -> str:
    """The banner blob ends with the row-dimension label after the last
    closing paren of the 'Unit of measurement' sentence."""
    tail = re.split(r"\)\s*", label_cell)[-1].strip()
    return tail or "Category"


def parse_sheet(text: str, indicator: str, sheet_id: str) -> pd.DataFrame:
    rows = list(csv.reader(io.StringIO(text)))
    header_idx = _find_header(rows)
    if header_idx < 0:
        print(f"    no header row found - skipping")
        return pd.DataFrame()

    header = [c.strip() for c in rows[header_idx]]
    banner = " ".join(c for c in header if c)
    release_m = _RELEASE_RE.search(banner)
    unit_m = _UNIT_RE.search(banner)

    records = []
    for row in rows[header_idx + 1:]:
        if not row or not row[0].strip():
            continue
        region = row[0].strip()
        for col_idx in range(1, min(len(row), len(header))):
            col_label = header[col_idx]
            raw = row[col_idx].replace(",", "").strip()
            if not col_label or not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            obs_year = None
            year_m = re.fullmatch(r"(20\d{2}|19\d{2})", col_label)
            if year_m:
                obs_year = int(year_m.group(1))
            records.append({
                "tracker_sheet":     sheet_id,
                "indicator":         indicator,
                "country_or_region": region,
                "column_label":      col_label,
                "obs_year":          obs_year,
                "unit":              unit_m.group(1).strip() if unit_m else "",
                "value":             value,
                "release_label":     release_m.group(1) if release_m else "",
            })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill: bool = False) -> None:
    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if backfill else "incremental"
    print(f"GEM Trackers Pipeline  mode={mode}\n")

    os.makedirs(BASE_DIR, exist_ok=True)
    frames: list[pd.DataFrame] = []

    for page_cfg in TRACKER_PAGES:
        source = page_cfg["source"]
        print(f"[{source}] discovering summary sheets...")
        sheets = discover_sheets(page_cfg["page_url"])
        if sheets:
            print(f"  found {len(sheets)} sheets on page")
        else:
            print(f"  page fetch failed - using {len(FALLBACK_SHEETS)} fallback IDs")
            sheets = FALLBACK_SHEETS

        total = len(sheets)
        for i, (indicator, sheet_id) in enumerate(sorted(sheets.items()), 1):
            print(f"  [{i}/{total}] {indicator}")
            text = fetch_sheet(sheet_id)
            if text is None:
                print("    fetch failed")
                continue
            df = parse_sheet(text, indicator, sheet_id)
            if df.empty:
                print("    no parseable data")
                continue
            df["source"]     = source
            df["fetched_at"] = now.isoformat()
            frames.append(df)
            print(f"    {len(df):,} records")
            time.sleep(REQUEST_INTERVAL)

    if not frames:
        print("\nNo summary data fetched.")
        return

    combined = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates()
        .sort_values(["indicator", "country_or_region", "obs_year"])
        .reset_index(drop=True)
    )

    path = write_partitioned(
        combined, BASE_DIR,
        f"gem_coal_summary_{mode}_{today_str}.parquet",
    )
    print(f"\n-> {path}")
    print(f"   {len(combined):,} rows | {combined['indicator'].nunique()} sheets "
          f"| release(s): {sorted(combined['release_label'].unique())}")

    print("\n--- GEM TRACKERS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="GEM tracker summary tables via public Google Sheets exports"
    )
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch all configured trackers (sheets are biannual snapshots)")
    args = parser.parse_args()
    main(backfill=args.backfill)
