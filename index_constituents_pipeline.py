#!/usr/bin/env python3
"""
Index Constituents Pipeline:
  Ingests index constituents for S&P 500, Nasdaq-100, Russell 3000/2000, Wilshire 5000.
  Writes to Iceberg table: constituents.index_members

  Data sources:
    1. Wikipedia  — S&P 500 (503 constituents with CIK, GICS sector, date added)
    2. stockanalysis.com — Nasdaq-100 (103 tickers)
    3. BlackRock varnish API — Russell 3000 (IWV), Russell 2000 (IWM), Wilshire 5000 (ITOT proxy)

  Storage: Apache Iceberg with Parquet/Snappy compression.
  Catalog:  storage/iceberg/constituents_catalog.db (SQLite-backed)
  Warehouse: storage/iceberg/constituents/

  NOTE: This pipeline uses PyIceberg table.overwrite() (not write_partitioned)
        to ensure idempotent snapshot replacement — no duplicate rows across runs.
"""

import os
import re
import sys
import logging
import argparse
import requests
import pandas as pd
from io import StringIO
from xml.etree import ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

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
CSV_BACKUP = STORAGE_ROOT / "raw" / "index_members_latest.csv"


# ---------------------------------------------------------------------------
# 1. S&P 500 from Wikipedia
# ---------------------------------------------------------------------------
def fetch_sp500_wikipedia() -> pd.DataFrame:
    log.info("[SP500] Fetching from Wikipedia...")
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    tables = pd.read_html(StringIO(r.text))
    df = tables[0]
    log.info("[SP500] Raw shape: %s, cols: %s", df.shape, list(df.columns))

    result = pd.DataFrame({
        "snapshot_date": date.today(),
        "index_code": "SPX",
        "index_name": "S&P 500",
        "ticker": df["Symbol"].str.replace(".", "-", regex=False),
        "company_name": df["Security"],
        "cusip": None,
        "isin": None,
        "figi": None,
        "cik": pd.to_numeric(df["CIK"], errors="coerce").astype("Int64"),
        "gics_sector": df["GICS Sector"],
        "gics_sub_industry": df.get("GICS Sub-Industry"),
        "weight_pct": None,
        "shares_outstanding": None,
        "market_cap": None,
        "date_added": pd.to_datetime(df["Date added"], errors="coerce"),
        "date_removed": None,
        "source": "wikipedia",
        "fetched_at": datetime.now(timezone.utc),
    })
    log.info("[SP500] Output: %d constituents", len(result))
    return result


# ---------------------------------------------------------------------------
# 2. Nasdaq-100 from stockanalysis.com
# ---------------------------------------------------------------------------
def fetch_nasdaq100() -> pd.DataFrame:
    log.info("[NDX] Fetching from stockanalysis.com...")
    url = "https://stockanalysis.com/list/nasdaq-100-stocks/"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()

    # Strip all SvelteKit hydration comment markers before parsing
    html = re.sub(r'<!----?>|<!--.*?-->', '', r.text)

    # Extract ticker + company name from the pre-rendered HTML table.
    # Pattern: <td class="sym ..."><a href="/stocks/SLUG/">TICKER</a></td>
    #          <td class="slw ...">Company Name</td>
    rows = re.findall(
        r'<td[^>]*class="sym[^"]*"[^>]*>\s*<a[^>]*href="/stocks/[^"]+/"[^>]*>([A-Z.]+)</a>'
        r'\s*</td>\s*<td[^>]*class="slw[^"]*"[^>]*>([^<]+)</td>',
        html,
    )

    if not rows:
        # Fallback: extract tickers only from anchor tags (no company names)
        tickers = re.findall(
            r'<a[^>]*href="/stocks/([^/]+)/"[^>]*>([A-Z.]+)</a>', r.text
        )
        if not tickers:
            raise RuntimeError("Could not scrape Nasdaq-100 data from stockanalysis.com")
        seen = set()
        rows = []
        for slug, ticker in tickers:
            if ticker not in seen:
                seen.add(ticker)
                rows.append((ticker, ""))
        log.info("[NDX] Fallback: extracted %d tickers from HTML anchors", len(rows))

    seen = set()
    unique = []
    for ticker, name in rows:
        ticker = ticker.strip()
        name = name.strip()
        if ticker and ticker not in seen:
            seen.add(ticker)
            unique.append((ticker, name or None))

    log.info("[NDX] Found %d unique tickers", len(unique))

    result = pd.DataFrame({
        "snapshot_date": date.today(),
        "index_code": "NDX",
        "index_name": "Nasdaq-100",
        "ticker": [t for t, _ in unique],
        "company_name": [n for _, n in unique],
        "cusip": None,
        "isin": None,
        "figi": None,
        "cik": None,
        "gics_sector": None,
        "gics_sub_industry": None,
        "weight_pct": None,
        "shares_outstanding": None,
        "market_cap": None,
        "date_added": None,
        "date_removed": None,
        "source": "stockanalysis.com",
        "fetched_at": datetime.now(timezone.utc),
    })
    log.info("[NDX] Output: %d constituents", len(result))
    return result


