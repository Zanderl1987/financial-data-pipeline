#!/usr/bin/env python3
"""
Securities Reference Pipeline:
  Builds a unified reference table of all financial securities across the pipeline.

  Data sources (layered):
    1. SEC EDGAR company_tickers.json — universal ticker/CIK/name/exchange for all filers
    2. Existing index_members Iceberg table — GICS sector, index membership flags
    3. Finnhub /stock/profile2 — exchange, country, IPO, market cap, shares, industry

  Writes to Iceberg table: constituents.securities
  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/

  NOTE: Finnhub calls are optional (--skip-finnhub) and rate-limited to 60 req/min.
        With --skip-finnhub, only SEC EDGAR + index_members data is used.
"""

import os
import sys
import time
import logging
import argparse
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

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = STORAGE_ROOT / "iceberg"
CATALOG_DB = ICEBERG_WAREHOUSE / "constituents_catalog.db"

EDGAR_USER_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "FinancialDataPipeline research@financial-data-pipeline.com"
)

FINNHUB_REQUEST_INTERVAL = 1.1  # 60 req/min


# ---------------------------------------------------------------------------
# 1. SEC EDGAR — company_tickers.json (universal base)
# ---------------------------------------------------------------------------
def fetch_edgar_tickers() -> pd.DataFrame:
    """Fetch all SEC filer ticker/CIK/name/exchange mappings."""
    log.info("[EDGAR] Fetching company_tickers.json...")
    url = "https://www.sec.gov/files/company_tickers.json"
    headers = {"User-Agent": EDGAR_USER_AGENT, "Accept": "application/json"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()

    rows = []
    for item in data.values():
        rows.append({
            "symbol": item.get("ticker", ""),
            "company_name": item.get("title", ""),
            "cik": int(item.get("cik_str", 0)) if item.get("cik_str") else None,
            "exchange": item.get("exchange", ""),
        })

    df = pd.DataFrame(rows)
    df = df[df["symbol"].str.len() > 0].reset_index(drop=True)
    log.info("[EDGAR] Loaded %d securities from SEC EDGAR", len(df))
    return df


# ---------------------------------------------------------------------------
# 2. Index membership flags + GICS sector from existing index_members table
# ---------------------------------------------------------------------------

# NOTE: each index source (Wikipedia/BlackRock/etc.) reports its own company_name
# (and only the SPX/Wikipedia source carries cik/gics_sector) for the same ticker,
# so this must GROUP BY ticker alone -- grouping by those metadata columns too
# (as this used to) splits one ticker into multiple rows, one per distinct
# metadata combination, and each row only captures the flags that co-occurred
# with it. That silently dropped is_russell3000/is_nasdaq100 for names like
# AAPL/MSFT/NVDA once a later drop_duplicates(subset="ticker") kept only one
# of the split rows. Metadata is picked with an explicit source priority
# (SPX/Wikipedia first, since it's the only source with cik/gics_sector).
_INDEX_FLAGS_SQL = """
    SELECT
        ticker,
        COALESCE(
            MAX(CASE WHEN index_code = 'SPX'     THEN company_name END),
            MAX(CASE WHEN index_code = 'NDX'     THEN company_name END),
            MAX(CASE WHEN index_code = 'RUT3000' THEN company_name END),
            MAX(CASE WHEN index_code = 'W5000'   THEN company_name END),
            MAX(CASE WHEN index_code = 'RUT2000' THEN company_name END)
        ) AS company_name,
        MAX(CASE WHEN index_code = 'SPX' THEN cik END) AS cik,
        MAX(CASE WHEN index_code = 'SPX' THEN gics_sector END) AS gics_sector,
        MAX(CASE WHEN index_code = 'SPX' THEN gics_sub_industry END) AS gics_sub_industry,
        MAX(CASE WHEN index_code = 'SPX'     THEN 1 ELSE 0 END) AS is_sp500,
        MAX(CASE WHEN index_code = 'NDX'     THEN 1 ELSE 0 END) AS is_nasdaq100,
        MAX(CASE WHEN index_code = 'RUT3000' THEN 1 ELSE 0 END) AS is_russell3000,
        MAX(CASE WHEN index_code = 'RUT2000' THEN 1 ELSE 0 END) AS is_russell2000,
        MAX(CASE WHEN index_code = 'W5000'   THEN 1 ELSE 0 END) AS is_wilshire5000
    FROM members
    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM members)
    GROUP BY ticker
"""


def _aggregate_index_flags(members) -> pd.DataFrame:
    """Run _INDEX_FLAGS_SQL against `members` (a DataFrame or duckdb relation with
    ticker/company_name/cik/gics_sector/gics_sub_industry/index_code/snapshot_date
    columns). Split out from build_index_flags() so the aggregation logic is
    testable against synthetic data, not just the real Iceberg files."""
    import duckdb

    df = duckdb.sql(_INDEX_FLAGS_SQL).fetchdf()
    for col in ["is_sp500", "is_nasdaq100", "is_russell3000", "is_russell2000", "is_wilshire5000"]:
        df[col] = df[col].astype(bool)
    return df


def build_index_flags() -> pd.DataFrame:
    """Read the existing index_members Iceberg table and compute per-ticker index membership flags."""
    import duckdb

    log.info("[INDEX] Reading index_members for membership flags...")
    parquet_path = (ICEBERG_WAREHOUSE / "constituents" / "index_members" / "**" / "*.parquet").as_posix()
    members = duckdb.sql(f"SELECT * FROM read_parquet('{parquet_path}', hive_partitioning=true)")
    df = _aggregate_index_flags(members)

    log.info("[INDEX] Computed flags for %d unique tickers", len(df))
    return df


# ---------------------------------------------------------------------------
# 3. Finnhub /stock/profile2 — enrichment for tracked symbols
# ---------------------------------------------------------------------------
def fetch_finnhub_profiles(symbols: list[str]) -> pd.DataFrame:
    """Fetch company profiles from Finnhub for a list of symbols."""
    if not FINNHUB_API_KEY:
        log.warning("[FINNHUB] FINNHUB_API_KEY not set — skipping profile enrichment")
        return pd.DataFrame()

    log.info("[FINNHUB] Fetching profiles for %d symbols...", len(symbols))
    rows = []
    for i, symbol in enumerate(symbols, 1):
        try:
            url = f"{FINNHUB_BASE_URL}/stock/profile2?symbol={symbol}&token={FINNHUB_API_KEY}"
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
            if "name" in data and data["name"]:
                rows.append({
                    "symbol": symbol,
                    "name": data.get("name"),
                    "exchange": data.get("exchange"),
                    "country": data.get("country"),
                    "ipo": data.get("ipo"),
                    "marketCapitalization": data.get("marketCapitalization"),
                    "shareOutstanding": data.get("shareOutstanding"),
                    "finnhubIndustry": data.get("finnhubIndustry"),
                    "currency": data.get("currency"),
                })
        except Exception as e:
            log.debug("[FINNHUB] Failed for %s: %s", symbol, e)

        if i % 50 == 0:
            log.info("[FINNHUB] Progress: %d/%d", i, len(symbols))
        time.sleep(FINNHUB_REQUEST_INTERVAL)

    df = pd.DataFrame(rows)
    log.info("[FINNHUB] Fetched %d profiles", len(df))
    return df


# ---------------------------------------------------------------------------
# 4. Merge all sources into unified securities table
# ---------------------------------------------------------------------------
def build_securities_table(
    edgar: pd.DataFrame,
    index_flags: pd.DataFrame,
    finnhub: pd.DataFrame,
) -> pd.DataFrame:
    """Merge all sources into a single securities reference DataFrame."""
    log.info("[MERGE] Building unified securities table...")

    # Start with EDGAR as the universal base
    securities = edgar.copy()
    securities["asset_type"] = "stock"  # default; can be refined later
    securities["currency"] = "USD"
    securities["country"] = None
    securities["market_cap"] = None
    securities["shares_outstanding"] = None
    securities["ipo_date"] = None
    securities["sector"] = None
    securities["industry"] = None
    securities["primary_source"] = "sec_edgar"

    # Layer 1: Merge index membership flags + GICS sector
    if not index_flags.empty:
        idx_cols = [
            "ticker", "gics_sector", "gics_sub_industry",
            "is_sp500", "is_nasdaq100", "is_russell3000", "is_russell2000", "is_wilshire5000",
        ]
        idx_merge = index_flags[idx_cols].drop_duplicates(subset="ticker").copy()
        idx_merge = idx_merge.rename(columns={"ticker": "symbol"})

        # Use EDGAR company_name as base, enrich with index_members company_name where EDGAR is empty
        idx_name = index_flags[["ticker", "company_name"]].drop_duplicates(subset="ticker").rename(
            columns={"ticker": "symbol", "company_name": "idx_company_name"}
        )

        securities = securities.merge(idx_merge, on="symbol", how="left")
        securities = securities.merge(idx_name, on="symbol", how="left")

        # Fill empty company_name from index_members
        mask = securities["company_name"].isna() | (securities["company_name"] == "")
        securities.loc[mask, "company_name"] = securities.loc[mask, "idx_company_name"]
        securities = securities.drop(columns=["idx_company_name"], errors="ignore")

        # Fill sector from GICS (Wikipedia source)
        mask = securities["sector"].isna()
        securities.loc[mask, "sector"] = securities.loc[mask, "gics_sector"]

        # Fill CIK from index_members where EDGAR has 0
        mask = securities["cik"].isna() | (securities["cik"] == 0)
        cik_map = index_flags.drop_duplicates(subset="ticker").set_index("ticker")["cik"]
        securities.loc[mask, "cik"] = securities.loc[mask, "symbol"].map(cik_map)

        # Mark primary source
        in_index = securities["is_sp500"].fillna(False) | securities["is_nasdaq100"].fillna(False) | securities["is_russell3000"].fillna(False)
        securities.loc[in_index, "primary_source"] = "index_members"

        # Fill NaN boolean flags with False
        for col in ["is_sp500", "is_nasdaq100", "is_russell3000", "is_russell2000", "is_wilshire5000"]:
            securities[col] = securities[col].fillna(False).astype(bool)

    # Layer 2: Merge Finnhub profile enrichment
    if not finnhub.empty:
        fh = finnhub.copy()
        fh = fh.rename(columns={
            "name": "fh_name",
            "exchange": "fh_exchange",
            "country": "fh_country",
            "ipo": "fh_ipo",
            "marketCapitalization": "fh_market_cap",
            "shareOutstanding": "fh_shares",
            "finnhubIndustry": "fh_industry",
            "currency": "fh_currency",
        })
        securities = securities.merge(fh, on="symbol", how="left")

        # Fill fields from Finnhub where current values are empty
        for col, fh_col in [
            ("exchange", "fh_exchange"),
            ("country", "fh_country"),
            ("ipo_date", "fh_ipo"),
            ("market_cap", "fh_market_cap"),
            ("shares_outstanding", "fh_shares"),
            ("industry", "fh_industry"),
            ("currency", "fh_currency"),
        ]:
            mask = securities[col].isna() | (securities[col] == "")
            securities.loc[mask, col] = securities.loc[mask, fh_col]

        # Fill company_name from Finnhub
        mask = securities["company_name"].isna() | (securities["company_name"] == "")
        securities.loc[mask, "company_name"] = securities.loc[mask, "fh_name"]

        # Update primary source for Finnhub-enriched rows
        enriched = securities["fh_market_cap"].notna()
        securities.loc[enriched, "primary_source"] = "finnhub_profile2"

        securities = securities.drop(columns=[c for c in securities.columns if c.startswith("fh_")], errors="ignore")

    # Fill remaining defaults
    for col in ["is_sp500", "is_nasdaq100", "is_russell3000", "is_russell2000", "is_wilshire5000"]:
        securities[col] = securities[col].fillna(False).astype(bool)

    securities["last_refreshed"] = datetime.now(timezone.utc)

    log.info("[MERGE] Final table: %d securities", len(securities))
    log.info("[MERGE] Coverage: %d with CIK, %d with sector, %d with market_cap",
             securities["cik"].notna().sum(),
             securities["sector"].notna().sum(),
             securities["market_cap"].notna().sum())

    return securities


# ---------------------------------------------------------------------------
# 5. Write to Iceberg
# ---------------------------------------------------------------------------
def write_to_iceberg(securities: pd.DataFrame) -> int:
    """Overwrite the entire securities table (reference data — full replace)."""
    import pyarrow as pa
    from pyiceberg.catalog import load_catalog
    from pyiceberg.expressions import AlwaysTrue

    log.info("[Iceberg] Writing %d rows to constituents.securities...", len(securities))

    catalog = load_catalog(
        "constituents",
        type="sql",
        uri=f"sqlite:///{CATALOG_DB.as_posix()}",
        warehouse=f"file://{ICEBERG_WAREHOUSE.as_posix()}",
    )
    table = catalog.load_table("constituents.securities")

    # Prepare DataFrame
    df = securities.copy()
    df["last_refreshed"] = pd.Timestamp.now(tz="UTC")

    # Ensure correct types
    for col in ["market_cap", "shares_outstanding"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["cik"] = pd.to_numeric(df["cik"], errors="coerce").astype("Int64")

    # Build Arrow schema matching the Iceberg table
    arrow_schema = pa.schema([
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("company_name", pa.string(), nullable=True),
        pa.field("asset_type", pa.string(), nullable=True),
        pa.field("sector", pa.string(), nullable=True),
        pa.field("industry", pa.string(), nullable=True),
        pa.field("exchange", pa.string(), nullable=True),
        pa.field("currency", pa.string(), nullable=True),
        pa.field("country", pa.string(), nullable=True),
        pa.field("market_cap", pa.float64(), nullable=True),
        pa.field("shares_outstanding", pa.float64(), nullable=True),
        pa.field("ipo_date", pa.string(), nullable=True),
        pa.field("cik", pa.int64(), nullable=True),
        pa.field("is_sp500", pa.bool_(), nullable=True),
        pa.field("is_nasdaq100", pa.bool_(), nullable=True),
        pa.field("is_dji30", pa.bool_(), nullable=True),
        pa.field("is_russell3000", pa.bool_(), nullable=True),
        pa.field("is_russell2000", pa.bool_(), nullable=True),
        pa.field("is_wilshire5000", pa.bool_(), nullable=True),
        pa.field("primary_source", pa.string(), nullable=True),
        pa.field("last_refreshed", pa.timestamp("us", tz="UTC"), nullable=False),
    ])

    # Select columns in schema order
    col_order = [f.name for f in arrow_schema]
    for col in col_order:
        if col not in df.columns:
            df[col] = None
    df = df[col_order]

    arrow_table = pa.Table.from_pandas(df, schema=arrow_schema, preserve_index=False)

    # Full replace — this is a reference table, not time-series
    table.overwrite(arrow_table, overwrite_filter=AlwaysTrue())
    log.info("[Iceberg] Successfully wrote %d rows to constituents.securities", len(arrow_table))

    # Verify
    import duckdb
    result = duckdb.sql(
        f"SELECT count(*) FROM read_parquet("
        f"'{ICEBERG_WAREHOUSE.as_posix()}/constituents/securities/**/*.parquet', "
        f"hive_partitioning=true)"
    ).fetchone()
    log.info("[Iceberg] Total rows in securities: %d", result[0])
    return result[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(skip_finnhub: bool = False, finnhub_symbols: list[str] | None = None):
    log.info("=" * 60)
    log.info("Securities Reference Pipeline — %s", date.today())
    log.info("=" * 60)

    # 1. SEC EDGAR base
    edgar = fetch_edgar_tickers()

    # 2. Index membership flags
    index_flags = build_index_flags()

    # 3. Finnhub enrichment (optional)
    finnhub = pd.DataFrame()
    if not skip_finnhub:
        # Determine which symbols to enrich
        if finnhub_symbols:
            symbols = finnhub_symbols
        else:
            # Enrich symbols that appear in index_members
            symbols = sorted(index_flags["ticker"].unique().tolist())
        finnhub = fetch_finnhub_profiles(symbols)

    # 4. Merge
    securities = build_securities_table(edgar, index_flags, finnhub)

    # 5. Write to Iceberg
    write_to_iceberg(securities)

    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Securities Reference Pipeline — unified ticker/CIK/sector/market-cap reference table"
    )
    parser.add_argument(
        "--skip-finnhub",
        action="store_true",
        help="Skip Finnhub profile enrichment (use only SEC EDGAR + index_members data).",
    )
    parser.add_argument(
        "--finnhub-symbols",
        nargs="+",
        default=None,
        help="Specific symbols to enrich via Finnhub (default: all index_members tickers).",
    )
    args = parser.parse_args()
    main(skip_finnhub=args.skip_finnhub, finnhub_symbols=args.finnhub_symbols)
