#!/usr/bin/env python3
"""
EPA Fuel & Emissions Data Pipeline.

Two keyless EPA datasets relevant to fuel markets:

1. epa_ghg_energy  — Inventory of U.S. Greenhouse Gas Emissions and Sinks,
   Chapter 3 (Energy) Table 3-1: CO2, CH4, and N2O emissions from energy
   by economic sector/source, 1990-2022, in MMT CO2 Eq.
   Source: EPA GHG Inventory Data Explorer report ZIP (energy.zip). The URL
   is pinned to the April 2024 release; a newer inventory needs a URL bump.

   The table is a hierarchy, so its rows must NOT be summed blindly. The
   `level` column says what each row is:
     gas_total   one per gas ("Total"), plus gas="All Gases" grand total
     source      top-level sources; these sum to their gas_total
     sub_source  the sector split of CO2 "Fossil Fuel Combustion" (already
                 counted inside that source row)
     memo        biomass/biofuel CO2 and international bunker fuels, which
                 EPA reports but excludes from its totals

2. epa_rfs_rin  — Renewable Fuel Standard (RFS) RIN generation and renewable
   fuel volume production by month and D-code, July 2010-present.
   Source: EPA "Spreadsheet of RIN Generation and Renewable Fuel Volume
   Production by Month" CSV. Each monthly file carries the full history back
   to July 2010, revised, so only the newest file is ever needed. The most
   recent month is partial when first published and is revised upward later.

No API key required for either. Exits 1 if either table comes back empty, so
a moved URL or renamed column shows up as a FAIL in run_all.py.

CLI:
  python epa_fuel_pipeline.py            # newest RFS file + GHG zip
  python epa_fuel_pipeline.py --backfill # same thing (the newest RFS file is
                                         # already the full, revised history)

Outputs:
  storage/raw/epa/ghg_energy/**/*.parquet   (CATALOG: epa_ghg_energy)
  storage/raw/epa/rfs_rin/**/*.parquet      (CATALOG: epa_rfs_rin)
"""

import argparse
import csv
import datetime
import io
import os
import re
import sys
import time
import zipfile

import pandas as pd
import requests

from storage_utils import write_partitioned

GHG_DIR      = os.path.join("storage", "raw", "epa", "ghg_energy")
RFS_DIR      = os.path.join("storage", "raw", "epa", "rfs_rin")

GHG_ZIP_URL  = "https://www.epa.gov/system/files/other-files/2024-04/energy.zip"
GHG_TABLE    = "energy_10-15/Table 3-1.csv"
RFS_PAGE_URL = ("https://www.epa.gov/fuels-registration-reporting-and-compliance-help/"
                "spreadsheet-rin-generation-and-renewable-fuel")

REQUEST_INTERVAL = 0.5
MAX_RETRIES      = 3

# D-code -> renewable fuel category (40 CFR 80.1426)
FUEL_NAMES = {
    3: "Cellulosic biofuel",
    4: "Biomass-based diesel",
    5: "Advanced biofuel",
    6: "Renewable fuel",
    7: "Cellulosic diesel",
}

# Table 3-1 hierarchy. CO2 "Fossil Fuel Combustion" is broken down by sector
# on the rows right after it; those rows are already inside it.
FFC_SECTORS = {
    "Transportation", "Electricity Generation", "Industrial",
    "Residential", "Commercial", "U.S. Territories",
}
# Reported by EPA but excluded from the totals (footnotes a and b in the
# table, whose letters are glued onto these labels in the CSV).
MEMO_PREFIXES = ("International Bunker Fuels", "Biomass", "Biofuels")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ghg_level(gas: str, source: str) -> str:
    if source == "Total":
        return "gas_total"
    if source.startswith(MEMO_PREFIXES):
        return "memo"
    if gas == "CO2" and source in FFC_SECTORS:
        return "sub_source"
    return "source"


def _get(url, params=None, timeout=60):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                return resp
            if resp.status_code == 429:
                print(f"  429 on {url} — backing off {60*attempt}s")
                time.sleep(60 * attempt)
            elif resp.status_code in (404, 410):
                return None
            else:
                print(f"  HTTP {resp.status_code} on {url}")
                if attempt >= MAX_RETRIES:
                    return None
                time.sleep(15 * attempt)
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}) on {url}: {exc}")
            time.sleep(15 * attempt)
    return None


# ---------------------------------------------------------------------------
# EPA GHG Inventory — Energy chapter Table 3-1 (MMT CO2 Eq. by gas/source)
# ---------------------------------------------------------------------------

