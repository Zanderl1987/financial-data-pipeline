#!/usr/bin/env python3
"""
Direxion Holdings Pipeline:
  Fetches daily holdings CSV for Direxion ETFs from their official site.
  Writes to Iceberg table: constituents.fund_holdings with source='direxion:<TICKER>'

  Data source: Direxion official daily holdings CSV (keyless, no auth)
  URL pattern: https://www.direxion.com/holdings/{ticker}.csv
  Note: a wrong/nonexistent ticker returns 403 (CDN "no such object"), not a WAF
  block -- confirmed via 8 rapid real-ticker requests all returning 200. The
  prior "403, likely WAF" finding was from probing the wrong path (missing the
  `.csv` suffix hits a Next.js route that also 403s).

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import os
import sys
import re
import time
import logging
import requests
import pandas as pd
from io import StringIO
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

# Direxion gap tickers
DIREXION_FUNDS = {
    "LABU": "Direxion Daily S&P Biotech Bull 3X ETF",
}

HOLDINGS_URL = "https://www.direxion.com/holdings/{ticker}.csv"


def fetch_direxion_holdings(ticker: str) -> pd.DataFrame:
    """Fetch holdings CSV for a single Direxion ETF."""
    url = HOLDINGS_URL.format(ticker=ticker)
    log.info(f"[{ticker}] Fetching from Direxion...")

    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()

    # First 3 lines are a title block (fund name, ticker, shares outstanding),
    # blank line, then the real CSV header.
    lines = r.text.splitlines()
    header_idx = next(i for i, l in enumerate(lines) if l.startswith('"TradeDate"'))
    df = pd.read_csv(StringIO("\n".join(lines[header_idx:])))
    log.info(f"[{ticker}] Raw: {len(df)} rows, cols: {list(df.columns)}")
    return df


def parse_direxion_holdings(ticker: str, fund_name: str, raw_df: pd.DataFrame) -> pd.DataFrame:
    """Parse Direxion CSV into our schema."""
    df = raw_df[raw_df["StockTicker"].notna() | raw_df["Cusip"].notna()].copy()
    dropped = len(raw_df) - len(df)
    if dropped > 0:
        log.info(f"[{ticker}] Filtered {dropped} rows with no ticker/cusip")

    effective_date = pd.to_datetime(df["TradeDate"], errors="coerce").dt.date

    result = pd.DataFrame({
        "snapshot_date": SNAPSHOT_DATE,
        "fund_ticker": ticker,
        "fund_name": fund_name,
        "fund_cik": None,
        "holding_ticker": df["StockTicker"].astype(str).str.strip().replace({"nan": None, "": None}),
        "holding_name": df["SecurityDescription"].astype(str).str.strip(),
        "cusip": df["Cusip"].astype(str).str.strip().replace({"nan": None}),
        "isin": None,
        "figi": None,
        "sedol": None,
        "weight_pct": pd.to_numeric(df["HoldingsPercent"], errors="coerce") * 100,
        "market_value_usd": pd.to_numeric(df["MarketValue"], errors="coerce"),
        "shares_held": pd.to_numeric(df["Shares"], errors="coerce"),
        "asset_category": "Common Stock",
        "sector": None,
        "country": None,
        "issuer_name": df["SecurityDescription"].astype(str).str.strip(),
        "filing_date": None,
        "reporting_period_end": effective_date,
        "source": f"direxion:{ticker}",
        "fetched_at": FETCHED_AT,
        "par_value": None,
        "maturity_date": None,
        "coupon_pct": None,
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
    log.info("Direxion Holdings Pipeline — %s", SNAPSHOT_DATE)
    log.info("=" * 60)

    frames = []
    for ticker, fund_name in DIREXION_FUNDS.items():
        try:
            raw_df = fetch_direxion_holdings(ticker)
            frames.append(parse_direxion_holdings(ticker, fund_name, raw_df))
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
