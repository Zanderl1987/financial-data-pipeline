#!/usr/bin/env python3
"""
Fund Holdings Pipeline:
  Fetches ETF holdings (BlackRock varnish API) and mutual fund holdings (EdgarTools N-PORT).
  Writes to Iceberg table: constituents.fund_holdings

  Data sources:
    1. BlackRock varnish API — iShares ETF holdings (XML Spreadsheet)
    2. EdgarTools — SEC N-PORT filings for mutual funds

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/

  NOTE: Overwrites per-fund_ticker partitions for idempotent re-runs.
"""

import os
import re
import sys
import time
import logging
import argparse
import requests
import pandas as pd
from xml.etree import ElementTree as ET
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
    "FinancialDataPipeline research@financial-data-pipeline.com"
)

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

# Rate limits
BLACKROCK_SLEEP = 0.5  # seconds between BlackRock API calls
EDGAR_SLEEP = 0.15     # seconds between EdgarTools calls (~6 req/sec, conservative)

# Non-security filter patterns (same as index_constituents_pipeline)
NON_SECURITY_TICKER_RE = re.compile(r"^--+$|^[A-Z]{0,2}\d{2,}$")
NON_SECURITY_NAME_RE = re.compile(
    r"Cash|Derivative|Emini|Futures|Option|Swap|Note\b", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# ETF Holdings — BlackRock varnish API
# ---------------------------------------------------------------------------
ETF_PID_MAP = {
    # US Equity — Broad Market
    "IVV":  {"pid": "239726", "name": "iShares Core S&P 500 ETF"},
    "ITOT": {"pid": "239724", "name": "iShares Core S&P Total US Stock Market ETF"},
    "IWV":  {"pid": "239714", "name": "iShares Russell 3000 ETF"},
    "IWB":  {"pid": "239707", "name": "iShares Russell 1000 ETF"},
    # US Equity — Style
    "IWF":  {"pid": "239706", "name": "iShares Russell 1000 Growth ETF"},
    "IWD":  {"pid": "239708", "name": "iShares Russell 1000 Value ETF"},
    # US Equity — Mid/Small Cap
    "IJH":  {"pid": "239763", "name": "iShares Core S&P Mid-Cap ETF"},
    "IJR":  {"pid": "239774", "name": "iShares Core S&P Small-Cap ETF"},
    "IWR":  {"pid": "239718", "name": "iShares Russell Mid-Cap ETF"},
    # US Equity — Small Cap Style
    "IWM":  {"pid": "239710", "name": "iShares Russell 2000 ETF"},
    "IWO":  {"pid": "239709", "name": "iShares Russell 2000 Growth ETF"},
    "IWN":  {"pid": "239712", "name": "iShares Russell 2000 Value ETF"},
    # International
    "IEFA": {"pid": "244049", "name": "iShares Core MSCI EAFE ETF"},
    "IEMG": {"pid": "244050", "name": "iShares Core MSCI Emerging Markets ETF"},
    "EFA":  {"pid": "239623", "name": "iShares MSCI EAFE ETF"},
    "EEM":  {"pid": "239637", "name": "iShares MSCI Emerging Markets ETF"},
    "ACWI": {"pid": "239600", "name": "iShares MSCI ACWI ETF"},
    # Fixed Income — REMOVED: bond ETFs have different XML column structure
    # AGG, LQD, HYG, TIP need a separate parser (different header row format)
    # "AGG":  {"pid": "239458", "name": "iShares Core U.S. Aggregate Bond ETF"},
    # "LQD":  {"pid": "239566", "name": "iShares iBoxx $ Investment Grade Corporate Bond ETF"},
    # "HYG":  {"pid": "239565", "name": "iShares iBoxx $ High Yield Corporate Bond ETF"},
    # "TIP":  {"pid": "239467", "name": "iShares TIPS Bond ETF"},
}

# Mutual funds to fetch via EdgarTools N-PORT
MUTUAL_FUND_UNIVERSE = {
    "VFIAX": "Vanguard 500 Index Fund",
    "VTSAX": "Vanguard Total Stock Market Index Fund",
    "VTIAX": "Vanguard Total International Stock Index Fund",
    "VBTLX": "Vanguard Total Bond Market Index Fund",
    "VGSLX": "Vanguard Real Estate Index Fund",
    "FXAIX": "Fidelity 500 Index Fund",
    "FSKAX": "Fidelity Total Market Index Fund",
    "FTIHX": "Fidelity Total International Index Fund",
    "FBALX": "Fidelity Blue Chip Growth Fund",
    "VWUSX": "Vanguard U.S. Growth Fund",
}


def fetch_blackrock_etf_holdings(ticker: str) -> pd.DataFrame:
    """Fetch ETF holdings from BlackRock varnish API."""
    info = ETF_PID_MAP[ticker]
    pid = info["pid"]
    fund_name = info["name"]
    log.info("[ETF:%s] Fetching from BlackRock (pid=%s)...", ticker, pid)

    api_url = (
        f"https://www.blackrock.com/varnish-api/blk-one01-product-data/"
        f"product-data/api/v1/get-fund-document"
        f"?appType=PRODUCT_PAGE&appSubType=ONE&targetSite=one&locale=en_US"
        f"&portfolioId={pid}&component=fundDownload&userType=individual"
    )
    r = requests.get(api_url, headers=HEADERS, timeout=60)
    r.raise_for_status()

    raw = r.content.decode("utf-8", errors="replace")
    fixed = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", "&amp;", raw)

    root = ET.fromstring(fixed.encode("utf-8"))
    ns = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}

    # Find Holdings worksheet
    holdings_ws = None
    for ws in root.findall(f"{{{ns['ss']}}}Worksheet"):
        if ws.get(f"{{{ns['ss']}}}Name") == "Holdings":
            holdings_ws = ws
            break
    if holdings_ws is None:
        raise RuntimeError(f"No Holdings worksheet found for {ticker}")

    # Parse rows
    rows_data = []
    for row in holdings_ws.iter(f"{{{ns['ss']}}}Row"):
        cells = []
        for cell in row.findall(f"{{{ns['ss']}}}Cell"):
            data_elem = cell.find(f"{{{ns['ss']}}}Data")
            cells.append(data_elem.text if data_elem is not None else None)
        if any(c for c in cells):
            rows_data.append(cells)

    # Find header row
    header_idx = None
    for i, row in enumerate(rows_data):
        row_text = " ".join(str(c).lower() for c in row if c)
        if "ticker" in row_text and ("weight" in row_text or "sector" in row_text):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError(f"Could not find header row for {ticker}")

    headers_row = [str(c).strip() for c in rows_data[header_idx]]
    data_rows = rows_data[header_idx + 1:]
    df = pd.DataFrame(data_rows, columns=headers_row[: len(headers_row)])

    # Filter non-securities
    ticker_col = next((c for c in df.columns if c.lower() == "ticker"), df.columns[0])
    name_col = next((c for c in df.columns if "name" in c.lower()), None)
    weight_col = next((c for c in df.columns if "weight" in c.lower()), None)
    asset_class_col = next((c for c in df.columns if "asset class" in c.lower()), None)
    sector_col = next((c for c in df.columns if "sector" in c.lower()), None)

    before = len(df)
    df = df[~df[ticker_col].astype(str).str.strip().str.match(NON_SECURITY_TICKER_RE)]
    if asset_class_col:
        df = df[~df[asset_class_col].astype(str).str.contains(
            "cash|Cash|derivative|Derivative", case=False, na=False
        )]
    if name_col:
        df = df[~df[name_col].astype(str).str.contains(
            "Cash|Derivative|Emini|Futures|Option|Swap", case=False, na=False
        )]
    dropped = before - len(df)
    if dropped > 0:
        log.info("[ETF:%s] Filtered %d non-security rows", ticker, dropped)

    # Build output
    result = pd.DataFrame({
        "snapshot_date": SNAPSHOT_DATE,
        "fund_ticker": ticker,
        "fund_name": fund_name,
        "fund_cik": None,
        "holding_ticker": df[ticker_col].astype(str).str.strip(),
        "holding_name": df[name_col].str.strip() if name_col else None,
        "cusip": None,
        "isin": None,
        "figi": None,
        "sedol": None,
        "weight_pct": pd.to_numeric(df[weight_col], errors="coerce") if weight_col else None,
        "market_value_usd": None,
        "shares_held": None,
        "asset_category": df[asset_class_col].str.strip() if asset_class_col else None,
        "sector": df[sector_col].str.strip() if sector_col else None,
        "country": None,
        "issuer_name": df[name_col].str.strip() if name_col else None,
        "filing_date": None,
        "reporting_period_end": None,
        "source": f"blackrock:{ticker}",
        "fetched_at": FETCHED_AT,
    })
    log.info("[ETF:%s] Output: %d holdings", ticker, len(result))
    return result


