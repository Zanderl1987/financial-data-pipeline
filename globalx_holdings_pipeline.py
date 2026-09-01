#!/usr/bin/env python3
"""
Global X Holdings Pipeline:
  Fetches daily holdings CSV for Global X ETFs from their CDN.
  Writes to Iceberg table: constituents.fund_holdings with source='globalx:<TICKER>'

  Data source: Global X official daily holdings CSV (keyless, no auth)
  URL pattern: https://assets.globalxetfs.com/funds/holdings/{ticker}_full-holdings_{YYYYMMDD}.csv
  Requires Referer header. Rate limited - use delays between requests.

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
from datetime import date, datetime, timezone, timedelta
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
    ),
    "Referer": "https://www.globalxetfs.com/"
}

STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = STORAGE_ROOT / "iceberg"
CATALOG_DB = ICEBERG_WAREHOUSE / "constituents_catalog.db"

SNAPSHOT_DATE = date.today()
FETCHED_AT = datetime.now(timezone.utc)

# Global X gap tickers
GLOBALX_TICKERS = ["AIQ", "COPX", "DIV", "QYLD", "RYLD", "XYLD"]

# Non-security filter patterns
NON_SECURITY_NAME_RE = re.compile(
    r"Cash|Derivative|Emini|Futures|Option|Swap|Note\b|Net Other Assets",
    re.IGNORECASE
)

# Global X publishes monthly holdings at month-end. Use the last month-end date.
def get_latest_month_end() -> str:
    """Get the most recent month-end date in YYYYMMDD format."""
    today = date.today()
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month - timedelta(days=1)
    return last_month_end.strftime("%Y%m%d")


def fetch_globalx_holdings(ticker: str, date_str: str, max_retries: int = 3) -> pd.DataFrame | None:
    """Fetch holdings CSV for a single Global X ETF with retry logic."""
    url = f"https://assets.globalxetfs.com/funds/holdings/{ticker}_full-holdings_{date_str}.csv"
    log.info(f"[{ticker}] Fetching from Global X ({date_str})...")
    
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                # Skip first 2 rows (fund name + "Fund Holdings Data as of...")
                df = pd.read_csv(StringIO(r.text), skiprows=2)
                log.info(f"[{ticker}] Raw: {len(df)} rows, cols: {list(df.columns)}")
                return df
            elif r.status_code == 404:
                log.warning(f"[{ticker}] Not found (404) for date {date_str} (attempt {attempt+1}/{max_retries})")
            else:
                log.warning(f"[{ticker}] HTTP {r.status_code} (attempt {attempt+1}/{max_retries})")
        except Exception as e:
            log.warning(f"[{ticker}] Fetch error: {e} (attempt {attempt+1}/{max_retries})")
        
        if attempt < max_retries - 1:
            wait = 5 * (attempt + 1)  # 5s, 10s, 15s backoff
            log.info(f"[{ticker}] Retrying in {wait}s...")
            time.sleep(wait)
    
    log.error(f"[{ticker}] All retries exhausted")
    return None


def parse_globalx_holdings(ticker: str, raw_df: pd.DataFrame) -> pd.DataFrame:
    """Parse Global X CSV into our schema."""
    # Expected columns:
    # % of Net Assets, Ticker, Name, SEDOL, Market Price ($), Shares Held, Market Value ($)
    
    cols = [str(c).strip() for c in raw_df.columns]
    raw_df.columns = cols
    
    # Identify columns
    weight_col = next((c for c in cols if "net assets" in c.lower() or "%" in c), cols[0])
    ticker_col = next((c for c in cols if c.lower() == "ticker"), cols[1])
    name_col = next((c for c in cols if c.lower() == "name"), cols[2])
    sedol_col = next((c for c in cols if "sedol" in c.lower()), None)
    price_col = next((c for c in cols if "market price" in c.lower()), None)
    shares_col = next((c for c in cols if "shares" in c.lower() and "held" in c.lower()), None)
    mkt_val_col = next((c for c in cols if "market value" in c.lower()), None)
    
    # Filter non-securities
    before = len(raw_df)
    if name_col:
        raw_df = raw_df[~raw_df[name_col].astype(str).str.contains(
            NON_SECURITY_NAME_RE, case=False, na=False
        )]
    # Also filter rows where ticker is empty or cash-like
    if ticker_col:
        raw_df = raw_df[~raw_df[ticker_col].astype(str).str.strip().str.match(r"^--+$|^[A-Z]{0,2}\d{2,}$")]
    dropped = before - len(raw_df)
    if dropped > 0:
        log.info(f"[{ticker}] Filtered {dropped} non-security rows")
    
    # Build output
    def clean_str(s):
        if s is None:
            return None
        return s.astype(str).replace({"nan": None, "NaN": None, "": None}).str.strip()
    
    def clean_num(s):
        if s is None:
            return None
        return pd.to_numeric(s.astype(str).str.replace(r"[$,%]", "", regex=True).str.replace(",", ""), errors="coerce")
    
    result = pd.DataFrame({
        "snapshot_date": SNAPSHOT_DATE,
        "fund_ticker": ticker,
        "fund_name": f"Global X {ticker} ETF",
        "fund_cik": None,
        "holding_ticker": clean_str(raw_df[ticker_col]) if ticker_col else None,
        "holding_name": clean_str(raw_df[name_col]) if name_col else None,
        "cusip": None,
        "isin": None,
        "figi": None,
        "sedol": clean_str(raw_df[sedol_col]) if sedol_col else None,
        "weight_pct": clean_num(raw_df[weight_col]) if weight_col else None,
        "market_value_usd": clean_num(raw_df[mkt_val_col]) if mkt_val_col else None,
        "shares_held": clean_num(raw_df[shares_col]) if shares_col else None,
        "asset_category": None,
        "sector": None,
        "country": None,
        "issuer_name": clean_str(raw_df[name_col]) if name_col else None,
        "filing_date": None,
        "reporting_period_end": None,
        "source": f"globalx:{ticker}",
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
    log.info("Global X Holdings Pipeline — %s", SNAPSHOT_DATE)
    log.info("=" * 60)

    # Use latest month-end date (Global X publishes monthly)
    date_str = get_latest_month_end()
    log.info(f"Using holdings date: {date_str}")

    frames = []
    for i, ticker in enumerate(GLOBALX_TICKERS):
        try:
            raw_df = fetch_globalx_holdings(ticker, date_str)
            if raw_df is not None and not raw_df.empty:
                frames.append(parse_globalx_holdings(ticker, raw_df))
            else:
                log.warning(f"[{ticker}] No data for date {date_str}")
        except Exception as e:
            log.error(f"[{ticker}] FAILED: {e}")
        # Rate limiting: longer delay between requests to avoid 429s
        if i < len(GLOBALX_TICKERS) - 1:
            time.sleep(10)

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