def _parse_ghg_table(content: str) -> pd.DataFrame:
    """
    Table 3-1 layout: rows alternate 'Gas' totals and source breakdowns.
      CO2, <year values...>          <- total CO2 emissions from energy
      Fossil Fuel Combustion, ...    <- CO2 breakdown
      Transportation, ...
      ...
      CH4, ...                       <- next gas total
      Natural Gas Systems, ...
      ...
      Total, ...                     <- grand total (all gases)
    Footnotes and blank rows are skipped.
    """
    lines = content.strip().splitlines()
    records = []          # (gas, source, year, value)
    column_years = None
    current_gas = None

    for raw in lines:
        # csv parse each line to handle quoted thousands separators
        row = next(csv.reader(io.StringIO(raw)))
        if not row or not row[0]:
            continue
        first = row[0].strip()
        if first.startswith("Table 3-1:"):
            continue
        if first.startswith(("+", "Note:", "a ", "b ")):
            continue

        cells = [c.strip() for c in row]
        if first == "Gas/Source":
            column_years = [int(y) for y in cells[1:] if y]
            continue

        # All value rows have the same fixed width as the header
        if len(cells) - 1 != len(column_years or []):
            continue

        label = cells[0]
        if label in ("CO2", "CH4", "N2O"):
            current_gas = label
            source = "Total"
        elif label == "Total":
            current_gas = "All Gases"
            source = "Total"
        else:
            source = label
            # Footnote letters are glued on: "International Bunker Fuelsb"
            if source.startswith(MEMO_PREFIXES):
                source = re.sub(r"(?<=[A-Za-z])[ab]$", "", source)
        if current_gas is None:
            continue

        for year, cell in zip(column_years, cells[1:]):
            cleaned = cell.replace(",", "")
            if cleaned == "+" or cleaned in ("", "NA"):
                continue
            try:
                value = float(cleaned)
            except ValueError:
                continue
            records.append((current_gas, source, year, value))

    df = pd.DataFrame(records, columns=["gas", "source", "obs_year", "value"])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(
        df["obs_year"].astype(str) + "-07-01", errors="coerce"
    )
    df["level"] = [_ghg_level(g, s) for g, s in zip(df["gas"], df["source"])]
    df["unit"] = "MMT CO2 Eq."
    df["fetched_at"] = _now_iso()
    df = df.drop(columns=["obs_year"])
    return df[["date", "gas", "source", "level", "value", "unit", "fetched_at"]]


def fetch_ghg_energy():
    resp = _get(GHG_ZIP_URL, timeout=120)
    if resp is None:
        return 0
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        content = zf.read(GHG_TABLE).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        print(f"  Failed to unpack {GHG_TABLE} from energy.zip: {exc}")
        return 0
    df = _parse_ghg_table(content)
    if df.empty:
        print("  No GHG energy rows parsed")
        return 0
    path = write_partitioned(df, GHG_DIR, "epa_ghg_energy.parquet")
    print(f"  Wrote {len(df):,} GHG energy rows -> {path}")
    return len(df)


# ---------------------------------------------------------------------------
# EPA RFS RIN generation (monthly CSV, cumulative history in each file)
# ---------------------------------------------------------------------------

def _csv_urls_from_page():
    resp = _get(RFS_PAGE_URL, timeout=60)
    if resp is None:
        return []
    links = re.findall(r'href=["\']([^"\']*rindata_[a-z]{3}\d{4}\.csv)', resp.text, re.I)
    # EPA lists newest first
    return links


def _parse_rfs_csv(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    df = df.rename(columns={"production_month": "prod_month"})
    wanted = ["fuel_code", "rin_year", "prod_month", "rin_quantity", "batch_volume"]
    missing = [c for c in wanted if c not in df.columns]
    if missing:
        raise ValueError(f"RFS CSV is missing columns {missing}; got {list(df.columns)}")
    df = df[wanted]
    df["fuel_code"] = pd.to_numeric(df["fuel_code"], errors="coerce").astype("Int64")
    df["rin_year"] = pd.to_numeric(df["rin_year"], errors="coerce").astype("Int64")
    df["prod_month"] = pd.to_numeric(df["prod_month"], errors="coerce").astype("Int64")
    df["rin_quantity"] = pd.to_numeric(df["rin_quantity"], errors="coerce")
    df["batch_volume"] = pd.to_numeric(df["batch_volume"], errors="coerce")

    valid = df["rin_year"].notna() & df["prod_month"].notna()
    df = df[valid].copy()
    df["date"] = pd.to_datetime(
        df["rin_year"].astype("Int64").astype(str) + "-" +
        df["prod_month"].astype("Int64").astype(str).str.zfill(2) + "-01",
        errors="coerce",
    )
    df["fuel_name"] = df["fuel_code"].map(FUEL_NAMES).fillna("Unknown")
    df["fetched_at"] = _now_iso()
    df = df[df["date"].notna()]
    return df[["date", "fuel_code", "fuel_name", "rin_year", "prod_month",
               "rin_quantity", "batch_volume", "fetched_at"]].reset_index(drop=True)


RFS_FALLBACK_FILES = 3


def fetch_rfs_rin():
    """
    Write the newest RFS file. Each file is the full revised history, so older
    files add nothing but stale numbers. If the newest one fails, try the next
    couple (listed newest first) rather than writing nothing.
    """
    urls = _csv_urls_from_page()
    if not urls:
        print("  Could not discover RFS CSV links")
        return 0

    for url in urls[:RFS_FALLBACK_FILES]:
        resp = _get(url, timeout=60)
        if resp is None:
            print(f"    Failed: {url}")
            time.sleep(REQUEST_INTERVAL)
            continue
        try:
            df = _parse_rfs_csv(resp.text)
        except Exception as exc:
            print(f"    Parse error for {url}: {exc}")
            continue
        if df.empty:
            print(f"    No rows parsed from {url}")
            continue
        df = df.sort_values(["date", "fuel_code"]).reset_index(drop=True)
        path = write_partitioned(df, RFS_DIR, "epa_rfs_rin.parquet")
        print(f"  {url.split('/')[-1]}: wrote {len(df):,} RFS rows -> {path}")
        return len(df)
    return 0


def main():
    parser = argparse.ArgumentParser(description="EPA fuel & emissions data")
    parser.add_argument("--backfill", action="store_true",
                        help="accepted for run_all.py; same as a normal run")
    parser.parse_args()

    print("== EPA GHG energy emissions ==")
    n1 = fetch_ghg_energy()
    print(f"  ghg_energy: {n1:,} rows")

    print("== EPA RFS RIN generation ==")
    n2 = fetch_rfs_rin()
    print(f"  rfs_rin: {n2:,} rows")

    print(f"\nDone. {n1:,} GHG rows, {n2:,} RFS rows.")
    if not (n1 and n2):
        print("! FAIL: a table came back empty (URL moved or format changed?)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())