#!/usr/bin/env python3
"""
OpenFIGI Identifier Resolution Pipeline:
  Resolves tickers to FIGI, Composite FIGI, security type, and exchange
  using the OpenFIGI v3 mapping API.

  Collects unique tickers from existing Iceberg tables (index_members,
  fund_holdings, securities) and enriches them with OpenFIGI identifiers.

  Writes to Iceberg table: constituents.identifier_map

  API:  https://api.openfigi.com/v3/mapping
  Auth: X-OPENFIGI-APIKEY header (optional — free tier has lower rate limits)
  Rate: 25 req/min (no key) or ~250 req/min (with key); 10 jobs/req (no key)
        or 100 jobs/req (with key)

  NOTE: OpenFIGI resolves tickers → FIGI. It does NOT return CUSIP/ISIN/SEDOL.
        Those must come from other sources (Wikipedia, EDGAR, Bloomberg).
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).parent))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STORAGE_ROOT = Path(__file__).parent / "storage" / "iceberg"
CATALOG_DB = STORAGE_ROOT / "constituents_catalog.db"
SNAPSHOT_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")

OPENFIGI_BASE = "https://api.openfigi.com/v3/mapping"
OPENFIGI_API_KEY = os.environ.get("OPENFIGI_API_KEY", "")

BATCH_SIZE_WITH_KEY = 100
BATCH_SIZE_NO_KEY = 10
RATE_LIMIT_RPM_WITH_KEY = 250   # approx
RATE_LIMIT_RPM_NO_KEY = 25
DELAY_WITH_KEY = 60.0 / RATE_LIMIT_RPM_WITH_KEY   # ~0.24s
DELAY_NO_KEY = 60.0 / RATE_LIMIT_RPM_NO_KEY        # 2.4s

log = logging.getLogger("openfigi")


# ---------------------------------------------------------------------------
# Collect tickers from existing Iceberg tables
# ---------------------------------------------------------------------------
def collect_tickers_from_iceberg() -> set[str]:
    """Read unique tickers from index_members, fund_holdings, and securities."""
    import duckdb

    con = duckdb.connect()
    tickers = set()

    # index_members — constituent tickers
    glob_pattern = (STORAGE_ROOT / "constituents" / "index_members" / "**" / "*.parquet").as_posix()
    try:
        df = con.sql(
            f"SELECT DISTINCT ticker FROM read_parquet('{glob_pattern}', hive_partitioning=true) "
            f"WHERE ticker IS NOT NULL AND ticker != ''"
        ).fetchdf()
        tickers.update(df["ticker"].str.upper().tolist())
        log.info("index_members: %d unique tickers", len(df))
    except Exception as e:
        log.warning("Could not read index_members: %s", e)

    # fund_holdings — holding tickers
    glob_pattern = (STORAGE_ROOT / "constituents" / "fund_holdings" / "**" / "*.parquet").as_posix()
    try:
        df = con.sql(
            f"SELECT DISTINCT holding_ticker FROM read_parquet('{glob_pattern}', hive_partitioning=true) "
            f"WHERE holding_ticker IS NOT NULL AND holding_ticker != ''"
        ).fetchdf()
        tickers.update(df["holding_ticker"].str.upper().tolist())
        log.info("fund_holdings: %d unique holding tickers", len(df))
    except Exception as e:
        log.warning("Could not read fund_holdings: %s", e)

    # securities — all symbols
    glob_pattern = (STORAGE_ROOT / "constituents" / "securities" / "**" / "*.parquet").as_posix()
    try:
        df = con.sql(
            f"SELECT DISTINCT symbol FROM read_parquet('{glob_pattern}') "
            f"WHERE symbol IS NOT NULL AND symbol != ''"
        ).fetchdf()
        tickers.update(df["symbol"].str.upper().tolist())
        log.info("securities: %d unique symbols", len(df))
    except Exception as e:
        log.warning("Could not read securities: %s", e)

    con.close()
    log.info("Total unique tickers collected: %d", len(tickers))
    return tickers


def get_existing_mapped_tickers() -> set[str]:
    """Read tickers already in the identifier_map Iceberg table."""
    import duckdb

    con = duckdb.connect()
    glob_pattern = (STORAGE_ROOT / "constituents" / "identifier_map" / "**" / "*.parquet").as_posix()
    try:
        df = con.sql(
            f"SELECT DISTINCT ticker FROM read_parquet('{glob_pattern}') "
            f"WHERE ticker IS NOT NULL"
        ).fetchdf()
        return set(df["ticker"].str.upper().tolist())
    except Exception:
        return set()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# OpenFIGI API
# ---------------------------------------------------------------------------
def openfigi_lookup(jobs: list[dict]) -> list[dict]:
    """POST a batch of mapping jobs to OpenFIGI v3.

    Args:
        jobs: list of {"idType": "TICKER", "idValue": "AAPL", "exchCode": "US"}

    Returns:
        list of response items (each has "data" or "warning")
    """
    headers = {"Content-Type": "application/json"}
    if OPENFIGI_API_KEY:
        headers["X-OPENFIGI-APIKEY"] = OPENFIGI_API_KEY

    for attempt in range(5):
        try:
            resp = requests.post(OPENFIGI_BASE, json=jobs, headers=headers, timeout=30)
            if resp.status_code == 429:
                # Rate limited — read retry-after or back off
                retry_after = int(resp.headers.get("Retry-After", 60))
                log.warning("Rate limited (429). Waiting %ds...", retry_after)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            log.warning("OpenFIGI request failed (attempt %d): %s", attempt + 1, e)
            time.sleep(2 ** attempt)
    log.error("OpenFIGI lookup failed after 5 attempts")
    return []


def resolve_tickers(tickers: list[str]) -> pd.DataFrame:
    """Resolve a list of tickers via OpenFIGI and return a DataFrame."""
    batch_size = BATCH_SIZE_WITH_KEY if OPENFIGI_API_KEY else BATCH_SIZE_NO_KEY
    delay = DELAY_WITH_KEY if OPENFIGI_API_KEY else DELAY_NO_KEY
    results = []

    total_batches = (len(tickers) + batch_size - 1) // batch_size
    log.info("Resolving %d tickers in %d batches (batch_size=%d, delay=%.1fs)...",
             len(tickers), total_batches, batch_size, delay)

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        batch_num = i // batch_size + 1

        jobs = [
            {"idType": "TICKER", "idValue": t, "exchCode": "US"}
            for t in batch
        ]

        responses = openfigi_lookup(jobs)

        for ticker, resp_item in zip(batch, responses):
            if "warning" in resp_item or "error" in resp_item:
                # No match — still record the ticker as unmatched
                results.append({
                    "ticker": ticker,
                    "company_name": None,
                    "cusip": None,
                    "isin": None,
                    "figi": None,
                    "composite_figi": None,
                    "sedol": None,
                    "cik": None,
                    "exchange": None,
                    "currency": None,
                    "security_type": None,
                    "is_active": False,
                    "source": "openfigi",
                    "fetched_at": datetime.now(timezone.utc),
                })
                continue

            data_list = resp_item.get("data", [])
            if not data_list:
                results.append({
                    "ticker": ticker,
                    "company_name": None,
                    "cusip": None,
                    "isin": None,
                    "figi": None,
                    "composite_figi": None,
                    "sedol": None,
                    "cik": None,
                    "exchange": None,
                    "currency": None,
                    "security_type": None,
                    "is_active": False,
                    "source": "openfigi",
                    "fetched_at": datetime.now(timezone.utc),
                })
                continue

            # Use first (most specific) match
            d = data_list[0]
            results.append({
                "ticker": d.get("ticker", ticker),
                "company_name": d.get("name"),
                "cusip": None,   # OpenFIGI doesn't return CUSIP
                "isin": None,    # OpenFIGI doesn't return ISIN
                "figi": d.get("figi"),
                "composite_figi": d.get("compositeFIGI"),
                "sedol": None,   # OpenFIGI doesn't return SEDOL
                "cik": None,
                "exchange": d.get("exchCode"),
                "currency": None,
                "security_type": d.get("securityType"),
                "is_active": True,
                "source": "openfigi",
                "fetched_at": datetime.now(timezone.utc),
            })

        if batch_num % 10 == 0 or batch_num == total_batches:
            log.info("  Batch %d/%d done (%d tickers resolved so far)",
                     batch_num, total_batches, len(results))

        time.sleep(delay)

    df = pd.DataFrame(results)
    matched = df["figi"].notna().sum()
    log.info("Resolution complete: %d/%d tickers matched (%.1f%%)",
             matched, len(df), 100 * matched / len(df) if df.shape[0] else 0)
    return df


# ---------------------------------------------------------------------------
# Write to Iceberg
# ---------------------------------------------------------------------------
def write_to_iceberg(df: pd.DataFrame) -> int:
    """Append resolved tickers to Iceberg; curated.py dedupes by ticker on
    read (keeps latest fetched_at), same pattern as every other Iceberg-
    backed table. NOTE: this must NOT be a full-table overwrite -- main()'s
    default (non-backfill) mode only resolves *new* unmapped tickers each
    run, so overwriting the whole table with just that small incremental
    batch would silently delete every previously-resolved ticker the next
    time run_all.py's openfigi stage finds anything new to resolve."""
    import pyarrow as pa
    from pyiceberg.catalog import load_catalog

    if df.empty:
        log.warning("[Iceberg] No data to write.")
        return 0

    log.info("[Iceberg] Writing %d rows to constituents.identifier_map...", len(df))

    catalog = load_catalog(
        "constituents",
        type="sql",
        uri=f"sqlite:///{CATALOG_DB.as_posix()}",
        warehouse=f"file://{STORAGE_ROOT.as_posix()}",
    )
    table = catalog.load_table("constituents.identifier_map")

    # Prepare DataFrame
    for col in ["cusip", "isin", "sedol"]:
        if col not in df.columns:
            df[col] = None
    df["fetched_at"] = pd.Timestamp.now(tz="UTC")

    arrow_schema = pa.schema([
        pa.field("ticker", pa.string(), nullable=True),
        pa.field("company_name", pa.string(), nullable=True),
        pa.field("cusip", pa.string(), nullable=True),
        pa.field("isin", pa.string(), nullable=True),
        pa.field("figi", pa.string(), nullable=True),
        pa.field("composite_figi", pa.string(), nullable=True),
        pa.field("sedol", pa.string(), nullable=True),
        pa.field("cik", pa.int64(), nullable=True),
        pa.field("exchange", pa.string(), nullable=True),
        pa.field("currency", pa.string(), nullable=True),
        pa.field("security_type", pa.string(), nullable=True),
        pa.field("is_active", pa.bool_(), nullable=True),
        pa.field("source", pa.string(), nullable=False),
        pa.field("fetched_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ])

    col_order = [f.name for f in arrow_schema]
    for col in col_order:
        if col not in df.columns:
            df[col] = None
    df = df[col_order]

    arrow_table = pa.Table.from_pandas(df, schema=arrow_schema, preserve_index=False)

    table.append(arrow_table)
    log.info("[Iceberg] Successfully appended %d rows to constituents.identifier_map",
             len(arrow_table))

    # Verify
    import duckdb
    result = duckdb.sql(
        f"SELECT count(*) FROM read_parquet("
        f"'{STORAGE_ROOT.as_posix()}/constituents/identifier_map/**/*.parquet')"
    ).fetchone()
    log.info("[Iceberg] Total rows in identifier_map: %d", result[0])
    return result[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(backfill: bool = False, tickers: list[str] | None = None):
    log.info("=" * 60)
    log.info("OpenFIGI Identifier Resolution — %s", SNAPSHOT_DATE)
    log.info("=" * 60)

    if tickers:
        all_tickers = set(t.upper() for t in tickers)
        log.info("Using %d user-specified tickers", len(all_tickers))
    else:
        all_tickers = collect_tickers_from_iceberg()

    if not all_tickers:
        log.warning("No tickers to resolve.")
        return

    if not backfill:
        existing = get_existing_mapped_tickers()
        new_tickers = all_tickers - existing
        log.info("Skipping %d already-mapped tickers, %d new to resolve",
                 len(existing), len(new_tickers))
        all_tickers = new_tickers

    if not all_tickers:
        log.info("All tickers already mapped. Nothing to do.")
        return

    # Resolve
    sorted_tickers = sorted(all_tickers)
    df = resolve_tickers(sorted_tickers)

    if df.empty:
        log.warning("No results from OpenFIGI.")
        return

    # Write to Iceberg
    write_to_iceberg(df)

    log.info("=" * 60)
    log.info("Done.")
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Resolve tickers to FIGI identifiers via OpenFIGI v3 API",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Re-resolve all tickers (even those already mapped)",
    )
    parser.add_argument(
        "--tickers", nargs="+",
        help="Specific tickers to resolve (default: auto-collect from Iceberg tables)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    main(backfill=args.backfill, tickers=args.tickers)
