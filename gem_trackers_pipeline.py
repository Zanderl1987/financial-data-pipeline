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

Output (one table per tracker):
  storage/raw/gem/gem_coal_summary_{mode}_{YYYYMMDD}.parquet       (coal plants)
  storage/raw/gem/gem_coal_mine_summary_{mode}_{YYYYMMDD}.parquet  (coal mines)
  storage/raw/gem/gem_steel_summary_{mode}_{YYYYMMDD}.parquet      (iron & steel)
  storage/raw/gem/gem_cement_summary_{mode}_{YYYYMMDD}.parquet     (cement/concrete)
  storage/raw/gem/gem_oilgas_summary_{mode}_{YYYYMMDD}.parquet     (oil & gas extraction)
  storage/raw/gem/gem_lng_summary_{mode}_{YYYYMMDD}.parquet        (LNG + pipelines)
"""

import argparse
import csv
import datetime
import html
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

GEM_PROJECT_URL = "https://globalenergymonitor.org/projects/{slug}/download-data/"
GVIZ_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"

# One entry per tracker download page; each lands in its own table. The
# oil&gas and LNG trackers link several summary titles to tabs of a single
# workbook, so discovered links carry a ?gid=<tab> that must be forwarded to
# the gviz export or every title would parse the same first tab.
TRACKER_PAGES: list[dict] = [
    {"source": "gem_gcpt", "slug": "global-coal-plant-tracker",
     "table": "gem_coal_summary"},
    {"source": "gem_gcmt", "slug": "global-coal-mine-tracker",
     "table": "gem_coal_mine_summary"},
    {"source": "gem_gspt", "slug": "global-steel-plant-tracker",
     "table": "gem_steel_summary"},
    {"source": "gem_gcct", "slug": "global-cement-and-concrete-tracker",
     "table": "gem_cement_summary"},
    {"source": "gem_goget", "slug": "global-oil-and-gas-extraction-tracker",
     "table": "gem_oilgas_summary"},
    {"source": "gem_ggit", "slug": "global-gas-infrastructure-tracker",
     "table": "gem_lng_summary"},
]

# Known-good IDs verified 2026-08-25, used only when a download page cannot
# be fetched (multi-tab workbooks fall back to their first tab).
FALLBACK_SHEETS: dict[str, dict[str, tuple[str, int | None]]] = {
    "gem_gcpt": {
        "Newly Operating Coal Plants by Year (MW)": ("1j35F0WrRJ9dbIJhtRkm8fvPw0Vsf-JV6G95u7gT-DDw", None),
        "Retired Coal Plants (MW)":                 ("1t3gO35bzcVI8ekq9318jBUq6nd7UADcut4gY3vjHZMM", None),
        "Planned Coal Plant Retirements (MW)":      ("1E82_2I7n4__oFzDTWVuZwPstfr1tk4jn1kFw4E_gf5w", None),
        "Captive Coal Plants":                      ("1xnFBS4W6MRF0qmTnn3SyacvTaYlkUKIXTJdGhX0lZpo", None),
        "Coal Plants by Combustion Technology":     ("1d0NyUPGzXMqxR7OczQXcaHzen381AbMjQ3YEDmCAgj0", None),
        "Global Ownership of Coal Plants (MW)":     ("1c4YVil_aLWLIApVKoePLJKiNvb4XO2qhrkupGPfT_QE", None),
    },
    "gem_gcmt": {
        "Coal Production by Country/Area":           ("16LpUIA4PpV-2a7qOHpOy9-BBSxgJ9KVOrin2ggp6B1U", None),
        "Coal Production by Region":                 ("1cfDNFMc_mLyS6nXRvWF47OReucFTUfgSppe-4pzMXmA", None),
        "Coal Production by Mine Type":              ("1PaQnSRjp_U8109i1HrEqzzgNewbdMvKpjK_dPdXqETA", None),
        "Coal Production by Coal Grade":             ("16YjWPehCEaoS4-Bg4Q45aDZvrurnsiNnzVfIp-R3GpM", None),
        "Coal Workforce Size by Country/Area":       ("11bPLEcIg6uFcDp1ekLhIe5w4_GQkj2ISqd0i870L9J4", None),
        "Coal Mine Methane Emissions by Region":     ("1QGGp6rfw7W8phlKc8RCFbudPOAcB-Tz3RSsecs8GyaQ", None),
    },
    "gem_gspt": {
        "Count of Iron & Steel Plants by Development Status in Each Country/Area": ("1zjO8jgHuGXRiaL16jjSLQdIY6S2-6-DW6mJkmL0Oh2M", 982556392),
        "Steel Capacity (TTPA) by Development Status in Each Country/Area":        ("10aR9TJC00JKeDrF7kZzD261ex86vq8j72UdcVLkc4nU", None),
        "Operating Steel Capacity (TTPA) by Production Method in Each Country/Area": ("1mOWPPmjCQtoAWUCY0pAChgWskobG0odjdNUHV041_a8", None),
        "Operating Steel Capacity (TTPA) by Production Method in Each Region":     ("1Z8onPtIemlw3H0hIAp8y1btG6Qm5cU9TpcWiMrxuc98", None),
    },
    "gem_gcct": {
        "Cement and Clinker Capacity (MTPA) by Status in Each Country/Area": ("1xMozMm0OElQBVdKtkqDQsL5oi6vIin7S_1f0n7NJvfE", None),
        "Operating Cement and Clinker Capacity (MTPA) in Each Country/Area": ("1nWBp_7eGuUO8S1Xs1tkyJlxSScVGyDImMdrYFvNRd7A", None),
        "Count of Operating Cement Plants in Each Country/Area":             ("1WXRhfTZ40QpiKIxEKP0btssOAETfU1udl3X79o2A0C8", None),
    },
    "gem_goget": {
        "Extraction Sites by Country/Area": ("1JHt24Rmm6e0DyeTSqvqH1i9nJ876iYrq6X1InCAHcf0", None),
        "Yearly FIDs Approved by Region":   ("1JHt24Rmm6e0DyeTSqvqH1i9nJ876iYrq6X1InCAHcf0", None),
    },
    "gem_ggit": {
        "LNG Export Capacity by Region": ("1NbEpGt2K5nY0XTSB_vlOyw9Ug8ZmvvOaRPuO9TgISIw", None),
        "LNG Import Capacity by Region": ("1NbEpGt2K5nY0XTSB_vlOyw9Ug8ZmvvOaRPuO9TgISIw", None),
        "Pipelines by Region":           ("1NbEpGt2K5nY0XTSB_vlOyw9Ug8ZmvvOaRPuO9TgISIw", None),
    },
}

_SHEET_LINK_RE = re.compile(
    r'<a[^>]+href="(https://docs\.google\.com/spreadsheets/d/[^"#?/]+)'
    r'(?:/edit\?(?:[^"#]*&)?gid=(\d+))?[^"]*"[^>]*>(.*?)</a>',
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


def discover_sheets(page_url: str) -> dict[str, tuple[str, int | None]]:
    """Parse a tracker download-data page into {link text -> (sheet ID, gid)}."""
    content = _get(page_url, timeout=90)
    if not content:
        return {}
    html_text = content.decode("utf-8", errors="replace")
    sheets: dict[str, tuple[str, int | None]] = {}
    for m in _SHEET_LINK_RE.finditer(html_text):
        sheet_id = m.group(1).rstrip("/").split("/")[-1]
        gid = int(m.group(2)) if m.group(2) else None
        title = re.sub(r"<[^>]+>", "", m.group(3))
        title = re.sub(r"\s*View\b[\s\uf061a]*$", "", title)
        title = html.unescape(re.sub(r"\s+", " ", title)).strip()
        if title and sheet_id:
            sheets[title] = (sheet_id, gid)
    return sheets


def fetch_sheet(sheet_id: str, gid: int | None = None) -> str | None:
    url = GVIZ_URL.format(sheet_id=sheet_id)
    if gid is not None:
        url += f"&gid={gid}"
    content = _get(url)
    if content is None:
        return None
    return content.decode("utf-8", errors="replace")


_STOPWORDS = {"by", "the", "in", "of", "and", "each", "a", "an", "to", "for"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS}


def _tab_gids(sheet_id: str) -> list[int]:
    """Enumerate tab gids of a public workbook via its htmlview page."""
    content = _get(f"https://docs.google.com/spreadsheets/d/{sheet_id}/htmlview")
    if not content:
        return []
    gids = {int(m) for m in re.findall(r"gid=([0-9]+)", content.decode("utf-8", errors="replace"))}
    return sorted(gids)


def resolve_multitab_titles(sheets: dict[str, tuple[str, int | None]]) -> dict[str, tuple[str, int | None]]:
    """Several tracker summary titles can link to tabs of one workbook without
    per-tab gids in their hrefs; left alone they would all parse tab 1 under
    different labels. Enumerate the workbook's tabs and match each title to
    the tab whose banner shares the most title tokens."""
    by_sheet: dict[str, list[str]] = {}
    for title, (sid, gid) in sheets.items():
        if gid is None:
            by_sheet.setdefault(sid, []).append(title)
    resolved = dict(sheets)
    for sid, titles in by_sheet.items():
        if len(titles) < 2:
            continue
        gids = _tab_gids(sid)
        if not gids:
            for extra in titles[1:]:
                del resolved[extra]
            continue
        banners: dict[int, str] = {}
        for gid in gids:
            txt = fetch_sheet(sid, gid)
            time.sleep(REQUEST_INTERVAL)
            if txt:
                banners[gid] = next(csv.reader(io.StringIO(txt)))[0]
        matched_any = False
        for title in titles:
            want = _tokens(title)
            best_gid, best_score = None, 1
            for gid, banner in banners.items():
                score = len(want & _tokens(banner))
                if score > best_score:
                    best_gid, best_score = gid, score
            if best_gid is not None:
                resolved[title] = (sid, best_gid)
                matched_any = True
        if matched_any:
            print(f"    resolved {len(titles)} shared-workbook titles across "
                  f"{len(banners)} tabs")
    return resolved


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
    sheet_cache: dict[tuple[str, int | None], str | None] = {}

    for page_cfg in TRACKER_PAGES:
        source = page_cfg["source"]
        page_url = GEM_PROJECT_URL.format(slug=page_cfg["slug"])
        table = page_cfg["table"]
        print(f"[{source}] discovering summary sheets...")
        sheets = discover_sheets(page_url)
        if sheets:
            print(f"  found {len(sheets)} sheet links on page")
            sheets = resolve_multitab_titles(sheets)
        else:
            print(f"  page fetch failed - using {len(FALLBACK_SHEETS[source])} fallback IDs")
            sheets = FALLBACK_SHEETS[source]

        total = len(sheets)
        for i, (indicator, (sheet_id, gid)) in enumerate(sorted(sheets.items()), 1):
            print(f"  [{i}/{total}] {indicator}")
            cache_key = (sheet_id, gid)
            if cache_key not in sheet_cache:
                sheet_cache[cache_key] = fetch_sheet(sheet_id, gid)
                time.sleep(REQUEST_INTERVAL)
            text = sheet_cache[cache_key]
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

    if not frames:
        print("\nNo summary data fetched.")
        return

    all_rows = pd.concat(frames, ignore_index=True).drop_duplicates()

    written: list[str] = []
    for table in sorted({c["table"] for c in TRACKER_PAGES}):
        sub = all_rows[all_rows["source"].isin(
            c["source"] for c in TRACKER_PAGES if c["table"] == table)]
        if sub.empty:
            continue
        sub = sub.sort_values(["indicator", "country_or_region", "obs_year"])
        path = write_partitioned(
            sub, BASE_DIR,
            f"{table}_{mode}_{today_str}.parquet",
        )
        written.append(table)
        print(f"\n-> {path}")
        print(f"   {len(sub):,} rows | {sub['indicator'].nunique()} sheets "
              f"| release(s): {sorted(sub['release_label'].unique())}")

    print("\n--- GEM TRACKERS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="GEM tracker summary tables via public Google Sheets exports"
    )
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch all configured trackers (sheets are biannual snapshots)")
    args = parser.parse_args()
    main(backfill=args.backfill)