# ---------------------------------------------------------------------------
# 3-5. BlackRock/iShares XML Spreadsheet via varnish API
# ---------------------------------------------------------------------------
BLACKROCK_PIDS = {
    "IWV":  {"pid": "239714", "index_code": "RUT3000", "index_name": "Russell 3000"},
    "IWM":  {"pid": "239710", "index_code": "RUT2000", "index_name": "Russell 2000"},
    "ITOT": {"pid": "239724", "index_code": "W5000",   "index_name": "Wilshire 5000 (via ITOT)"},
}

# Patterns that indicate non-security rows to filter out
NON_SECURITY_TICKER_RE = re.compile(r"^--+$|^[A-Z]{0,2}\d{2,}$")
NON_SECURITY_NAME_RE = re.compile(
    r"Cash|Derivative|Emini|Futures|Option|Swap|Note\b", re.IGNORECASE
)


def fetch_blackrock_holdings(ticker: str) -> pd.DataFrame:
    """Fetch ETF holdings from BlackRock varnish API (XML Spreadsheet format)."""
    info = BLACKROCK_PIDS[ticker]
    pid = info["pid"]
    index_code = info["index_code"]
    index_name = info["index_name"]
    log.info("[%s] Fetching from BlackRock varnish API (pid=%s)...", ticker, pid)

    api_url = (
        f"https://www.blackrock.com/varnish-api/blk-one01-product-data/"
        f"product-data/api/v1/get-fund-document"
        f"?appType=PRODUCT_PAGE&appSubType=ONE&targetSite=one&locale=en_US"
        f"&portfolioId={pid}&component=fundDownload&userType=individual"
    )
    r = requests.get(api_url, headers=HEADERS, timeout=60)
    r.raise_for_status()

    # Decode and fix unescaped & in URLs
    raw = r.content.decode("utf-8", errors="replace")
    fixed = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", "&amp;", raw)

    root = ET.fromstring(fixed.encode("utf-8"))
    ns = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}

    # Find the Holdings worksheet
    holdings_ws = None
    for ws in root.findall(f"{{{ns['ss']}}}Worksheet"):
        if ws.get(f"{{{ns['ss']}}}Name") == "Holdings":
            holdings_ws = ws
            break
    if holdings_ws is None:
        raise RuntimeError(f"No Holdings worksheet found for {ticker}")

    # Parse all rows
    rows_data = []
    for row in holdings_ws.iter(f"{{{ns['ss']}}}Row"):
        cells = []
        for cell in row.findall(f"{{{ns['ss']}}}Cell"):
            data_elem = cell.find(f"{{{ns['ss']}}}Data")
            cells.append(data_elem.text if data_elem is not None else None)
        if any(c for c in cells):
            rows_data.append(cells)

    log.info("[%s] Parsed %d XML rows", ticker, len(rows_data))

    # Find header row (contains "Ticker" and "Weight")
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
    log.info("[%s] Holdings DataFrame: %s", ticker, df.shape)

    # Map to standard columns before filtering
    ticker_col = next((c for c in df.columns if c.lower() == "ticker"), df.columns[0])
    name_col = next((c for c in df.columns if "name" in c.lower()), None)
    weight_col = next((c for c in df.columns if "weight" in c.lower()), None)
    asset_class_col = next((c for c in df.columns if "asset class" in c.lower()), None)

    # --- Filter out non-security rows ---
    before = len(df)

    # Drop rows where ticker matches non-security patterns (e.g. "--", codes)
    df = df[~df[ticker_col].astype(str).str.strip().str.match(NON_SECURITY_TICKER_RE)]

    # Drop rows where name or asset class indicates cash / derivatives
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
        log.info("[%s] Filtered out %d non-security rows", ticker, dropped)

    result = pd.DataFrame({
        "snapshot_date": date.today(),
        "index_code": index_code,
        "index_name": index_name,
        "ticker": df[ticker_col].astype(str).str.strip(),
        "company_name": df[name_col].str.strip() if name_col else None,
        "cusip": None,
        "isin": None,
        "figi": None,
        "cik": None,
        "gics_sector": None,
        "gics_sub_industry": None,
        "weight_pct": pd.to_numeric(df[weight_col], errors="coerce") if weight_col else None,
        "shares_outstanding": None,
        "market_cap": None,
        "date_added": None,
        "date_removed": None,
        "source": f"blackrock:{ticker}",
        "fetched_at": datetime.now(timezone.utc),
    })
    log.info("[%s] Output: %d constituents", ticker, len(result))
    return result


