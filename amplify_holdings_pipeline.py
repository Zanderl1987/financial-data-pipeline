#!/usr/bin/env python3
"""
Amplify Holdings Pipeline:
  Fetches daily fund holdings for Amplify ETFs from their public Google
  Firestore feed (the data that powers amplifyetfs.com's holdings tables).
  Writes to Iceberg table: constituents.fund_holdings with source='amplify:<TICKER>'

  Data source: Firestore REST API, project `amplify-etfs-data-feed`, keyless
  (public read rules -- no auth header needed).
    GET https://firestore.googleapis.com/v1/projects/amplify-etfs-data-feed/
        databases/(default)/documents/funds/{TICKER}/holdings
  Document IDs under each fund are as-of dates; pick the latest (max date) and
  read its `holdings` array. Shape per holding (verified live 2026-09-01):
    SecurityName, StockTicker, CUSIP, Price, Shares, MarketValue,
    Weightings ("5.29%"), holding_type ("equity"|"cash"), money_market_flag,
    NetAssets, CreationUnits, SharesOutstanding, Date.
  Some historical docs use the older MoneyMarketFlag naming -- parse tolerantly
  via field .get().

  Known gaps: same tokenization gotcha as every Firestore doc -- a value can be
  {nullValue:null}, {booleanValue:..}, {integerValue:".."}, {doubleValue:..} or
  {stringValue:..}; handled by _val(). Cash & Other rows (holding_type=cash or
  money_market_flag=true) are filtered, matching the other issuer pipelines.

  CLI:
    python amplify_holdings_pipeline.py                    # default universe (DIVO)
    python amplify_holdings_pipeline.py --tickers DIVO     # explicit

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import os
import sys
import re
import argparse
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

load_dotenv = None
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = STORAGE_ROOT / "iceberg"
CATALOG_DB = ICEBERG_WAREHOUSE / "constituents_catalog.db"

FIRESTORE_BASE = (
    "https://firestore.googleapis.com/v1/projects/amplify-etfs-data-feed/"
    "databases/(default)/documents"
)

# Amplify gap tickers (all Amplify funds available in the feed; DIVO is the gap ticker)
AMPLIFY_TICKERS = ["DIVO"]

CASH_HOLDING_TYPES = {"cash"}


def _val(v):
    """Unwrap a Firestore field value (nullValue/booleanValue/integerValue/
    doubleValue/stringValue)."""
    if not isinstance(v, dict) or not v:
        return None
    if "nullValue" in v:
        return None
    if "booleanValue" in v:
        return v["booleanValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return v["doubleValue"]
    if "stringValue" in v:
        return v["stringValue"]
    o = v.get("mapValue", {}).get("fields") or v.get("arrayValue")
    return o


def _array(field):
    """Return the list of maps under a Firestore `holdings` arrayValue field."""
    if not isinstance(field, dict):
        return []
    if "arrayValue" in field:
        return field["arrayValue"].get("values", [])
    if "values" in field:
        return field["values"]
    return []


def fetch_amplify_holdings(ticker: str) -> pd.DataFrame:
    """Fetch the latest holdings document for a single Amplify ETF."""
    log.info("[%s] Fetching latest holdings doc from Firestore...", ticker)

    # 1. list as-of-date docs under funds/<TICKER>/holdings
    doc_ids = []
    token = None
    while True:
        url = f"{FIRESTORE_BASE}/funds/{ticker}/holdings?pageSize=300"
        if token:
            url += f"&pageToken={token}"
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        payload = r.json()
        doc_ids += [d["name"].rsplit("/", 1)[-1] for d in payload.get("documents", [])]
        token = payload.get("nextPageToken")
        if not token:
            break
    if not doc_ids:
        raise RuntimeError(f"No holdings docs found for {ticker}")

    latest = sorted(doc_ids)[-1]
    log.info("[%s] Latest as-of date: %s (%d snapshots available)",
             ticker, latest, len(doc_ids))

    # 2. fetch the latest doc
    r = requests.get(f"{FIRESTORE_BASE}/funds/{ticker}/holdings/{latest}", timeout=60)
    r.raise_for_status()
    doc = r.json().get("fields", {})

    as_of = _val(doc.get("asOfDate")) or latest
    rows = []
    for item in _array(doc.get("holdings")):
        f = (item or {}).get("mapValue", {}).get("fields", {}) or {}
        money_mm = _val(f.get("money_market_flag"))
        if money_mm is None:
            money_mm = _val(f.get("MoneyMarketFlag")) not in (None, 0, False, "0", "false")
        htype = _val(f.get("holding_type"))
        name = _val(f.get("SecurityName")) or ""
        if htype in CASH_HOLDING_TYPES or money_mm is True or "cash" in name.lower():
            continue
        weight_raw = _val(f.get("Weightings"))
        weight = None
        if isinstance(weight_raw, str):
            m = re.search(r"-?\d+(?:\.\d+)?", weight_raw)
            if m:
                weight = float(m.group(0))
        row = {
            "snapshot_date": pd.to_datetime(_val(f.get("Date")) or as_of, errors="coerce").date(),
            "holding_ticker": _val(f.get("StockTicker")),
            "holding_name": name or None,
            "cusip": _val(f.get("CUSIP")),
            "weight_pct": weight,
            "market_value_usd": _val(f.get("MarketValue")),
            "shares_held": _val(f.get("Shares")),
            "price": _val(f.get("Price")),
            "asset_category": htype,
            "sector": None,
            "country": None,
        }
        if row["cusip"] in (None, "", "nan"):
            row["cusip"] = None
        rows.append(row)

    if not rows:
        raise RuntimeError(f"No securities found in latest {ticker} snapshot {latest}")

    df = pd.DataFrame(rows)
    df["fund_ticker"] = ticker
    df["fund_name"] = f"Amplify {ticker} ETF"
    df["source"] = f"amplify:{ticker}"
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"], errors="coerce")
    log.info("[%s] Parsed %d holdings (as of %s)", ticker, len(df), as_of)
    return df


def write_to_iceberg(all_data: list[pd.DataFrame]) -> int:
    """Write fund holdings to Iceberg, overwriting each fund_ticker partition."""
    import pyarrow as pa
    from pyiceberg.catalog import load_catalog
    from pyiceberg.expressions import EqualTo

    if not all_data:
        log.warning("[Iceberg] No data to write.")
        return 0

    combined = pd.concat(all_data, ignore_index=True)
    log.info("[Iceberg] Writing %d rows across %d funds...",
             len(combined), combined["fund_ticker"].nunique())

    try:
        catalog = load_catalog(
            "constituents",
            type="sql",
            uri=f"sqlite:///{CATALOG_DB.as_posix()}",
            warehouse=f"file://{ICEBERG_WAREHOUSE.as_posix()}",
        )
        table = catalog.load_table("constituents.fund_holdings")
    except Exception as e:
        log.error("[Iceberg] Failed to load catalog/table: %s", e)
        return 0

    df = combined.copy()
    for col in ["snapshot_date", "filing_date", "reporting_period_end", "maturity_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    df["fetched_at"] = pd.Timestamp.now(tz="UTC")

    c = df.columns
    for extra in ["price"]:
        if extra in c:
            df = df.drop(columns=[extra])

    arrow_schema = pa.schema([
        pa.field("snapshot_date", pa.date32(), nullable=False),
        pa.field("fund_ticker", pa.string(), nullable=False),
        pa.field("fund_name", pa.string(), nullable=True),
        pa.field("fund_cik", pa.int64(), nullable=True),
        pa.field("holding_ticker", pa.string(), nullable=True),
        pa.field("holding_name", pa.string(), nullable=True),
        pa.field("cusip", pa.string(), nullable=True),
        pa.field("isin", pa.string(), nullable=True),
        pa.field("figi", pa.string(), nullable=True),
        pa.field("sedol", pa.string(), nullable=True),
        pa.field("weight_pct", pa.float64(), nullable=True),
        pa.field("market_value_usd", pa.float64(), nullable=True),
        pa.field("shares_held", pa.float64(), nullable=True),
        pa.field("asset_category", pa.string(), nullable=True),
        pa.field("sector", pa.string(), nullable=True),
        pa.field("country", pa.string(), nullable=True),
        pa.field("issuer_name", pa.string(), nullable=True),
        pa.field("filing_date", pa.date32(), nullable=True),
        pa.field("reporting_period_end", pa.date32(), nullable=True),
        pa.field("source", pa.string(), nullable=False),
        pa.field("fetched_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("par_value", pa.float64(), nullable=True),
        pa.field("maturity_date", pa.date32(), nullable=True),
        pa.field("coupon_pct", pa.float64(), nullable=True),
        pa.field("duration", pa.float64(), nullable=True),
        pa.field("ytm_pct", pa.float64(), nullable=True),
    ])
    col_order = [f.name for f in arrow_schema]
    for col in col_order:
        if col not in df.columns:
            df[col] = None
    df = df[col_order]

    try:
        arrow_table = pa.Table.from_pandas(df, schema=arrow_schema, preserve_index=False)
    except Exception as e:
        log.error("[Iceberg] Arrow schema conversion failed: %s", e)
        return 0

    total_written = 0
    write_errors = []
    try:
        with table.transaction() as txn:
            for fund_ticker in sorted(df["fund_ticker"].unique()):
                try:
                    fund_df = arrow_table.filter(pa.compute.equal(arrow_table.column("fund_ticker"), fund_ticker))
                    txn.overwrite(fund_df, overwrite_filter=EqualTo("fund_ticker", fund_ticker))
                    total_written += len(fund_df)
                    log.info("[Iceberg]   %s: %d rows staged", fund_ticker, len(fund_df))
                except Exception as e:
                    log.error("[Iceberg]   %s: write FAILED: %s", fund_ticker, e)
                    write_errors.append(fund_ticker)
    except Exception as e:
        log.error("[Iceberg] Transaction commit FAILED: %s", e)
        return 0

    if write_errors:
        log.warning("[Iceberg] Write errors (%d): %s", len(write_errors), ", ".join(write_errors))

    try:
        from iceberg_utils import expire_old_snapshots
        table.refresh()
        expire_old_snapshots(table, retain_days=30, log=log)
    except Exception as e:
        log.warning("[Iceberg] Snapshot expiration failed (non-fatal): %s", e)

    if total_written > 0:
        log.info("[Iceberg] Total written: %d rows", total_written)
        try:
            import duckdb
            result = duckdb.sql(
                f"SELECT count(*) FROM read_parquet("
                f"'{ICEBERG_WAREHOUSE.as_posix()}/constituents/fund_holdings/**/*.parquet', "
                f"hive_partitioning=true)"
            ).fetchone()
            log.info("[Iceberg] Total rows in fund_holdings: %d", result[0])
        except Exception as e:
            log.warning("[Iceberg] Verification query failed: %s", e)

    return total_written


def main():
    parser = argparse.ArgumentParser(description="Amplify ETF holdings via Firestore")
    parser.add_argument("--tickers", nargs="+", default=AMPLIFY_TICKERS,
                        help="Amplify tickers to fetch (default all)")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Amplify Holdings Pipeline")
    log.info("=" * 60)

    frames = []
    for ticker in args.tickers:
        try:
            frames.append(fetch_amplify_holdings(ticker))
        except Exception as e:
            log.error("[%s] FAILED: %s", ticker, e)

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        log.info("TOTAL: %d rows across %d funds", len(combined), combined["fund_ticker"].nunique())
        for ft, count in combined.groupby("fund_ticker").size().items():
            log.info("  %-10s %d holdings", ft, count)
    else:
        log.warning("No data fetched.")

    write_to_iceberg(frames)
    log.info("Done.")


if __name__ == "__main__":
    main()