#!/usr/bin/env python3
"""
Vanguard Bond ETF Holdings Pipeline:
  Fetches quarterly holdings for the six Vanguard fixed-income ETFs that the
  generic fund_holdings_pipeline.py N-PORT path cannot resolve correctly --
  BIV, BLV, BSV, VCIT, VCSH, VCLT -- by walking the sponsor trust's NPORT-P
  filings and matching each fund's series_id. Writes to Iceberg table
  constituents.fund_holdings with source='edgar_nport:<TICKER>'.

  Data source: SEC EDGAR N-PORT-P filings via EdgarTools.
    - Vanguard Bond Index Funds (CIK 794105)        carries series S000002561
      (BIV), S000002562 (BLV), S000002563 (BSV).
    - Vanguard Whitehall Funds II (CIK 1021882)     carries series S000026863
      (VCIT), S000026862 (VCSH), S000026864 (VCLT).
  (series/class/CIK taken from SEC company_tickers_mf.json, 2026-09-01.)

  Why a dedicated script instead of fund_holdings_pipeline.py's generic
  EdgarTools path: `Fund(ticker)` resolves these ETF tickers to a *sibling
  share class* of the same series (e.g. BIV -> VBILX or the mutual-fund class),
  so get_latest_report()/get_portfolio() return the wrong fund. `.series`
  resolves correctly, but report/portfolio fetching does not. Same failure
  class as ROBO (Exchange Traded Concepts Trust), fixed the same way: walk the
  trust's NPORT-P filings newest-first and take the first filing whose own
  report.series_id matches, rather than trusting Fund(ticker).

  Reporting cadence: N-PORT is quarterly with a ~60 day filing lag, so daily
  runs rewrite the same quarter's numbers until a new one is filed -- expected
  and harmless (Iceberg overwrite is idempotent per fund_ticker).

  CLI:
    python vanguard_bond_holdings_pipeline.py                     # all six
    python vanguard_bond_holdings_pipeline.py --tickers BIV VCLT  # subset

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import os
import sys
import time
import argparse
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
EDGAR_SLEEP = 0.15  # between filing.obj() calls (~6 req/sec, well under EDGAR's 10)

STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = STORAGE_ROOT / "iceberg"
CATALOG_DB = ICEBERG_WAREHOUSE / "constituents_catalog.db"

SNAPSHOT_DATE = date.today()
FETCHED_AT = datetime.now(timezone.utc)

# ticker -> (series_id, fund name). CIKs from SEC company_tickers_mf.json.
FUNDS = {
    "BIV":  ("S000002561", "Vanguard Intermediate-Term Bond Index Fund"),
    "BLV":  ("S000002562", "Vanguard Long-Term Bond Index Fund"),
    "BSV":  ("S000002563", "Vanguard Short-Term Bond Index Fund"),
    "VCIT": ("S000026863", "Vanguard Intermediate-Term Corporate Bond Index Fund"),
    "VCSH": ("S000026862", "Vanguard Short-Term Corporate Bond Index Fund"),
    "VCLT": ("S000026864", "Vanguard Long-Term Corporate Bond Index Fund"),
}

# sponsor trust CIK -> series ids it files for (one filing-walk per trust)
TRUSTS: dict[int, list[str]] = {
    794105:  ["S000002561", "S000002562", "S000002563"],
    1021882: ["S000026862", "S000026863", "S000026864"],
}

# A Vanguard trust files one NPORT-P per series per quarter. The newest quarter's
# batch is all filed the same day, so the wanted series should match within the
# first ~30-40 filings; a generous bound guards against trusts that bundle more.
MAX_FILINGS_SCANNED_PER_TRUST = 300


def _find_reports(cik: int, want_series: set[str]) -> dict:
    """Walk one trust's NPORT-P filings (newest first), returning {series_id:
    report} for every wanted series found. The scan ends once a full quarter's
    batch has been passed (all wanted series either found or filed elsewhere
    under this CIK and impossible to prove absent within the scan)."""
    from edgar import Company, set_identity

    set_identity(EDGAR_USER_AGENT)
    company = Company(cik)
    filings = company.get_filings(form="NPORT-P")
    log.info(
        "[CIK %s] Scanning up to %d of %d trust NPORT-P filings for series %s...",
        cik, MAX_FILINGS_SCANNED_PER_TRUST, len(filings), sorted(want_series),
    )

    found: dict[str, object] = {}
    for i in range(min(MAX_FILINGS_SCANNED_PER_TRUST, len(filings))):
        filing = filings[i]
        try:
            report = filing.obj()
        except Exception as e:
            log.warning("[CIK %s] Filing %d (%s) failed to parse: %s", cik, i, filing.filing_date, e)
            continue
        if report is not None and getattr(report, "series_id", None) in want_series:
            sid = report.series_id
            if sid not in found:
                log.info(
                    "[CIK %s] Matched series %s at filing index %d: %s, name=%r",
                    cik, sid, i, filing.filing_date, getattr(report, "name", None),
                )
                found[sid] = report
            if len(found) == len(want_series):
                break
        time.sleep(EDGAR_SLEEP)

    missing = want_series - set(found)
    if missing:
        log.warning(
            "[CIK %s] Series not found in first %d filings: %s",
            cik, MAX_FILINGS_SCANNED_PER_TRUST, sorted(missing),
        )
    return found


def _parse_report(fund_name: str, ticker: str, report) -> pd.DataFrame:
    """Parse a matched N-PORT report into fund_holdings schema (bond rows are
    CUSIP/ISIN-keyed, same mapping as fund_holdings_pipeline.py)."""
    df = report.securities_data()
    if df is None or df.empty:
        raise RuntimeError(f"[{ticker}] Empty portfolio")

    col_map = {
        "name": "holding_name",
        "ticker": "holding_ticker",
        "cusip": "cusip",
        "isin": "isin",
        "value_usd": "market_value_usd",
        "pct_value": "weight_pct",
        "balance": "shares_held",
        "asset_category": "asset_category",
        "investment_country": "country",
        "maturity_date": "maturity_date",
        "annualized_rate": "coupon_pct",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    for col in ["market_value_usd", "weight_pct", "shares_held", "coupon_pct"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    result = pd.DataFrame({
        "snapshot_date": SNAPSHOT_DATE,
        "fund_ticker": ticker,
        "fund_name": fund_name,
        "fund_cik": getattr(report, "cik", None),
        "holding_ticker": df.get("holding_ticker"),
        "holding_name": df.get("holding_name"),
        "cusip": df.get("cusip"),
        "isin": df.get("isin"),
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
        "par_value": None,
        "maturity_date": df.get("maturity_date"),
        "coupon_pct": df.get("coupon_pct"),
        "duration": None,
        "ytm_pct": None,
    })

    # Keep rows that identify a security by TICKER, CUSIP, or ISIN (bond-fund
    # N-PORT rows are CUSIP/ISIN-keyed; drops cash/sweep rows like "Vanguard
    # Market Liquidity" whose cusip is "N/A").
    ticker_ok = result["holding_ticker"].notna() & (result["holding_ticker"].astype(str) != "")
    cusip_ok = result["cusip"].fillna("").astype(str).str.len() >= 6
    isin_ok = result["isin"].fillna("").astype(str).str.len() >= 8
    non_na = ~(
        result["cusip"].fillna("").astype(str).isin(["N/A", "nan", "", "NaN"])
        & result["isin"].fillna("").astype(str).isin(["N/A", "nan", "", "NaN"])
    )
    result = result[ticker_ok | ((cusip_ok | isin_ok) & non_na)]

    log.info("[%s] Output: %d holdings (period: %s)", ticker, len(result),
             getattr(report, "reporting_period", "unknown"))
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
    parser = argparse.ArgumentParser(description="Vanguard bond ETF (N-PORT series-matched) holdings")
    parser.add_argument("--tickers", nargs="+", default=list(FUNDS),
                        help="Vanguard bond tickers to fetch (default: all six)")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Vanguard Bond ETF Holdings Pipeline -- %s (%s)",
             SNAPSHOT_DATE, ", ".join(args.tickers))
    log.info("=" * 60)

    wanted = {t.upper() for t in args.tickers}
    unknown = wanted - set(FUNDS)
    if unknown:
        log.warning("Unknown tickers ignored: %s", sorted(unknown))
    wanted &= set(FUNDS)

    # Group wanted series by trust so each trust's filing list is walked once.
    frames = []
    want_series_by_trust = {
        cik: [sid for sid in ss if any(FUNDS[t][0] == sid for t in wanted)]
        for cik, ss in TRUSTS.items()
    }
    for cik, want_series in want_series_by_trust.items():
        if not want_series:
            continue
        try:
            reports = _find_reports(cik, set(want_series))
        except Exception as e:
            log.error("[CIK %s] Trust scan FAILED: %s", cik, e)
            continue
        for ticker, (series_id, fund_name) in FUNDS.items():
            if series_id in reports and ticker in wanted:
                try:
                    frames.append(_parse_report(fund_name, ticker, reports[series_id]))
                except Exception as e:
                    log.error("[%s] Parse FAILED: %s", ticker, e)

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