# ---------------------------------------------------------------------------
# Write to Iceberg (idempotent overwrite for today's snapshot)
# ---------------------------------------------------------------------------
ICEBERG_SCHEMA_FIELDS = [
    ("snapshot_date", "date32", False),
    ("index_code", "string", False),
    ("index_name", "string", True),
    ("ticker", "string", False),
    ("company_name", "string", True),
    ("cusip", "string", True),
    ("isin", "string", True),
    ("figi", "string", True),
    ("cik", "int64", True),
    ("gics_sector", "string", True),
    ("gics_sub_industry", "string", True),
    ("weight_pct", "float64", True),
    ("shares_outstanding", "int64", True),
    ("market_cap", "float64", True),
    ("date_added", "date32", True),
    ("date_removed", "date32", True),
    ("source", "string", False),
    ("fetched_at", "timestamp[us, tz=UTC]", False),
]


def _build_arrow_schema():
    import pyarrow as pa
    arrow_types = {
        "date32":                 pa.date32(),
        "string":                 pa.string(),
        "int64":                  pa.int64(),
        "float64":                pa.float64(),
        "timestamp[us, tz=UTC]":  pa.timestamp("us", tz="UTC"),
    }
    fields = []
    for name, dtype, nullable in ICEBERG_SCHEMA_FIELDS:
        atype = arrow_types.get(dtype)
        if atype is None:
            raise ValueError(f"Unknown Arrow type string: {dtype!r}")
        fields.append(pa.field(name, atype, nullable=nullable))
    return pa.schema(fields)