def fetch_all_etf_holdings(only_tickers: list[str] | None = None) -> list[pd.DataFrame]:
    """Fetch holdings for ETFs in ETF_PID_MAP."""
    targets = only_tickers if only_tickers else list(ETF_PID_MAP.keys())
    frames = []
    for ticker in targets:
        if ticker not in ETF_PID_MAP:
            log.warning("[ETF:%s] Not in ETF_PID_MAP — skipping", ticker)
            continue
        try:
            frames.append(fetch_blackrock_etf_holdings(ticker))
        except Exception as e:
            log.error("[ETF:%s] FAILED: %s", ticker, e)
        time.sleep(BLACKROCK_SLEEP)
    return frames


# ---------------------------------------------------------------------------
# Mutual Fund Holdings — EdgarTools N-PORT
# ---------------------------------------------------------------------------
def fetch_mutual_fund_holdings(ticker: str) -> pd.DataFrame:
    """Fetch mutual fund holdings from SEC N-PORT via EdgarTools."""
    from edgar import Fund, set_identity

    set_identity(EDGAR_USER_AGENT)
    log.info("[MF:%s] Fetching N-PORT holdings...", ticker)

    fund = Fund(ticker)
    report = fund.get_latest_report()

    if report is None:
        raise RuntimeError(f"No N-PORT filing found for {ticker}")

    df = report.securities_data()
    if df is None or df.empty:
        raise RuntimeError(f"Empty portfolio for {ticker}")

    # Normalize column names to match our schema
    col_map = {
        "name": "holding_name",
        "ticker": "holding_ticker",
        "cusip": "cusip",
        "value_usd": "market_value_usd",
        "pct_value": "weight_pct",
        "balance": "shares_held",
        "asset_category": "asset_category",
        "investment_country": "country",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    # Parse numeric columns
    for col in ["market_value_usd", "weight_pct", "shares_held"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Build output
    result = pd.DataFrame({
        "snapshot_date": SNAPSHOT_DATE,
        "fund_ticker": ticker,
        "fund_name": report.name or ticker,
        "fund_cik": int(report.cik) if report.cik else None,
        "holding_ticker": df.get("holding_ticker"),
        "holding_name": df.get("holding_name"),
        "cusip": df.get("cusip"),
        "isin": None,
        "figi": None,
        "sedol": None,
        "weight_pct": df.get("weight_pct"),
        "market_value_usd": df.get("market_value_usd"),
        "shares_held": df.get("shares_held"),
        "asset_category": df.get("asset_category"),
        "sector": None,
        "country": df.get("country"),
        "issuer_name": df.get("holding_name"),
        "filing_date": None,
        "reporting_period_end": pd.to_datetime(report.reporting_period, errors="coerce")
            if hasattr(report, "reporting_period") else None,
        "source": f"edgar_nport:{ticker}",
        "fetched_at": FETCHED_AT,
    })

    # Drop rows with no holding_ticker (cash, derivatives, etc.)
    result = result[result["holding_ticker"].notna() & (result["holding_ticker"] != "")]
    log.info("[MF:%s] Output: %d holdings (period: %s)", ticker, len(result),
             getattr(report, "reporting_period", "unknown"))
    return result


def fetch_all_mutual_fund_holdings(only_tickers: list[str] | None = None) -> list[pd.DataFrame]:
    """Fetch holdings for mutual funds in MUTUAL_FUND_UNIVERSE."""
    targets = only_tickers if only_tickers else list(MUTUAL_FUND_UNIVERSE.keys())
    frames = []
    for ticker in targets:
        if ticker not in MUTUAL_FUND_UNIVERSE:
            log.warning("[MF:%s] Not in MUTUAL_FUND_UNIVERSE — skipping", ticker)
            continue
        try:
            frames.append(fetch_mutual_fund_holdings(ticker))
        except Exception as e:
            log.error("[MF:%s] FAILED: %s", ticker, e)
        time.sleep(EDGAR_SLEEP)
    return frames


# ---------------------------------------------------------------------------
# Write to Iceberg (per-fund_ticker overwrite)
# ---------------------------------------------------------------------------
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

    catalog = load_catalog(
        "constituents",
        type="sql",
        uri=f"sqlite:///{CATALOG_DB.as_posix()}",
        warehouse=f"file://{ICEBERG_WAREHOUSE.as_posix()}",
    )
    table = catalog.load_table("constituents.fund_holdings")

    # Prepare DataFrame
    df = combined.copy()
    for col in ["snapshot_date", "filing_date", "reporting_period_end"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    df["fetched_at"] = pd.Timestamp.now(tz="UTC")

    # Arrow schema matching the Iceberg table
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
    ])

    col_order = [f.name for f in arrow_schema]
    for col in col_order:
        if col not in df.columns:
            df[col] = None
    df = df[col_order]

    arrow_table = pa.Table.from_pandas(df, schema=arrow_schema, preserve_index=False)

    # Overwrite per fund_ticker — replace today's data for each fund
    total_written = 0
    for fund_ticker in sorted(df["fund_ticker"].unique()):
        fund_df = arrow_table.filter(pa.compute.equal(arrow_table.column("fund_ticker"), fund_ticker))
        table.overwrite(fund_df, overwrite_filter=EqualTo("fund_ticker", fund_ticker))
        total_written += len(fund_df)
        log.info("[Iceberg]   %s: %d rows written", fund_ticker, len(fund_df))

    log.info("[Iceberg] Total written: %d rows across %d funds",
             total_written, df["fund_ticker"].nunique())

    # Verify
    import duckdb
    result = duckdb.sql(
        f"SELECT count(*) FROM read_parquet("
        f"'{ICEBERG_WAREHOUSE.as_posix()}/constituents/fund_holdings/**/*.parquet', "
        f"hive_partitioning=true)"
    ).fetchone()
    log.info("[Iceberg] Total rows in fund_holdings: %d", result[0])
    return result[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(
    skip_etf: bool = False,
    skip_mf: bool = False,
    etf_tickers: list[str] | None = None,
    mf_tickers: list[str] | None = None,
):
    log.info("=" * 60)
    log.info("Fund Holdings Pipeline — %s", SNAPSHOT_DATE)
    log.info("=" * 60)

    frames = []

    # ETF holdings via BlackRock
    if not skip_etf:
        log.info("--- ETF Holdings (BlackRock varnish API) ---")
        frames.extend(fetch_all_etf_holdings(only_tickers=etf_tickers))
    else:
        log.info("--- Skipping ETF holdings ---")

    # Mutual fund holdings via EdgarTools
    if not skip_mf:
        log.info("--- Mutual Fund Holdings (EdgarTools N-PORT) ---")
        frames.extend(fetch_all_mutual_fund_holdings(only_tickers=mf_tickers))
    else:
        log.info("--- Skipping mutual fund holdings ---")

    # Summary
    if frames:
        combined = pd.concat(frames, ignore_index=True)
        log.info("-" * 60)
        log.info("TOTAL: %d rows across %d funds", len(combined), combined["fund_ticker"].nunique())
        for ft, count in combined.groupby("fund_ticker").size().items():
            log.info("  %-10s %d holdings", ft, count)
        log.info("-" * 60)
    else:
        log.warning("No data fetched.")

    # Write to Iceberg
    write_to_iceberg(frames)

    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fund Holdings Pipeline — ETF (BlackRock) + Mutual Fund (EdgarTools N-PORT)"
    )
    parser.add_argument(
        "--skip-etf",
        action="store_true",
        help="Skip ETF holdings (BlackRock varnish API).",
    )
    parser.add_argument(
        "--skip-mf",
        action="store_true",
        help="Skip mutual fund holdings (EdgarTools N-PORT).",
    )
    parser.add_argument(
        "--etf-tickers",
        nargs="+",
        default=None,
        help="Specific ETF tickers to fetch (default: all in ETF_PID_MAP).",
    )
    parser.add_argument(
        "--mf-tickers",
        nargs="+",
        default=None,
        help="Specific mutual fund tickers to fetch (default: all in MUTUAL_FUND_UNIVERSE).",
    )
    args = parser.parse_args()
    main(
        skip_etf=args.skip_etf,
        skip_mf=args.skip_mf,
        etf_tickers=args.etf_tickers,
        mf_tickers=args.mf_tickers,
    )
