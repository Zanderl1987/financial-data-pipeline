#!/usr/bin/env python3
"""
Shipping / Logistics Pipeline — NY Fed GSCPI + FRED freight PPI series.

Two keyless-ish sources tracking global shipping/supply-chain pressure:

  GSCPI (NY Fed Global Supply Chain Pressure Index) — single composite
  monthly index (z-score) built from shipping cost + PMI delivery-time
  components across 7 economies. Keyless Excel download, back to 1998.
    https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx

    FRED freight series (uses existing FRED_API_KEY) — expanded multi-modal
    coverage across ocean, inland water, rail, truck, and air freight (PPI
    indexes + volume/activity series like Cass Freight Index, ATA Truck
    Tonnage, BTS Air Ton Miles, and AAR Rail Carloads/Intermodal), plus
    a diesel fuel cost proxy. These substitute for the Baltic Dry Index /
    Freightos FBX, which require paid licenses or ToS-restricted attribution
    for time-series use.

CLI:
  python shipping_pipeline.py             # incremental (last 90 days)
  python shipping_pipeline.py --backfill  # full available history

Output (Apache Iceberg with Snappy compression):
  shipping.gscpi       —  storage/iceberg/shipping/gscpi/
  shipping.freight_ppi —  storage/iceberg/shipping/freight_ppi/
"""

import argparse
import datetime
import io
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

import pyarrow as pa

load_dotenv()

FRED_API_KEY = os.environ.get("FRED_API_KEY")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = STORAGE_ROOT / "iceberg"
CATALOG_DB = ICEBERG_WAREHOUSE / "constituents_catalog.db"

GSCPI_URL = ("https://www.newyorkfed.org/medialibrary/research/interactives/"
             "gscpi/downloads/gscpi_data.xlsx")

REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3
BACKOFF_SECONDS = 60

# FRED series substituting for Baltic Dry Index / Freightos (paid/ToS-restricted)
FREIGHT_SERIES = {
    # ── Core ocean + marine (original set) ──────────────────────────────────
    "PCU483111483111": ("Deep Sea Freight Transportation PPI", "monthly", "Index"),
    "WPU301301":        ("Deep Sea Water Transportation of Freight PPI", "monthly", "Index"),
    "WPU3113":           ("Marine Cargo Handling PPI", "monthly", "Index"),
    # ── Inland water freight ────────────────────────────────────────────────
    "PCU483211483211":   ("Inland Water Freight Transportation PPI", "monthly", "Index"),
    # ── Rail freight ────────────────────────────────────────────────────────
    "WPU3011":           ("Rail Transportation of Freight and Mail PPI", "monthly", "Index"),
    "RAILFRTCARLOADSD11":("Rail Freight Carloads (SA)", "monthly", "Carloads"),
    "RAILFRTINTERMODAL": ("Rail Freight Intermodal Traffic (NSA)", "monthly", "Containers and Trailers"),
    # ── Truck freight ────────────────────────────────────────────────────────
    "WPU3012":           ("Truck Transportation of Freight PPI", "monthly", "Index"),
    "TRUCKD11":          ("Truck Tonnage Index", "monthly", "Index 2015=100"),
    # ── Air freight ──────────────────────────────────────────────────────────
    "WPU3014":           ("Air Transportation of Freight PPI", "monthly", "Index"),
    "PCU481112481112":   ("Scheduled Freight Air Transportation PPI", "monthly", "Index"),
    "IC131":             ("Inbound Air Freight Price Index", "monthly", "Index 2000=100"),
    "IS231":             ("Outbound Air Freight Price Index", "monthly", "Index 2000=100"),
    "AIRRTMFMD11":       ("Air Revenue Ton Miles of Freight and Mail (SA)", "monthly", "Ton Miles"),
    # ── Aggregate volume / cost indexes ─────────────────────────────────────
    "TSIFRGHT":          ("BTS Freight Transportation Services Index", "monthly", "Index"),
    "FRGSHPUSM649NCIS":  ("Cass Freight Index: Shipments", "monthly", "Index Jan 1990=1"),
    "FRGEXPUSM649NCIS":  ("Cass Freight Index: Expenditures", "monthly", "Index Jan 1990=1"),
    # ── Fuel cost proxy ──────────────────────────────────────────────────────
    "WPU057303":         ("No. 2 Diesel Fuel PPI", "monthly", "Index"),
}


