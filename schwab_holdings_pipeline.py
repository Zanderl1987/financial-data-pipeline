#!/usr/bin/env python3
"""
Schwab ETF Holdings Pipeline:
  Fetches daily holdings CSV for Schwab Asset Management ETFs from their
  official site. Writes to Iceberg table: constituents.fund_holdings with
  source='schwab_etf:<TICKER>'

  Data source: Schwab Asset Management official daily holdings CSV, keyless.
  The CSV filename is date-stamped (e.g. SCHM_FundHoldings_2026-08-31.CSV) and
  not guessable -- each run scrapes the ticker's product page
  (schwabassetmanagement.com/products/{ticker}) for the current link rather
  than constructing a URL.

  schwabassetmanagement.com is Cloudflare-fronted and 403s plain `requests`
  (TLS/JA3 fingerprint, same class of block as royalnavy.mod.uk/JMIC) --
  confirmed live 2026-09-01 that curl_cffi's Chrome impersonation passes with
  no further friction (5 rapid sequential requests, zero rate-limiting).

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import os
import sys
import re
import time
import logging
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

STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = STORAGE_ROOT / "iceberg"
CATALOG_DB = ICEBERG_WAREHOUSE / "constituents_catalog.db"

SNAPSHOT_DATE = date.today()
FETCHED_AT = datetime.now(timezone.utc)

# Schwab gap tickers
SCHWAB_FUNDS = {
    "SCHM": "Schwab U.S. Mid-Cap ETF",
    "SCHY": "Schwab International Dividend Equity ETF",
}

PRODUCT_PAGE_URL = "https://www.schwabassetmanagement.com/products/{ticker_lower}"
BASE_URL = "https://www.schwabassetmanagement.com"

_HOLDINGS_LINK_RE = re.compile(
    r'href="(/sites/[^"]*?FundHoldings[^"]*?\.CSV)"', re.IGNORECASE
)


def find_holdings_csv_url(ticker: str) -> str:
    """Scrape the product page for today's dated holdings CSV link."""
    from curl_cffi import requests as creq

    url = PRODUCT_PAGE_URL.format(ticker_lower=ticker.lower())
    r = creq.get(url, impersonate="chrome124", timeout=60)
    r.raise_for_status()

    m = _HOLDINGS_LINK_RE.search(r.text)
    if not m:
        raise RuntimeError(f"No FundHoldings CSV link found on {url}")
    return BASE_URL + m.group(1)


def fetch_schwab_holdings(ticker: str) -> pd.DataFrame:
    """Fetch holdings CSV for a single Schwab ETF."""
    from curl_cffi import requests as creq

    csv_url = find_holdings_csv_url(ticker)
    log.info(f"[{ticker}] Fetching {csv_url}...")

    r = creq.get(csv_url, impersonate="chrome124", timeout=60)
    r.raise_for_status()

    df = pd.read_csv(StringIO(r.text))
    log.info(f"[{ticker}] Raw: {len(df)} rows, cols: {list(df.columns)}")
    return df


def parse_schwab_holdings(ticker: str, fund_name: str, raw_df: pd.DataFrame) -> pd.DataFrame:
    """Parse Schwab CSV into our schema."""
    df = raw_df[raw_df["Symbol"].notna()].copy()
    dropped = len(raw_df) - len(df)
    if dropped > 0:
        log.info(f"[{ticker}] Filtered {dropped} rows with no symbol (cash/other)")

    as_of = pd.to_datetime(df["As-Of-Date"], errors="coerce")

    result = pd.DataFrame({
        "snapshot_date": SNAPSHOT_DATE,
        "fund_ticker": ticker,
        "fund_name": fund_name,
        "fund_cik": None,
        "holding_ticker": df["Symbol"].astype(str).str.strip(),
        "holding_name": df["Name"].astype(str).str.strip(),
        "cusip": None,
        "isin": None,
        "figi": df.get("BBG FIGI"),
        "sedol": None,
        "weight_pct": pd.to_numeric(df["Percent of Assets"], errors="coerce"),
        "market_value_usd": None,
        "shares_held": pd.to_numeric(df["Quantity"], errors="coerce"),
        "asset_category": "Common Stock",
        "sector": df.get("Sector"),
        "country": df.get("Country"),
        "issuer_name": df["Name"].astype(str).str.strip(),
        "filing_date": None,
        "reporting_period_end": as_of.dt.date,
        "source": f"schwab_etf:{ticker}",
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
    log.info("Schwab ETF Holdings Pipeline — %s", SNAPSHOT_DATE)
    log.info("=" * 60)

    frames = []
    for ticker, fund_name in SCHWAB_FUNDS.items():
        try:
            raw_df = fetch_schwab_holdings(ticker)
            frames.append(parse_schwab_holdings(ticker, fund_name, raw_df))
        except Exception as e:
            log.error(f"[{ticker}] FAILED: {e}")
        time.sleep(0.5)  # Be polite

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