def write_to_iceberg(all_data: pd.DataFrame) -> int:
    """Overwrite today's snapshot in the Iceberg index_members table.

    Uses table.overwrite() with an EqualTo filter on snapshot_date so that
    re-running the pipeline on the same day replaces (not duplicates) rows.
    Returns the total row count after write.
    """
    import pyarrow as pa
    from pyiceberg.catalog import load_catalog
    from pyiceberg.expressions import EqualTo

    log.info("[Iceberg] Preparing %d rows for write...", len(all_data))

    catalog = load_catalog(
        "constituents",
        type="sql",
        uri=f"sqlite:///{CATALOG_DB.as_posix()}",
        warehouse=f"file://{ICEBERG_WAREHOUSE.as_posix()}",
    )
    table = catalog.load_table("constituents.index_members")

    # Prepare DataFrame
    df = all_data.copy()
    for col in ["snapshot_date", "date_added", "date_removed"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    df["fetched_at"] = pd.Timestamp.now(tz="UTC")

    arrow_table = pa.Table.from_pandas(
        df, schema=_build_arrow_schema(), preserve_index=False
    )

    # Atomic overwrite: delete rows for today's snapshot_date, then insert new ones.
    # This prevents duplicates across re-runs on the same day.
    snapshot_date = date.today()
    table.overwrite(arrow_table, overwrite_filter=EqualTo("snapshot_date", snapshot_date))
    log.info(
        "[Iceberg] Overwrote %d rows for snapshot_date=%s",
        len(arrow_table), snapshot_date,
    )

    # Verify with DuckDB count (avoids DataScan.count_rows issue)
    import duckdb
    result = duckdb.sql(
        f"SELECT count(*) FROM read_parquet("
        f"'{ICEBERG_WAREHOUSE.as_posix()}/constituents/index_members/**/*.parquet', "
        f"hive_partitioning=true)"
    ).fetchone()
    total = result[0]
    log.info("[Iceberg] Total rows in index_members: %d", total)
    return total


# ---------------------------------------------------------------------------
# CSV backup
# ---------------------------------------------------------------------------
def save_csv_backup(all_data: pd.DataFrame) -> None:
    CSV_BACKUP.parent.mkdir(parents=True, exist_ok=True)
    all_data.to_csv(CSV_BACKUP, index=False)
    log.info("[CSV] Saved backup to %s (%d rows)", CSV_BACKUP, len(all_data))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(backfill: bool = False, start_year: int | None = None, end_year: int | None = None):
    log.info("=" * 60)
    log.info("Index Constituents Pipeline — %s", date.today())
    log.info("=" * 60)

    frames = []

    # 1. S&P 500
    try:
        frames.append(fetch_sp500_wikipedia())
    except Exception as e:
        log.error("[SP500] FAILED: %s", e)

    # 2. Nasdaq-100
    try:
        frames.append(fetch_nasdaq100())
    except Exception as e:
        log.error("[NDX] FAILED: %s", e)

    # 3-5. Russell 3000 / Russell 2000 / Wilshire 5000 via BlackRock
    for ticker in ["IWV", "IWM", "ITOT"]:
        try:
            frames.append(fetch_blackrock_holdings(ticker))
        except Exception as e:
            log.error("[%s] FAILED: %s", BLACKROCK_PIDS[ticker]["index_code"], e)

    if not frames:
        log.error("No data fetched. Exiting.")
        return

    all_data = pd.concat(frames, ignore_index=True)

    log.info("-" * 60)
    log.info(
        "TOTAL: %d rows across %d indices",
        len(all_data), all_data["index_code"].nunique(),
    )
    for code, count in all_data.groupby("index_code").size().items():
        log.info("  %-10s %d", code, count)
    log.info("-" * 60)

    # Write to Iceberg (idempotent)
    write_to_iceberg(all_data)

    # CSV backup
    save_csv_backup(all_data)

    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Index Constituents Pipeline — S&P 500, Nasdaq-100, Russell 3000/2000, Wilshire 5000"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Currently a no-op; all data is fetched in a single pass regardless.",
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=None,
        help="Reserved for future use (e.g. historical Wikipedia revision snapshots).",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=None,
        help="Reserved for future use.",
    )
    args = parser.parse_args()
    main(backfill=args.backfill, start_year=args.start_year, end_year=args.end_year)
