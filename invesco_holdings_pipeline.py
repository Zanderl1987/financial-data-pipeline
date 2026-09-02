#!/usr/bin/env python3
"""
Invesco Holdings Pipeline:
  Fetches daily holdings JSON for Invesco ETFs from their internal dng-api backend.
  Writes to Iceberg table: constituents.fund_holdings with source='invesco:<TICKER>'

  Data source: Invesco dng-api (powers the public product-detail page's holdings
  table, keyless, no auth). The public www.invesco.com CSV/XLSX download URLs
  used by other issuers 406 here -- this JSON endpoint does not.
  URL pattern: https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/{cusip}/holdings/fund?idType=cusip&productType=ETF

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import os
import sys
import time
import logging
import requests
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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = STORAGE_ROOT / "iceberg"
CATALOG_DB = ICEBERG_WAREHOUSE / "constituents_catalog.db"

SNAPSHOT_DATE = date.today()
FETCHED_AT = datetime.now(timezone.utc)

# Invesco gap tickers -> (CUSIP, fund name). CUSIP is the idType key the dng-api
# needs; found via the productMetaData block embedded in each ticker's
# product-detail page (the site is an AEM SPA -- no static holdings link/href).
INVESCO_FUNDS = {
    "DBC": ("46138B103", "Invesco DB Commodity Index Tracking Fund"),
    "PBJ": ("46137V753", "Invesco Food & Beverage ETF"),
    "PCY": ("46138E784", "Invesco Emerging Markets Sovereign Debt ETF"),
    "PEJ": ("46137V720", "Invesco Leisure & Entertainment ETF"),
    "TAN": ("46138G706", "Invesco Solar ETF"),
}

HOLDINGS_URL = (
    "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/{cusip}"
    "/holdings/fund?idType=cusip&productType=ETF"
)

# Pure cash/currency line items -- not securities. Futures/swaps/money-market
# collateral are kept: for commodity/bond funds like DBC/PCY those ARE the
# disclosed portfolio, not idle cash (same treatment as proshares_holdings_pipeline.py).
NON_SECURITY_TYPE_CODES = {"CURR", "UCURR", "CURRCOL"}


def fetch_invesco_holdings(ticker: str, cusip: str) -> dict:
    """Fetch holdings JSON for a single Invesco ETF."""
    url = HOLDINGS_URL.format(cusip=cusip)
    log.info(f"[{ticker}] Fetching from Invesco dng-api...")

    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    data = r.json()
    log.info(f"[{ticker}] Raw: {data.get('totalNumberOfHoldings')} holdings, as of {data.get('effectiveDate')}")
    return data


def parse_invesco_holdings(ticker: str, fund_name: str, data: dict) -> pd.DataFrame:
    """Parse Invesco dng-api JSON into our schema."""
    holdings = data.get("holdings", [])
    effective_date = pd.to_datetime(data.get("effectiveDate"), errors="coerce")

    df = pd.DataFrame(holdings)
    if df.empty:
        log.warning(f"[{ticker}] No holdings returned.")
        return pd.DataFrame()

    before = len(df)
    df = df[~df["securityTypeCode"].isin(NON_SECURITY_TYPE_CODES)]
    dropped = before - len(df)
    if dropped > 0:
        log.info(f"[{ticker}] Filtered {dropped} cash/currency rows")

    result = pd.DataFrame({
        "snapshot_date": SNAPSHOT_DATE,
        "fund_ticker": ticker,
        "fund_name": fund_name,
        "fund_cik": None,
        "holding_ticker": df["ticker"],
        "holding_name": df["issuerName"],
        "cusip": df["cusip"],
        "isin": None,
        "figi": None,
        "sedol": None,
        "weight_pct": pd.to_numeric(df["percentageOfTotalNetAssets"], errors="coerce"),
        "market_value_usd": pd.to_numeric(df["marketValueBase"], errors="coerce"),
        "shares_held": pd.to_numeric(df["units"], errors="coerce"),
        "asset_category": df["securityTypeName"],
        "sector": df.get("sectorName"),
        "country": None,
        "issuer_name": df["issuerName"],
        "filing_date": None,
        "reporting_period_end": effective_date.date() if pd.notnull(effective_date) else None,
        "source": f"invesco:{ticker}",
        "fetched_at": FETCHED_AT,
        "par_value": None,
        "maturity_date": pd.to_datetime(df.get("maturityDate"), errors="coerce").dt.date,
        "coupon_pct": pd.to_numeric(df.get("coupon"), errors="coerce"),
        "duration": None,
        "ytm_pct": None,
    })

    log.info(f"[{ticker}] Output: {len(result)} holdings")
    return result


def write_to_iceberg(all_data: list[pd.DataFrame]) -> int:
    """Write fund holdings to Iceberg, overwriting each fund_ticker partition."""
    import pyarrow as pa
    from pyiceberg.catalog import load_catalog
    from pyiceberg.expressions import EqualTo

    if not all_data:
        log.warning("[Iceberg] No data to write.")
        return 0

    combined = pd.concat(all_data, ignore_index=True)
    log.info(f"[Iceberg] Writing {len(combined)} rows across {combined['fund_ticker'].nunique()} funds...")

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

    df = combined.copy()
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
    write_errors = []
    try:
        with table.transaction() as txn:
            for fund_ticker in sorted(df["fund_ticker"].unique()):
                try:
                    fund_df = arrow_table.filter(pa.compute.equal(arrow_table.column("fund_ticker"), fund_ticker))
                    txn.overwrite(fund_df, overwrite_filter=EqualTo("fund_ticker", fund_ticker))
                    total_written += len(fund_df)
                    log.info(f"[Iceberg]   {fund_ticker}: {len(fund_df)} rows staged")
                except Exception as e:
                    log.error(f"[Iceberg]   {fund_ticker}: write FAILED: {e}")
                    write_errors.append(fund_ticker)
    except Exception as e:
        log.error(f"[Iceberg] Transaction commit FAILED: {e}")
        return 0

    if write_errors:
        log.warning(f"[Iceberg] Write errors ({len(write_errors)}): {', '.join(write_errors)}")

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
    log.info("Invesco Holdings Pipeline — %s", SNAPSHOT_DATE)
    log.info("=" * 60)

    frames = []
    for ticker, (cusip, fund_name) in INVESCO_FUNDS.items():
        try:
            data = fetch_invesco_holdings(ticker, cusip)
            frames.append(parse_invesco_holdings(ticker, fund_name, data))
        except Exception as e:
            log.error(f"[{ticker}] FAILED: {e}")
        time.sleep(1)  # Be polite

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        log.info("-" * 60)
        log.info("TOTAL: %d rows across %d funds", len(combined), combined["fund_ticker"].nunique())
        for ft, count in combined.groupby("fund_ticker").size().items():
            log.info("  %-10s %d holdings", ft, count)
        log.info("-" * 60)
    else:
        log.warning("No data fetched.")

    write_to_iceberg(frames)
    log.info("Done.")


if __name__ == "__main__":
    main()
