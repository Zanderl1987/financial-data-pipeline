#!/usr/bin/env python3
"""
USGS Mineral Commodity Summaries (MCS) Helium + Rare Gases Pipeline.

Fetches the annual HELIUM data releases published on USGS ScienceBase alongside
the Mineral Commodity Summaries report (public domain / CC0, no API key):

  Release 2022-2025: per-commodity items titled
      "Mineral Commodity Summaries YYYY - HELIUM Data Release"
      containing mcsYYYY-heliu_salient.csv (US salient statistics, wide) and,
      through 2024, mcsYYYY-heliu_world.csv (world production/reserves).
  Release 2026+:     the report switched to a combined per-commodity file
      ("Mineral Commodity Summaries YYYY Data Release - Commodity Salient
      U.S. and World Statistics"); the helium chapter is
      "HELIUM AND RARE GASES" (helium, neon, argon, krypton, xenon).

All three layouts are reshaped into one tidy table keyed on
(obs_year, series, commodity, country). Later releases restate earlier
years, so curated dedup keeps the newest fetch per key.

ScienceBase years verified live 2026-08-24: 2022, 2023, 2024, 2025 have
per-commodity HELIUM items; 2026 exists only as the combined release.
2020/2021 were never published as separate data releases.

CLI:
  python usgs_helium_mcs_pipeline.py             # latest available release
  python usgs_helium_mcs_pipeline.py --backfill  # every release 2020-present

Output:
  storage/raw/usgs_mcs_helium/usgs_mcs_helium_{mode}_{YYYYMMDD}.parquet
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

BASE_DIR = os.path.join("storage", "raw", "usgs_mcs_helium")
MAX_RETRIES = 3
BACKOFF_SECONDS = 30
REQUEST_INTERVAL = 1.5
HEADERS = {"User-Agent": "Mozilla/5.0 financial-data-pipeline/1.0"}

SB_SEARCH_URL = "https://www.sciencebase.gov/catalog/items"
SB_ITEM_URL = "https://www.sciencebase.gov/catalog/item/{item_id}"
BACKFILL_START = 2020

HELIUM_CHAPTER = "HELIUM AND RARE GASES"

# salient.csv wide columns -> (series, unit). Matched case-insensitively by
# prefix because the Grade-A sales column name varies across releases
# (Grade-A-Salesmcm vs Grade-A-Salescm).
SALIENT_SERIES: list[tuple[str, str, str]] = [
    ("extracted_mcm",       "extracted",              "million cubic meters"),
    ("withdrawn_mcm",       "withdrawn_from_storage", "million cubic meters"),
    ("grade-a-sales",       "grade_a_sales",          "million cubic meters"),
    ("imports_mcm",         "imports",                "million cubic meters"),
    ("exports_mcm",         "exports",                "million cubic meters"),
    ("consump_mcm",         "apparent_consumption",   "million cubic meters"),
    ("nir_pct",             "net_import_reliance",    "percent"),
]

# Combined-release Statistics values -> series slugs
STATISTICS_MAP = {
    "sold or used":        "sold_or_used",
    "import":              "imports",
    "export":              "exports",
    "consumption":         "apparent_consumption",
    "net import reliance": "net_import_reliance",
    "production":          "production",
    "reserves":            "reserves",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_bytes(url: str) -> bytes | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 0:
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


def _sb_search(query: str) -> list[dict]:
    """Search ScienceBase catalog items, returning raw item dicts."""
    content = _get_json(SB_SEARCH_URL, {
        "q": query, "format": "json", "fields": "title,files", "max": 10,
    })
    if content is None:
        return []
    return content.get("items", []) or []


def _get_json(url: str, params: dict) -> dict | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(BACKOFF_SECONDS * attempt)
                continue
            print(f"    HTTP {r.status_code} on {url}")
            return None
        except (requests.RequestException, ValueError) as e:
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_SECONDS)
            else:
                print(f"    Request error: {e}")
    return None


def _sb_files(item_id: str) -> list[dict]:
    content = _get_json(SB_ITEM_URL.format(item_id=item_id),
                        {"format": "json", "fields": "files,title"})
    if content is None:
        return []
    return content.get("files", []) or []


# ---------------------------------------------------------------------------
# Value / label normalisation
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    s = str(text).lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def _parse_value(raw) -> float:
    """Numeric coercion for MCS cells: commas stripped, >/< bounds kept as the
    number itself (USGS convention: '>95' means greater than 95), footnote
    codes (E, s, NA, --) become NaN."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return float("nan")
    s = str(raw).replace(",", "").strip()
    s = re.sub(r"^[<>]", "", s).strip()
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _country_slug(country: str) -> str:
    c = re.sub(r"\(rounded\)", "", str(country))
    return _slug(c)


