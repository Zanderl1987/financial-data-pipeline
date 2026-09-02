#!/usr/bin/env python3
"""
First Trust Holdings Pipeline:
  Fetches official daily/current fund holdings for First Trust ETFs from the
  server-rendered holdings page (ftportfolios.com / Retail/Etf/EtfHoldings.aspx).
  Writes to Iceberg table: constituents.fund_holdings with
  source='firsttrust:<TICKER>'

  Data source: keyless ASP.NET page, fully server-side rendered (no JS).
    GET https://www.ftportfolios.com/Retail/Etf/EtfHoldings.aspx?Ticker={TICKER}
  The holdings table is one of several <table>s on the page; it is the one
  whose header contains Security Name / Identifier / CUSIP / Classification /
  Shares / Quantity / Market Value / Weighting. Columns (verified live for FAN
  2026-09-01):
    Security Name | Identifier | CUSIP | Classification | Shares / Quantity |
    Market Value | Weighting
  Identifier is an exchange-suffixed ticker (VWS.DC, ORSTED.DC, ...). Rows with
  an empty CUSIP and an identifier like $USD/$CAD are spot currency balances --
  filtered, matching the other issuer pipelines. As-of date is in the page text
  ("Fund Holdings of the Fund as of 8/31/2026"); the fund name from the Title.

  CLI:
    python firsttrust_holdings_pipeline.py                    # default universe (FAN)
    python firsttrust_holdings_pipeline.py --tickers FAN      # explicit

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import io
import os
import re
import sys
import argparse
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = STORAGE_ROOT / "iceberg"
CATALOG_DB = ICEBERG_WAREHOUSE / "constituents_catalog.db"

HOLDINGS_URL = "https://www.ftportfolios.com/Retail/Etf/EtfHoldings.aspx?Ticker={ticker}"

# First Trust gap tickers
FIRSTTRUST_TICKERS = ["FAN"]

AS_OF_RE = re.compile(r"as of\s+(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
FUND_NAME_RE = re.compile(r"First Trust ([^(]+) \(", re.IGNORECASE)


def _money(s):
    if s is None:
        return None
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def fetch_firsttrust_holdings(ticker: str) -> pd.DataFrame:
    """Fetch the holdings table for a single First Trust ETF."""
    url = HOLDINGS_URL.format(ticker=ticker)
    log.info("[%s] Fetching from First Trust...", ticker)
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()

    html = r.text
    tables = pd.read_html(io.StringIO(html))
    df = None
    for t in tables:
        if len(t) == 0 or len(t.columns) < 3:
            continue
        # Aset of the page has no <thead>; pd.read_html puts the header text in
        # the first data row. Detect by that row's contents, then promote it.
        first_row = " ".join(str(c) for c in t.iloc[0].tolist()).lower()
        if "security name" in first_row and "weight" in first_row and "cusip" in first_row:
            t.columns = [str(c).strip() for c in t.iloc[0].tolist()]
            df = t.iloc[1:].reset_index(drop=True).copy()
            break
    if df is None:
        raise RuntimeError(f"Could not find holdings table on {url}")

    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns={
        "Security Name": "holding_name",
        "Identifier": "holding_ticker",
        "CUSIP": "cusip",
        "Classification": "sector",
        "Shares / Quantity": "shares_held",
        "Market Value": "market_value_usd",
        "Weighting": "weight_pct",
    })

    # Drop spot-currency / cash rows (empty CUSIP, identifier like $USD) and
    # any fully empty rows. CUSIP comes back from read_html as float NaN when
    # missing, so use isna() (a str-regex on astype(str) misses it).
    df = df[~df["holding_name"].astype(str).str.match(r"^\s*(nan|None)\s*$", na=False)]
    ident = df["holding_ticker"].astype(str).str.strip()
    empty_cusip = df["cusip"].isna() | df["cusip"].astype(str).str.match(r"^(nan|None)?\s*$", na=False)
    df = df[~(empty_cusip & ident.str.match(r"^\$", na=False))]

    # Parse numbers
    df["shares_held"] = pd.to_numeric(
        df["shares_held"].astype(str).str.replace(",", ""), errors="coerce")
    df["market_value_usd"] = df["market_value_usd"].map(_money)
    wt = df["weight_pct"].astype(str).str.replace("%", "").str.strip()
    df["weight_pct"] = pd.to_numeric(wt, errors="coerce")

    for col in ("holding_ticker", "holding_name", "cusip", "sector"):
        df[col] = df[col].map(lambda s: s.strip() if isinstance(s, str) else s)
        df[col] = df[col].replace({"nan": None, "None": None, "": None})
    # read_html may render a numeric CUSIP as e.g. "472204107.0"; normalize and
    # keep only well-formed codes.
    cusip_s = df["cusip"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    df["cusip"] = cusip_s.where(cusip_s.str.match(r"^[0-9A-Za-z]{6,}$", na=False))

    # Metadata from the page
    m = AS_OF_RE.search(html)
    snapshot_date = pd.to_datetime(m.group(1), format="%m/%d/%Y").date() if m else None
    m = FUND_NAME_RE.search(html)
    fund_name = f"First Trust {m.group(1).strip()}" if m else f"First Trust {ticker}"

    df["snapshot_date"] = snapshot_date
    df["fund_ticker"] = ticker
    df["fund_name"] = fund_name
    df["asset_category"] = "equity"
    df["country"] = None
    df["source"] = f"firsttrust:{ticker}"

    log.info("[%s] Parsed %d holdings (as of %s)", ticker, len(df), snapshot_date)
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
    parser = argparse.ArgumentParser(description="First Trust ETF holdings")
    parser.add_argument("--tickers", nargs="+", default=FIRSTTRUST_TICKERS,
                        help="First Trust tickers to fetch (default all)")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("First Trust Holdings Pipeline")
    log.info("=" * 60)

    frames = []
    for ticker in args.tickers:
        try:
            frames.append(fetch_firsttrust_holdings(ticker))
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