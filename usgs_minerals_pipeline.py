#!/usr/bin/env python3
"""
USGS National Minerals Information Center (NMIC) Pipeline.

Fetches two types of files from USGS commodity pages:
  MIS (Monthly Industrial Survey) — monthly US import/export volumes and values
      Available for: cobalt, manganese
      Source: https://www.usgs.gov/centers/national-minerals-information-center/{name}-statistics-and-information
  MYB (Minerals Yearbook) — annual production and trade statistics
      Available for: lithium, graphite, nickel, rare earths, silicon
      Same page structure, but links to annual workbooks

Data is extracted from numbered table sheets (T1–T5 in MIS files):
  T2 — US imports for consumption, by country/material type (monthly)
  T4 — US exports (monthly)

Implicit average price can be derived from: value ($000) / quantity (MT) × 1000 = $/MT

No API key required. All files are freely available from USGS S3/pubs.usgs.gov.

CLI:
  python usgs_minerals_pipeline.py             # last 6 months of MIS + latest MYB
  python usgs_minerals_pipeline.py --backfill  # all available MIS files + all MYB

Output:
  storage/raw/usgs_minerals/usgs_minerals_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import io
import os
import re
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR = os.path.join("storage", "raw", "usgs_minerals")
MAX_RETRIES = 3
BACKOFF_SECONDS = 30
REQUEST_INTERVAL = 1.5
HEADERS = {"User-Agent": "Mozilla/5.0 financial-data-pipeline/1.0"}

# Month names as they appear in USGS tables
MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

COMMODITIES: dict[str, dict] = {
    "cobalt": {
        "page_url": "https://www.usgs.gov/centers/national-minerals-information-center/cobalt-statistics-and-information",
        "mis_abbrev": "cobal",
        "category":   "battery_materials",
        "notes":      "NMC/NCA cathode precursor; LME-traded",
    },
    "manganese": {
        "page_url": "https://www.usgs.gov/centers/national-minerals-information-center/manganese-statistics-and-information",
        "mis_abbrev": "manga",
        "category":   "metals",
        "notes":      "LFP/NMC batteries, steel alloys; almost entirely imported",
    },
    "lithium": {
        "page_url": "https://www.usgs.gov/centers/national-minerals-information-center/lithium-statistics-and-information",
        "mis_abbrev": None,  # no MIS; use MYB annual reports instead
        "category":   "battery_materials",
        "notes":      "All Li-ion battery electrolytes and cathodes",
    },
    "graphite": {
        "page_url": "https://www.usgs.gov/centers/national-minerals-information-center/graphite-statistics-and-information",
        "mis_abbrev": None,
        "category":   "battery_materials",
        "notes":      "Battery anode material; >90% of supply from China",
    },
    "nickel": {
        "page_url": "https://www.usgs.gov/centers/national-minerals-information-center/nickel-statistics-and-information",
        "mis_abbrev": None,
        "category":   "metals",
        "notes":      "NMC cathodes, stainless steel",
    },
    "rare_earths": {
        "page_url": "https://www.usgs.gov/centers/national-minerals-information-center/rare-earths-statistics-and-information",
        "mis_abbrev": None,
        "category":   "battery_materials",
        "notes":      "EV motor magnets (Nd, Pr, Dy); China dominant supplier",
    },
    "iron_steel": {
        "page_url": "https://www.usgs.gov/centers/national-minerals-information-center/iron-and-steel-statistics-and-information",
        "mis_abbrev": None,
        "category":   "industrial",
        "notes":      "Iron ore + steel; 95% of global metal production by tonnage",
        "myb_pattern": r"myb1-\d{4}-(iron-steel|feste)",
    },
    "helium": {
        "page_url": "https://www.usgs.gov/centers/national-minerals-information-center/helium-statistics-and-information",
        "mis_abbrev": None,
        "category":   "industrial",
        "notes":      "US production, sales/shipments, and price; MRI/semiconductor/aerospace/welding uses",
        "myb_pattern": r"myb1-\d{4}-helium",
    },
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_bytes(url: str) -> bytes | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=45, allow_redirects=True)
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


def _scrape_xlsx_urls(page_url: str, pattern: str | None = None) -> list[str]:
    """Fetch a USGS commodity page and return all xlsx href URLs (optionally filtered)."""
    content = _get_bytes(page_url)
    if not content:
        return []
    html = content.decode("utf-8", errors="replace")
    urls = [m.group(1) for m in re.finditer(r'href="(https?://[^"]+\.xlsx)"', html)]
    if pattern:
        urls = [u for u in urls if re.search(pattern, u, re.IGNORECASE)]
    return list(dict.fromkeys(urls))   # deduplicate preserving order


# ---------------------------------------------------------------------------
# MIS Excel parser  (cobalt/manganese monthly trade files)
# ---------------------------------------------------------------------------

def _parse_mis(content: bytes, commodity: str, category: str,
               source_url: str) -> pd.DataFrame:
    """
    Parse a USGS MIS workbook.  Extracts every data table sheet (T1–T5),
    tracking year context from rows whose first cell is a 4-digit year, and
    labelling month rows with their calendar year-month.  All numeric cells
    are kept in long format.
    """
    try:
        xl = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    except Exception as e:
        print(f"    Excel open error: {e}")
        return pd.DataFrame()

    records = []
    data_sheets = [s for s in xl.sheet_names if re.match(r"^T\d+$", s)]

    for sheet_name in data_sheets:
        try:
            raw = xl.parse(sheet_name, header=None)
        except Exception:
            continue
        if raw.empty:
            continue

        # Extract table title from row 0
        table_title = str(raw.iloc[0, 0]).strip() if pd.notna(raw.iloc[0, 0]) else sheet_name

        current_year: int | None = None
        for _, row in raw.iterrows():
            cell0 = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            if not cell0 or cell0.lower() in ("nan", "none"):
                continue

            # Year context row
            try:
                yr = int(float(cell0))
                if 1990 <= yr <= 2040:
                    current_year = yr
                    continue
            except (ValueError, TypeError):
                pass

            # Month row
            month_num = MONTH_MAP.get(cell0.lower().split("\n")[0].split()[0].lower())
            if month_num and current_year:
                period = f"{current_year}-{month_num:02d}"
                for col_idx in range(1, len(row)):
                    cell = row.iloc[col_idx]
                    try:
                        val_str = str(cell).replace(",", "").split()[0]   # strip footnotes
                        val = float(val_str)
                        records.append({
                            "commodity":   commodity,
                            "category":    category,
                            "file_type":   "MIS",
                            "sheet":       sheet_name,
                            "table_title": table_title,
                            "period":      period,
                            "period_type": "monthly",
                            "col_idx":     col_idx,
                            "value":       val,
                            "source_url":  source_url,
                        })
                    except (ValueError, TypeError, IndexError):
                        continue

    return pd.DataFrame(records) if records else pd.DataFrame()


# ---------------------------------------------------------------------------
# MYB / MCS Excel parser  (lithium, graphite, nickel — annual)
# ---------------------------------------------------------------------------

_YEAR_RANGE = set(range(1900, 2031))


def _parse_myb(content: bytes, commodity: str, category: str,
               source_url: str) -> pd.DataFrame:
    """
    Parse a USGS Minerals Yearbook or Mineral Commodity Summaries workbook.
    Scans each sheet for rows whose column headers are years, then extracts
    all labelled data rows beneath them.
    """
    try:
        xl = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    except Exception as e:
        print(f"    Excel open error: {e}")
        return pd.DataFrame()

    records = []
    for sheet_name in xl.sheet_names:
        if sheet_name.lower() in ("text", "removetextbutton", "cover", "toc"):
            continue
        try:
            raw = xl.parse(sheet_name, header=None)
        except Exception:
            continue
        if raw.empty or raw.shape[1] < 3:
            continue

        # Find header row with year columns
        year_row_idx: int | None = None
        year_cols: dict[int, int] = {}
        for row_idx in range(min(15, len(raw))):
            matches: dict[int, int] = {}
            for col_idx, cell in enumerate(raw.iloc[row_idx]):
                try:
                    yr = int(float(cell))
                    if yr in _YEAR_RANGE:
                        matches[col_idx] = yr
                except (TypeError, ValueError):
                    continue
            if len(matches) >= 3:
                year_row_idx = row_idx
                year_cols = matches
                break

        if not year_cols:
            continue

        for row_idx in range(year_row_idx + 1, len(raw)):
            row = raw.iloc[row_idx]
            label = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            if not label or label.lower() in ("nan", "none", ""):
                continue
            for col_idx, year in year_cols.items():
                if col_idx >= len(row):
                    continue
                try:
                    val = float(str(row.iloc[col_idx]).replace(",", "").split()[0])
                    records.append({
                        "commodity":   commodity,
                        "category":    category,
                        "file_type":   "MYB",
                        "sheet":       sheet_name,
                        "table_title": label,
                        "period":      str(year),
                        "period_type": "annual",
                        "col_idx":     0,
                        "value":       val,
                        "source_url":  source_url,
                    })
                except (ValueError, TypeError, IndexError):
                    continue

    return pd.DataFrame(records) if records else pd.DataFrame()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill: bool = False) -> None:
    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if backfill else "incremental"
    print(f"USGS Minerals Pipeline  mode={mode}\n")

    os.makedirs(BASE_DIR, exist_ok=True)
    frames: list[pd.DataFrame] = []
    failed: list[str] = []

    for commodity, info in COMMODITIES.items():
        print(f"[{commodity}]")
        page_url    = info["page_url"]
        mis_abbrev  = info["mis_abbrev"]
        category    = info["category"]
        commodity_frames: list[pd.DataFrame] = []

        # ── MIS branch (monthly data) ───────────────────────────────────────
        if mis_abbrev:
            mis_pattern = rf"mis-\d{{6}}-{mis_abbrev}\.xlsx"
            print(f"  Scraping commodity page for MIS files...")
            mis_urls = _scrape_xlsx_urls(page_url, pattern=mis_pattern)
            if not mis_urls:
                print(f"  No MIS files found on page")
            else:
                if not backfill:
                    mis_urls = mis_urls[:6]  # last ~6 months
                print(f"  Found {len(mis_urls)} MIS files — downloading {len(mis_urls)}")
                for url in mis_urls:
                    fname = url.split("/")[-1]
                    content = _get_bytes(url)
                    if content is None:
                        print(f"    {fname}: download failed")
                        continue
                    df = _parse_mis(content, commodity, category, url)
                    if not df.empty:
                        commodity_frames.append(df)
                        print(f"    {fname}: {len(df):,} records")
                    else:
                        print(f"    {fname}: no parseable data")
                    time.sleep(REQUEST_INTERVAL)

        # ── MYB branch (annual data) ────────────────────────────────────────
        else:
            myb_pattern = info.get("myb_pattern", r"myb1-\d{4}-")
            print(f"  Scraping commodity page for MYB files...")
            myb_urls = _scrape_xlsx_urls(page_url, pattern=myb_pattern)
            if not myb_urls:
                # Also try MCS (Mineral Commodity Summaries) pattern
                mcs_pattern = r"mcs\d{4}-"
                myb_urls = _scrape_xlsx_urls(page_url, pattern=mcs_pattern)
            if not myb_urls:
                print(f"  No MYB/MCS files found on page")
            else:
                if not backfill:
                    myb_urls = myb_urls[:2]  # latest 2 years
                print(f"  Found {len(myb_urls)} MYB files — downloading {len(myb_urls)}")
                for url in myb_urls:
                    fname = url.split("/")[-1]
                    content = _get_bytes(url)
                    if content is None:
                        print(f"    {fname}: download failed")
                        continue
                    df = _parse_myb(content, commodity, category, url)
                    if not df.empty:
                        commodity_frames.append(df)
                        print(f"    {fname}: {len(df):,} records")
                    else:
                        print(f"    {fname}: no parseable data")
                    time.sleep(REQUEST_INTERVAL)

        if commodity_frames:
            frames.extend(commodity_frames)
        else:
            print(f"  [!] No data fetched for {commodity}")
            failed.append(commodity)

        print()

    if not frames:
        print("No data fetched from any commodity.")
        return

    combined = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["commodity", "sheet", "table_title", "period", "col_idx", "value"])
        .sort_values(["commodity", "period", "sheet"])
        .reset_index(drop=True)
    )
    combined["source"]     = "USGS NMIC"
    combined["fetched_at"] = now.isoformat()

    path = write_partitioned(
        combined, BASE_DIR,
        f"usgs_minerals_{mode}_{today_str}.parquet",
    )
    print(f"-> {path}")
    print(f"   {len(combined):,} rows | {combined['commodity'].nunique()} commodities "
          f"| {combined['file_type'].value_counts().to_dict()}")
    if failed:
        print(f"   Failed: {', '.join(failed)}")

    print("\n--- USGS MINERALS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="USGS NMIC critical mineral statistics (MIS monthly + MYB annual)"
    )
    parser.add_argument("--backfill", action="store_true",
                        help="Download all available MIS files + all MYB files (vs. latest only)")
    args = parser.parse_args()
    main(backfill=args.backfill)