# ---------------------------------------------------------------------------
# Parsers — one per known file layout
# ---------------------------------------------------------------------------

def _parse_salient(content: bytes, title: str) -> pd.DataFrame:
    """mcsYYYY-heliu_salient.csv — wide US salient statistics, one row per
    statistic year, one column per metric."""
    df = pd.read_csv(io.BytesIO(content), encoding="cp1252")
    records = []
    for _, row in df.iterrows():
        try:
            obs_year = int(row["Year"])
        except (TypeError, ValueError):
            continue
        commodity = _slug(row.get("Commodity", "helium"))
        for col, series, unit in SALIENT_SERIES:
            matched = [c for c in df.columns
                       if str(c).lower().startswith(col.lower())]
            if not matched:
                continue
            val = _parse_value(row[matched[0]])
            if pd.isna(val):
                continue
            records.append({
                "obs_year":   obs_year,
                "series":     series,
                "commodity":  commodity,
                "country":    "united_states",
                "value":      val,
                "unit":       unit,
                "source_release": title,
            })
    return pd.DataFrame(records)


def _parse_world(content: bytes, title: str) -> pd.DataFrame:
    """mcsYYYY-heliu_world.csv — mine production (and reserves from ~2023)
    by country, with the statistic year embedded in the column header."""
    df = pd.read_csv(io.BytesIO(content), encoding="cp1252")
    prod_years: dict[str, int] = {}
    reserve_cols: list[str] = []
    for col in df.columns:
        m = re.search(r"(?i)(prod|reserv).*?(\d{4})", str(col))
        if m:
            if m.group(1).lower().startswith("prod"):
                prod_years[col] = int(m.group(2))
        elif re.search(r"(?i)reserv", str(col)):
            reserve_cols.append(col)
    fallback_year = max(prod_years.values()) if prod_years else None

    records = []
    for _, row in df.iterrows():
        country = _country_slug(row.get("Country", ""))
        if not country:
            continue
        for col, obs_year in prod_years.items():
            val = _parse_value(row[col])
            if not pd.isna(val):
                records.append({
                    "obs_year":   obs_year,
                    "series":     f"world_production_{country}",
                    "commodity":  "helium",
                    "country":    country,
                    "value":      val,
                    "unit":       "million cubic meters",
                    "source_release": title,
                })
        for col in reserve_cols:
            val = _parse_value(row[col])
            if not pd.isna(val) and fallback_year:
                records.append({
                    "obs_year":   fallback_year,
                    "series":     f"reserves_{country}",
                    "commodity":  "helium",
                    "country":    country,
                    "value":      val,
                    "unit":       "million cubic meters",
                    "source_release": title,
                })
    return pd.DataFrame(records)


def _parse_combined(content: bytes, title: str) -> pd.DataFrame:
    """MCS2026+ combined Commodities_Data.csv — already tidy; keep only the
    HELIUM AND RARE GASES chapter."""
    df = pd.read_csv(io.BytesIO(content), encoding="cp1252")
    chapter_col = next(c for c in df.columns if "chapter" in str(c).lower())
    heli = df[df[chapter_col].astype(str).str.upper() == HELIUM_CHAPTER]
    records = []
    for _, row in heli.iterrows():
        obs_year = _parse_value(row.get("Year"))
        if pd.isna(obs_year):
            continue
        series = STATISTICS_MAP.get(str(row.get("Statistics", "")).lower())
        if not series:
            continue
        val = _parse_value(row.get("Value"))
        if pd.isna(val):
            continue
        records.append({
            "obs_year":   int(obs_year),
            "series":     series,
            "commodity":  _slug(row.get("Commodity", "helium")),
            "country":    _country_slug(row.get("Country", "")),
            "value":      val,
            "unit":       str(row.get("Unit", "")).strip(),
            "source_release": title,
        })
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Release discovery + download
# ---------------------------------------------------------------------------

