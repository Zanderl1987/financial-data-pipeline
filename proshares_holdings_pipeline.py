#!/usr/bin/env python3
"""
ProShares Holdings Pipeline:
  Fetches daily holdings for all ProShares ETFs from the master CSV at
  https://accounts.profunds.com/etfdata/psdlyhld.csv
  Writes to Iceberg table: constituents.fund_holdings with source='proshares:<TICKER>'

  Data source: ProShares official daily holdings file (keyless, no auth)
  Updated daily by ProShares, contains all ProShares ETF holdings including
  inverse/leveraged funds that use swaps/futures (Security Ticker often NaN,
  but Security Description identifies the underlying).

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import os
import sys
import re
import time
import logging
import argparse
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

# ProShares master holdings URL
PROSHARES_HOLDINGS_URL = "https://accounts.profunds.com/etfdata/psdlyhld.csv"

# Non-security filter patterns
NON_SECURITY_NAME_RE = re.compile(
    r"Cash|Derivative|Emini|Futures|Option|Swap|Note\b|Net Other Assets",
    re.IGNORECASE
)

# Only process our gap tickers - map full fund name to ticker
GAP_TICKER_MAP = {
    "ProShares Short Dow30": "DOG",
    "ProShares Short QQQ": "PSQ",
    "ProShares Short S&P500": "SH",
    "ProShares Short VIX Short-Term Futures ETF": "SVXY",
}


def fetch_proshares_master() -> pd.DataFrame:
    """Fetch the master ProShares daily holdings CSV."""
    log.info("Fetching ProShares master holdings...")
    r = requests.get(PROSHARES_HOLDINGS_URL, headers=HEADERS, timeout=60)
    r.raise_for_status()

    # Skip first 3 rows (title, date, blank), header is row 4 (0-indexed: 3)
    df = pd.read_csv(StringIO(r.text), skiprows=3)
    log.info(f"ProShares master: {len(df)} rows, {df['Fund Ticker'].nunique()} funds")
    return df


def parse_proshares_holdings(master_df: pd.DataFrame) -> list[pd.DataFrame]:
    """Parse master DataFrame into per-fund DataFrames matching our schema."""
    # Clean column names
    master_df.columns = [c.strip() for c in master_df.columns]

    # Identify columns
    fund_col = "Fund Ticker"
    sec_ticker_col = next((c for c in master_df.columns if "Security Ticker" in c), None)
    sec_name_col = next((c for c in master_df.columns if "Security Description" in c), None)
    sec_sedol_col = next((c for c in master_df.columns if "Sedol" in c), None)
    coupon_col = next((c for c in master_df.columns if "Coupon" in c), None)
    maturity_col = next((c for c in master_df.columns if "Maturity" in c), None)
    shares_col = next((c for c in master_df.columns if "Shares" in c or "Contracts" in c), None)
    exposure_col = next((c for c in master_df.columns if "Exposure" in c), None)
    mkt_val_col = next((c for c in master_df.columns if "Market Value" in c), None)

    frames = []
    for fund_name in sorted(master_df[fund_col].unique()):
        # Only process our gap tickers
        if fund_name.strip() not in GAP_TICKER_MAP:
            continue
        fund_ticker = GAP_TICKER_MAP[fund_name.strip()]
        fund_df = master_df[master_df[fund_col] == fund_name].copy()
        
        # Filter non-securities
        if sec_name_col:
            before = len(fund_df)
            fund_df = fund_df[~fund_df[sec_name_col].astype(str).str.contains(
                NON_SECURITY_NAME_RE, na=False
            )]
            dropped = before - len(fund_df)
            if dropped > 0:
                log.info(f"  [{fund_name}] Filtered {dropped} non-security rows")

        if fund_df.empty:
            continue

        # Build output - handle NaN columns safely
        def safe_str_strip(series):
            if series is None:
                return None
            return series.astype(str).replace({"nan": None, "NaN": None, "": None}).str.strip()

        result = pd.DataFrame({
            "snapshot_date": SNAPSHOT_DATE,
            "fund_ticker": fund_ticker,
            "fund_name": fund_name.strip(),
            "fund_cik": None,
            "holding_ticker": safe_str_strip(fund_df[sec_ticker_col]) if sec_ticker_col else None,
            "holding_name": safe_str_strip(fund_df[sec_name_col]) if sec_name_col else None,
            "cusip": None,
            "isin": None,
            "figi": None,
            "sedol": safe_str_strip(fund_df[sec_sedol_col]) if sec_sedol_col else None,
            "weight_pct": None,  # Not directly provided
            "market_value_usd": pd.to_numeric(fund_df[mkt_val_col].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce") if mkt_val_col else None,
            "shares_held": pd.to_numeric(fund_df[shares_col].astype(str).str.replace(",", ""), errors="coerce") if shares_col else None,
            "asset_category": None,
            "sector": None,
            "country": None,
            "issuer_name": safe_str_strip(fund_df[sec_name_col]) if sec_name_col else None,
            "filing_date": None,
            "reporting_period_end": None,
            "source": f"proshares:{fund_ticker}",
            "fetched_at": FETCHED_AT,
            "par_value": None,
            "maturity_date": pd.to_datetime(fund_df[maturity_col], errors="coerce").dt.date if maturity_col else None,
            "coupon_pct": pd.to_numeric(fund_df[coupon_col], errors="coerce") if coupon_col else None,
            "duration": None,
            "ytm_pct": None,
        })

        # Clean holding_ticker: replace 'nan' strings with None
        if "holding_ticker" in result.columns:
            result["holding_ticker"] = result["holding_ticker"].replace({"nan": None, "": None})

        log.info(f"  [{fund_ticker}] Output: {len(result)} holdings")
        frames.append(result)

    return frames


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
    log.info("ProShares Holdings Pipeline — %s", SNAPSHOT_DATE)
    log.info("=" * 60)

    master_df = fetch_proshares_master()
    frames = parse_proshares_holdings(master_df)

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