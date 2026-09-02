#!/usr/bin/env python3
"""
USCF Holdings Pipeline:
  Fetches official daily fund holdings for USCF commodity funds (United States
  Commodity Funds / USCF Investments) from the issuer's Marketing API and writes
  them to Iceberg table constituents.fund_holdings with source='uscf:<TICKER>'.

  Data source: USCF's public holdings page is a JS SPA. The holdings table is
  built by settings__holdings.js, which calls:
    GET https://secure.alpsinc.com/MarketingAPI/api/v1/holding/{ticker}/full
  with a Bearer JWT. The token is issued per-page-load by a PHP endpoint on the
  same site (rotates daily, never needs a real browser):
    GET https://www.uscfinvestments.com/site-template/assets/javascript/api_key.php
      -> var token = '<jwt>';

  NOTE ON THE OLD "USO DEAD-END" NOTE: earlier sessions recorded USO as "page
  loads but its data URL 500s" -- that was a wrong-path artifact, not a WAF.
  The assets actually live under /site-template/assets/javascript/ (fetching
  /assets/javascript/... directly 500s). The api_key.php + Marketing API combo
  works keyless with a plain User-Agent, no cookies, no cf_clearance.

  USO is an ETP (USCF "type": "ETP"), so rows are commodity interests --
  futures (holdingtypeabbrev FUT, identifier like CLV6), US treasuries (GOVT,
  CUSIP/ISIN/SEDOL populated), and swaps (SWAP) -- no equity-style weight
  column on the site, but the API still returns `weight` as a fraction of net
  assets, which converts to weight_pct = weight * 100.

  CLI:
    python uscf_holdings_pipeline.py                   # default universe (USO)
    python uscf_holdings_pipeline.py --tickers USO BNO # explicit

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import os
import re
import sys
import argparse
import logging
import requests
import pandas as pd
from datetime import date, datetime, timezone
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

API_KEY_URL = "https://www.uscfinvestments.com/site-template/assets/javascript/api_key.php"
API_URL = "https://secure.alpsinc.com/MarketingAPI/api/v1/"
HOLDINGS_PATH = "holding/{ticker}/full"

# USCF / United States Commodity Funds gap tickers. USO is the 44-gap list
# entry; the other USCF funds can be appended here the same way.
USCF_FUNDS = {
    "USO": "United States Oil Fund",
}

# e.g. "TREASURY BILL 0 2/4/2027" -> coupon 0, maturity 2027-02-04
_TBILL_NAME_RE = re.compile(r"treasury bill\s+(?:(\d+\.?\d*)\s+)?(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)


def _get_token() -> str:
    """Fetch the daily-rotating Bearer JWT the marketing API requires."""
    r = requests.get(API_KEY_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(r"var token = '([^']+)'", r.text)
    if not m:
        raise RuntimeError(f"api_key.php did not contain a token (got {len(r.text)} bytes)")
    return m.group(1)


def fetch_uscf_holdings(ticker: str) -> pd.DataFrame:
    """Fetch holdings for a single USCF fund from the Marketing API."""
    url = API_URL + HOLDINGS_PATH.format(ticker=ticker)
    token = _get_token()
    log.info("[%s] Fetching from USCF Marketing API...", ticker)
    r = requests.get(
        url,
        headers={**HEADERS, "Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"[{ticker}] API returned no holdings: {str(data)[:200]}")
    log.info("[%s] Raw: %d rows", ticker, len(data))
    return pd.DataFrame(data)


def parse_uscf_holdings(ticker: str, fund_name: str, raw: pd.DataFrame) -> pd.DataFrame:
    """Map the API's flat JSON rows onto fund_holdings schema."""
    df = raw.copy()

    def _cell(col):
        return df.get(col).map(lambda v: v if v is not None else None)

    asof = pd.to_datetime(df["asofdate"], errors="coerce")
    snapshot_date = asof.dt.date.mode().iloc[0] if not asof.dt.date.dropna().empty else None

    holding_type = _cell("holdingtype").astype("object")
    cusip = _cell("cusip").astype("object")
    # Treasuries carry maturity + coupon in the name ("TREASURY BILL 0 2/4/2027").
    def _tbill_meta(name):
        if not isinstance(name, str):
            return None, None
        m = _TBILL_NAME_RE.search(name)
        if not m:
            return None, None
        try:
            maturity = datetime.strptime(m.group(2), "%m/%d/%Y").date()
        except ValueError:
            maturity = None
        coupon = float(m.group(1)) if m.group(1) else (0.0 if "TREASURY BILL" in name.upper() else None)
        return maturity, coupon

    tbill_meta = df["name"].map(_tbill_meta)
    maturity = tbill_meta.map(lambda t: t[0])
    coupon = tbill_meta.map(lambda t: t[1])

    weight = pd.to_numeric(df.get("weight"), errors="coerce") * 100.0
    market_value = pd.to_numeric(df.get("marketvalue"), errors="coerce")
    shares = pd.to_numeric(df.get("shares"), errors="coerce")

    result = pd.DataFrame({
        "snapshot_date": snapshot_date,
        "fund_ticker": ticker,
        "fund_name": fund_name,
        "fund_cik": None,
        "holding_ticker": _cell("identifiertodisplay"),
        "holding_name": _cell("name"),
        "cusip": cusip,
        "isin": _cell("isin"),
        "figi": None,
        "sedol": _cell("sedol"),
        "weight_pct": weight,
        "market_value_usd": market_value,
        "shares_held": shares,
        "asset_category": holding_type,
        "sector": None,
        "country": None,
        "issuer_name": None,
        "filing_date": None,
        "reporting_period_end": snapshot_date,
        "source": f"uscf:{ticker}",
        "fetched_at": datetime.now(timezone.utc),
        "par_value": None,
        "maturity_date": maturity,
        "coupon_pct": coupon,
        "duration": None,
        "ytm_pct": None,
    })

    for col in ("holding_ticker", "holding_name", "cusip", "isin", "sedol"):
        result[col] = result[col].map(
            lambda v: (v.strip() if isinstance(v, str) else (None if v is None else v))
        )
        result[col] = result[col].replace({"nan": None, "None": None, "": None, "NaN": None})

    log.info("[%s] Output: %d holdings (as of %s)", ticker, len(result), snapshot_date)
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
    parser = argparse.ArgumentParser(description="USCF ETF/ETP holdings")
    parser.add_argument("--tickers", nargs="+", default=list(USCF_FUNDS),
                        help="USCF tickers to fetch (default: %s)" % ", ".join(USCF_FUNDS))
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("USCF Holdings Pipeline -- %s (%s)", date.today().isoformat(), ", ".join(args.tickers))
    log.info("=" * 60)

    frames = []
    for ticker in args.tickers:
        fund_name = USCF_FUNDS.get(ticker, f"USCF {ticker.upper()}")
        try:
            raw = fetch_uscf_holdings(ticker)
            frames.append(parse_uscf_holdings(ticker, fund_name, raw))
        except Exception as e:
            log.error("[%s] FAILED: %s", ticker, e)

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