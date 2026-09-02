#!/usr/bin/env python3
"""
Robo Global (ROBO) Holdings Pipeline:
  Fetches quarterly holdings for the ROBO Global Robotics and Automation
  Index ETF from its SEC N-PORT filing via EdgarTools.
  Writes to Iceberg table: constituents.fund_holdings with source='edgar_nport:ROBO'

  Data source: SEC EDGAR N-PORT-P filings, Exchange Traded Concepts Trust
  (CIK 1452937), series S000042659.

  Why a dedicated script instead of fund_holdings_pipeline.py's generic
  EdgarTools path: `Fund('ROBO').get_latest_report()` and `.get_portfolio()`
  both resolve to the WRONG series -- Exchange Traded Concepts Trust sponsors
  dozens of unrelated ETFs (EMQQ, FMQQ, ETC 6 Meridian funds, Range funds,
  etc.) and both of those EdgarTools calls return whatever series the trust
  filed most recently, not the one matching the ticker. `Fund('ROBO').series`
  DOES resolve correctly to S000042659 -- the bug is only in report/portfolio
  fetching, not ticker resolution. Confirmed live 2026-09-01: get_portfolio()
  returned a mega-cap dividend basket (Altria, Verizon, Coca-Cola...), not
  robotics/automation names. Fixed here by walking the trust's NPORT-P
  filings newest-first and taking the first one whose own series_id matches.

  Reporting cadence: N-PORT is quarterly with a ~60 day filing lag, so
  "daily" runs will keep re-writing the same latest quarter until a new one
  is filed -- expected and harmless (Iceberg overwrite is idempotent per
  fund_ticker).

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import os
import sys
import time
import logging
import pandas as pd
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

EDGAR_USER_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "financial-data-pipeline research@example.com",
)
EDGAR_SLEEP = 0.15  # seconds between EdgarTools filing.obj() calls (~6 req/sec)

STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = STORAGE_ROOT / "iceberg"
CATALOG_DB = ICEBERG_WAREHOUSE / "constituents_catalog.db"

SNAPSHOT_DATE = date.today()
FETCHED_AT = datetime.now(timezone.utc)

TRUST_CIK = 1452937          # Exchange Traded Concepts Trust
ROBO_SERIES_ID = "S000042659"
ROBO_TICKER = "ROBO"
ROBO_FUND_NAME = "ROBO Global Robotics and Automation Index ETF"

# Not securities -- cash sweep / short-term investment vehicles.
NON_SECURITY_ASSET_CATEGORIES = {"STIV"}

MAX_FILINGS_SCANNED = 60  # trust files ~30 series/quarter; one quarter's batch is enough


def find_robo_report():
    """Walk the trust's NPORT-P filings (newest first) for the one whose
    series_id matches ROBO -- Fund(ticker).get_latest_report() picks the
    wrong series for this multi-series trust (see module docstring)."""
    from edgar import Company, set_identity

    set_identity(EDGAR_USER_AGENT)
    company = Company(TRUST_CIK)
    filings = company.get_filings(form="NPORT-P")
    log.info(f"[{ROBO_TICKER}] Scanning up to {MAX_FILINGS_SCANNED} of {len(filings)} trust NPORT-P filings for series {ROBO_SERIES_ID}...")

    for i in range(min(MAX_FILINGS_SCANNED, len(filings))):
        filing = filings[i]
        try:
            report = filing.obj()
        except Exception as e:
            log.warning(f"[{ROBO_TICKER}] Filing {i} ({filing.filing_date}) failed to parse: {e}")
            continue
        if report.series_id == ROBO_SERIES_ID:
            log.info(f"[{ROBO_TICKER}] Matched series at filing index {i}: {filing.filing_date}, report name={report.name!r}")
            return report
        time.sleep(EDGAR_SLEEP)

    raise RuntimeError(f"No NPORT-P filing for series {ROBO_SERIES_ID} found in the first {MAX_FILINGS_SCANNED} trust filings")


def parse_robo_holdings(report) -> pd.DataFrame:
    """Parse the matched N-PORT report into our schema."""
    df = report.securities_data()
    if df is None or df.empty:
        raise RuntimeError("Empty portfolio for ROBO")

    before = len(df)
    df = df[~df["asset_category"].isin(NON_SECURITY_ASSET_CATEGORIES)].copy()
    dropped = before - len(df)
    if dropped > 0:
        log.info(f"[{ROBO_TICKER}] Filtered {dropped} cash-sweep rows")

    for col in ["value_usd", "pct_value", "balance", "annualized_rate"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    reporting_period = pd.to_datetime(report.reporting_period, errors="coerce")

    result = pd.DataFrame({
        "snapshot_date": SNAPSHOT_DATE,
        "fund_ticker": ROBO_TICKER,
        "fund_name": ROBO_FUND_NAME,
        "fund_cik": int(report.cik) if report.cik else None,
        "holding_ticker": df.get("ticker"),
        "holding_name": df.get("name"),
        "cusip": df.get("cusip"),
        "isin": df.get("isin"),
        "figi": None,
        "sedol": None,
        "weight_pct": df.get("pct_value"),
        "market_value_usd": df.get("value_usd"),
        "shares_held": df.get("balance"),
        "asset_category": df.get("asset_category"),
        "sector": None,
        "country": df.get("investment_country"),
        "issuer_name": df.get("name"),
        "filing_date": None,
        "reporting_period_end": reporting_period.date() if pd.notnull(reporting_period) else None,
        "source": f"edgar_nport:{ROBO_TICKER}",
        "fetched_at": FETCHED_AT,
        "par_value": None,
        "maturity_date": None,
        "coupon_pct": df.get("annualized_rate"),
        "duration": None,
        "ytm_pct": None,
    })

    log.info(f"[{ROBO_TICKER}] Output: {len(result)} holdings")
    return result


def write_to_iceberg(df: pd.DataFrame) -> int:
    """Write ROBO holdings to Iceberg, overwriting the fund_ticker partition."""
    import pyarrow as pa
    from pyiceberg.catalog import load_catalog
    from pyiceberg.expressions import EqualTo

    if df is None or df.empty:
        log.warning("[Iceberg] No data to write.")
        return 0

    log.info(f"[Iceberg] Writing {len(df)} rows for {ROBO_TICKER}...")

    try:
        catalog = load_catalog(
            "constituents",
            type="sql",
            uri=f"sqlite:///{CATALOG_DB.as_posix()}",
            warehouse=f"file://{ICEBERG_WAREHOUSE.as_posix()}",
        )
        table = catalog.load_table("constituents.fund_holdings")
    except Exception as e:
        log.error(f"[Iceberg] Failed to load catalog/table: {e}")
        return 0

    for col in ["snapshot_date", "filing_date", "reporting_period_end", "maturity_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    df["fetched_at"] = pd.Timestamp.now(tz="UTC")

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
        log.error(f"[Iceberg] Arrow schema conversion failed: {e}")
        return 0

    total_written = 0
    try:
        with table.transaction() as txn:
            txn.overwrite(arrow_table, overwrite_filter=EqualTo("fund_ticker", ROBO_TICKER))
            total_written = len(arrow_table)
            log.info(f"[Iceberg]   {ROBO_TICKER}: {total_written} rows staged")
    except Exception as e:
        log.error(f"[Iceberg] Transaction commit FAILED: {e}")
        return 0

    try:
        from iceberg_utils import expire_old_snapshots
        table.refresh()
        expire_old_snapshots(table, retain_days=30, log=log)
    except Exception as e:
        log.warning(f"[Iceberg] Snapshot expiration failed (non-fatal): {e}")

    if total_written > 0:
        log.info(f"[Iceberg] Total written: {total_written} rows")
        try:
            import duckdb
            result = duckdb.sql(
                f"SELECT count(*) FROM read_parquet("
                f"'{ICEBERG_WAREHOUSE.as_posix()}/constituents/fund_holdings/**/*.parquet', "
                f"hive_partitioning=true)"
            ).fetchone()
            log.info(f"[Iceberg] Total rows in fund_holdings: {result[0]}")
        except Exception as e:
            log.warning(f"[Iceberg] Verification query failed: {e}")

    return total_written


def main():
    log.info("=" * 60)
    log.info("ROBO Global Holdings Pipeline — %s", SNAPSHOT_DATE)
    log.info("=" * 60)

    try:
        report = find_robo_report()
        df = parse_robo_holdings(report)
    except Exception as e:
        log.error(f"[{ROBO_TICKER}] FAILED: {e}")
        df = pd.DataFrame()

    write_to_iceberg(df)
    log.info("Done.")


if __name__ == "__main__":
    main()