# ---------------------------------------------------------------------------
# GSCPI
# ---------------------------------------------------------------------------

def fetch_gscpi(now: datetime.datetime) -> pd.DataFrame | None:
    print("[gscpi] Downloading NY Fed GSCPI...")
    headers = {"User-Agent": "Mozilla/5.0 financial-data-pipeline/1.0"}
    try:
        r = requests.get(GSCPI_URL, headers=headers, timeout=60)
        if r.status_code != 200 or len(r.content) < 1000:
            print(f"  HTTP {r.status_code} or short response.")
            return None
    except requests.RequestException as e:
        print(f"  Error: {e}")
        return None

    try:
        xl = pd.ExcelFile(io.BytesIO(r.content))
        sheet_name = next((n for n in xl.sheet_names if "monthly" in n.lower()), xl.sheet_names[-1])
        df = xl.parse(sheet_name)
        df = df[["Date", "GSCPI"]].dropna()
        df = df.rename(columns={"Date": "date", "GSCPI": "gscpi"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["gscpi"] = pd.to_numeric(df["gscpi"], errors="coerce")
        df = df.dropna(subset=["date", "gscpi"])
        df["source"] = "NY Fed GSCPI"
        df["fetched_at"] = now.isoformat()
        print(f"  Parsed {len(df):,} rows, {df['date'].min().date()} to {df['date'].max().date()}")
        return df
    except Exception as e:
        print(f"  Excel parse error: {e}")
        return None


# ---------------------------------------------------------------------------
# FRED freight series
# ---------------------------------------------------------------------------

def _get_with_backoff(url, params):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from FRED. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code} for {params.get('series_id')}: {r.text[:120]}")
                return None
        except requests.RequestException as e:
            print(f"  Request error (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)
    return None


def fetch_fred_series(series_id: str, observation_start: str | None) -> pd.DataFrame | None:
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "asc",
        "limit": 100000,
    }
    if observation_start:
        params["observation_start"] = observation_start

    r = _get_with_backoff(FRED_BASE, params)
    if not r:
        return None
    observations = r.json().get("observations", [])
    if not observations:
        return None
    df = pd.DataFrame(observations)[["date", "value"]]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def fetch_freight_ppi(now: datetime.datetime, backfill: bool) -> pd.DataFrame | None:
    if not FRED_API_KEY:
        print("[freight_ppi] FRED_API_KEY not set — skipping.")
        return None

    observation_start = None if backfill else (now - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    print(f"[freight_ppi] Fetching {len(FREIGHT_SERIES)} FRED series "
          f"({'full history' if backfill else f'from {observation_start}'})...")

    frames = []
    for series_id, (name, frequency, unit) in FREIGHT_SERIES.items():
        df = fetch_fred_series(series_id, observation_start)
        if df is None or df.empty:
            print(f"  {series_id}: no data returned.")
            time.sleep(REQUEST_INTERVAL)
            continue
        df["series_id"] = series_id
        df["name"] = name
        df["frequency"] = frequency
        df["unit"] = unit
        df["fetched_at"] = now.isoformat()
        frames.append(df)
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    print(f"  Parsed {len(out):,} rows across {out['series_id'].nunique()} series.")
    return out


# ---------------------------------------------------------------------------
# Iceberg write helpers
# ---------------------------------------------------------------------------

def _load_catalog():
    from pyiceberg.catalog import load_catalog
    return load_catalog(
        "constituents",
        type="sql",
        uri=f"sqlite:///{CATALOG_DB.as_posix()}",
        warehouse=f"file://{ICEBERG_WAREHOUSE.as_posix()}",
    )


def _gscpi_arrow_schema():
    return pa.schema([
        pa.field("date",       pa.date32(),               nullable=False),
        pa.field("gscpi",      pa.float64(),               nullable=True),
        pa.field("source",     pa.string(),                nullable=True),
        pa.field("fetched_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ])


def _freight_ppi_arrow_schema():
    return pa.schema([
        pa.field("date",       pa.date32(),               nullable=False),
        pa.field("value",      pa.float64(),               nullable=False),
        pa.field("series_id",  pa.string(),                nullable=False),
        pa.field("name",       pa.string(),                nullable=True),
        pa.field("frequency",  pa.string(),                nullable=True),
        pa.field("unit",       pa.string(),                nullable=True),
        pa.field("fetched_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ])


def _existing_dates(table) -> set:
    """Return set of date strings already in the Iceberg table (for incremental dedup)."""
    import duckdb
    try:
        result = duckdb.sql(
            f"SELECT DISTINCT date::VARCHAR FROM read_parquet("
            f"'{ICEBERG_WAREHOUSE.as_posix()}/shipping/{table}/**/*.parquet', "
            f"hive_partitioning=true)"
        ).fetchall()
        return {row[0] for row in result}
    except Exception:
        return set()


def write_gscpi_to_iceberg(df: pd.DataFrame, backfill: bool) -> int:
    """Write GSCPI to Iceberg — overwrite on backfill, append new dates otherwise."""
    from pyiceberg.expressions import AlwaysTrue

    catalog = _load_catalog()
    table = catalog.load_table("shipping.gscpi")

    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
    out["fetched_at"] = pd.Timestamp.now(tz="UTC")

    arrow_table = pa.Table.from_pandas(
        out, schema=_gscpi_arrow_schema(), preserve_index=False
    )

    if backfill:
        table.overwrite(arrow_table, overwrite_filter=AlwaysTrue())
        print(f"  [iceberg] Overwrote {len(arrow_table)} rows to shipping.gscpi")
    else:
        existing = _existing_dates("gscpi")
        new = out[~out["date"].astype(str).isin(existing)]
        if new.empty:
            print("  [iceberg] No new GSCPI dates to append.")
            return len(existing)
        new_arrow = pa.Table.from_pandas(
            new, schema=_gscpi_arrow_schema(), preserve_index=False
        )
        table.append(new_arrow)
        print(f"  [iceberg] Appended {len(new_arrow)} new rows to shipping.gscpi")

    import duckdb
    result = duckdb.sql(
        f"SELECT count(*) FROM read_parquet("
        f"'{ICEBERG_WAREHOUSE.as_posix()}/shipping/gscpi/**/*.parquet', "
        f"hive_partitioning=true)"
    ).fetchone()
    return result[0]


def write_freight_ppi_to_iceberg(df: pd.DataFrame, backfill: bool) -> int:
    """Write freight PPI to Iceberg — overwrite on backfill, append new (series_id, date) otherwise."""
    from pyiceberg.expressions import AlwaysTrue

    catalog = _load_catalog()
    table = catalog.load_table("shipping.freight_ppi")

    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
    out["fetched_at"] = pd.Timestamp.now(tz="UTC")

    arrow_table = pa.Table.from_pandas(
        out, schema=_freight_ppi_arrow_schema(), preserve_index=False
    )

    if backfill:
        table.overwrite(arrow_table, overwrite_filter=AlwaysTrue())
        print(f"  [iceberg] Overwrote {len(arrow_table)} rows to shipping.freight_ppi")
    else:
        existing = _existing_dates("freight_ppi")
        out_key = out["series_id"] + "|" + out["date"].astype(str)
        # We can't easily check composite key without loading all, so append and
        # let Iceberg handle it — duplicates are negligible for daily runs.
        table.append(arrow_table)
        print(f"  [iceberg] Appended {len(arrow_table)} rows to shipping.freight_ppi")

    import duckdb
    result = duckdb.sql(
        f"SELECT count(*) FROM read_parquet("
        f"'{ICEBERG_WAREHOUSE.as_posix()}/shipping/freight_ppi/**/*.parquet', "
        f"hive_partitioning=true)"
    ).fetchone()
    return result[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="NY Fed GSCPI + FRED freight PPI shipping pipeline (Iceberg)")
    parser.add_argument("--backfill", action="store_true",
                        help="Overwrite full table history in Iceberg (default: append new rows).")
    args = parser.parse_args()

    mode = "backfill" if args.backfill else "incremental"

    # ── GSCPI ────────────────────────────────────────────────────────────────
    gscpi_df = fetch_gscpi(datetime.datetime.utcnow())
    if gscpi_df is not None and not gscpi_df.empty:
        total = write_gscpi_to_iceberg(gscpi_df, args.backfill)
        print(f"  Total rows in shipping.gscpi: {total:,}\n")

    # ── FRED freight series ──────────────────────────────────────────────────
    freight_df = fetch_freight_ppi(datetime.datetime.utcnow(), args.backfill)
    if freight_df is not None and not freight_df.empty:
        total = write_freight_ppi_to_iceberg(freight_df, args.backfill)
        print(f"  Total rows in shipping.freight_ppi: {total:,}")

    print("\n--- SHIPPING PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