def _fetch_release(release_year: int) -> tuple[list[pd.DataFrame], str]:
    """Download every CSV in the given release year's helium data and parse it.
    Returns (frames, description). Tries the dedicated per-commodity item
    first, then falls back to the combined salient-statistics release."""
    query = f'"Mineral Commodity Summaries {release_year} - HELIUM Data Release"'
    items = [i for i in _sb_search(query)
             if f"{release_year}" in str(i.get("title", ""))
             and "HELIUM" in str(i.get("title", "")).upper()]
    if items:
        frames = []
        title = items[0].get("title", "")
        for f in _sb_files(items[0].get("id", "")):
            name = str(f.get("name", ""))
            if not name.lower().endswith(".csv"):
                continue
            content = _get_bytes(f.get("url") or f.get("downloadUri"))
            if content is None:
                print(f"    {name}: download failed")
                continue
            if "_salient" in name:
                frames.append(_parse_salient(content, title))
            elif "_world" in name:
                frames.append(_parse_world(content, title))
            time.sleep(REQUEST_INTERVAL)
        return frames, title

    query = f'"Mineral Commodity Summaries {release_year} Data Release"'
    items = [i for i in _sb_search(query)
             if "Commodity Salient" in str(i.get("title", ""))]
    if items:
        title = items[0].get("title", "")
        for f in _sb_files(items[0].get("id", "")):
            name = str(f.get("name", ""))
            if name.lower().endswith(".csv"):
                content = _get_bytes(f.get("url") or f.get("downloadUri"))
                if content is None:
                    print(f"    {name}: download failed")
                    break
                return [_parse_combined(content, title)], title
    return [], ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(backfill: bool = False) -> None:
    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if backfill else "incremental"
    print(f"USGS MCS Helium Pipeline  mode={mode}\n")

    os.makedirs(BASE_DIR, exist_ok=True)

    years = list(range(BACKFILL_START, now.year + 1))
    if not backfill:
        years = years[::-1]   # newest release first, stop at the first hit

    frames: list[pd.DataFrame] = []
    fetched: list[int] = []
    for release_year in years:
        print(f"[{release_year}] searching ScienceBase...")
        rel_frames, title = _fetch_release(release_year)
        rel_frames = [f for f in rel_frames if not f.empty]
        if rel_frames:
            total = sum(len(f) for f in rel_frames)
            print(f"  {title}: {total:,} records")
            frames.extend(rel_frames)
            fetched.append(release_year)
            time.sleep(REQUEST_INTERVAL)
        else:
            print(f"  no helium data release found")
        if not backfill and fetched:
            break

    if not frames:
        print("No data fetched for any release year.")
        return

    combined = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["obs_year", "series", "commodity", "country",
                                 "value", "source_release"])
        .sort_values(["commodity", "series", "obs_year"])
        .reset_index(drop=True)
    )
    combined["source"]     = "USGS MCS ScienceBase"
    combined["fetched_at"] = now.isoformat()

    path = write_partitioned(
        combined, BASE_DIR,
        f"usgs_mcs_helium_{mode}_{today_str}.parquet",
    )
    print(f"\n-> {path}")
    print(f"   {len(combined):,} rows | releases: {fetched} | "
          f"commodities: {sorted(combined['commodity'].unique())}")

    print("\n--- USGS MCS HELIUM PIPELINE COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="USGS Mineral Commodity Summaries helium (+ rare gases) annual releases"
    )
    parser.add_argument("--backfill", action="store_true",
                        help=f"Fetch every release {BACKFILL_START}-present (vs. latest only)")
    args = parser.parse_args()
    main(backfill=args.backfill)
