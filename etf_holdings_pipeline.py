#!/usr/bin/env python3
"""
ETF Holdings Pipeline:
  Fetches full holdings for US-listed ETFs from SecuritiesDB (free, no auth).
  Writes to Iceberg table: constituents.etf_holdings

  Data source:
    SecuritiesDB Free ETF Holdings API (securitiesdb.com)
    GET https://securitiesdb.com/api/v1/etfs/{ticker}/holdings
    No API key required, no rate limit documented.
    Data sourced from SEC EDGAR N-PORT + issuer APIs.

  CLI:
    python etf_holdings_pipeline.py                    # default universe (200+ ETFs)
    python etf_holdings_pipeline.py --etf-tickers SPY VOO QQQ  # specific tickers
    python etf_holdings_pipeline.py --limit 50         # first 50 ETFs only
    python etf_holdings_pipeline.py --backfill         # same as default (snapshot)

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
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

REQUEST_DELAY = 0.1

DEFAULT_ETF_UNIVERSE = {
    # --- Broad Market US ---
    "SPY":  "SPDR S&P 500 ETF Trust",
    "IVV":  "iShares Core S&P 500 ETF",
    "VOO":  "Vanguard S&P 500 ETF",
    "VTI":  "Vanguard Total Stock Market ETF",
    "ITOT": "iShares Core S&P Total US Stock Market ETF",
    "IWV":  "iShares Russell 3000 ETF",
    "IWB":  "iShares Russell 1000 ETF",
    "SCHB": "Schwab US Broad Market ETF",
    "DIA":  "SPDR Dow Jones Industrial Average ETF",
    "SPLG": "SPDR Portfolio S&P 500 ETF",
    "SPTM": "SPDR Total Stock Market ETF",
    "SCHX": "Schwab US Large-Cap ETF",
    "IWM":  "iShares Russell 2000 ETF",
    "IJR":  "iShares Core S&P Small-Cap ETF",
    "IJH":  "iShares Core S&P Mid-Cap ETF",
    "IWR":  "iShares Russell Mid-Cap ETF",
    "SCHM": "Schwab US Mid-Cap ETF",
    "SCHA": "Schwab US Small-Cap ETF",
    "IWO":  "iShares Russell 2000 Growth ETF",
    "IWN":  "iShares Russell 2000 Value ETF",
    "IWF":  "iShares Russell 1000 Growth ETF",
    "IWD":  "iShares Russell 1000 Value ETF",
    # --- International ---
    "EFA":  "iShares MSCI EAFE ETF",
    "EEM":  "iShares MSCI Emerging Markets ETF",
    "IEFA": "iShares Core MSCI EAFE ETF",
    "IEMG": "iShares Core MSCI Emerging Markets ETF",
    "VXUS": "Vanguard Total International Stock ETF",
    "VEA":  "Vanguard FTSE Developed Markets ETF",
    "VWO":  "Vanguard FTSE Emerging Markets ETF",
    "ACWI": "iShares MSCI ACWI ETF",
    "ACWX": "iShares MSCI ACWI ex US ETF",
    "IXUS": "iShares Core MSCI Total Intl Stock ETF",
    "VEU":  "Vanguard FTSE All-World ex-US ETF",
    "SCHE": "Schwab Emerging Markets Equity ETF",
    "SCHF": "Schwab International Equity ETF",
    "SPDW": "SPDR Portfolio Developed World ex-US ETF",
    "SPEM": "SPDR Portfolio Emerging Markets ETF",
    # --- US Sector ---
    "XLK":  "Technology Select Sector SPDR ETF",
    "XLF":  "Financial Select Sector SPDR ETF",
    "XLE":  "Energy Select Sector SPDR ETF",
    "XLV":  "Health Care Select Sector SPDR ETF",
    "XLY":  "Consumer Discretionary Select Sector SPDR ETF",
    "XLI":  "Industrial Select Sector SPDR ETF",
    "XLC":  "Communication Services Select Sector SPDR ETF",
    "XLRE": "Real Estate Select Sector SPDR ETF",
    "XLP":  "Consumer Staples Select Sector SPDR ETF",
    "XLU":  "Utilities Select Sector SPDR ETF",
    "XLB":  "Materials Select Sector SPDR ETF",
    "VGT":  "Vanguard Information Technology ETF",
    "VFH":  "Vanguard Financials ETF",
    "VDE":  "Vanguard Energy ETF",
    "VHT":  "Vanguard Health Care ETF",
    "VCR":  "Vanguard Consumer Discretionary ETF",
    "VIS":  "Vanguard Industrials ETF",
    "VOX":  "Vanguard Communication Services ETF",
    "VNQ":  "Vanguard Real Estate ETF",
    "VAW":  "Vanguard Materials ETF",
    "VPU":  "Vanguard Utilities ETF",
    "VUG":  "Vanguard Growth ETF",
    "VTV":  "Vanguard Value ETF",
    "VBK":  "Vanguard Small-Cap Growth ETF",
    "VBR":  "Vanguard Small-Cap Value ETF",
    # --- Factor / Style ---
    "USMV": "iShares MSCI USA Min Vol Factor ETF",
    "QUAL": "iShares MSCI USA Quality Factor ETF",
    "MTUM": "iShares MSCI USA Momentum Factor ETF",
    "SIZE": "iShares MSCI USA Size Factor ETF",
    "VLUE": "iShares MSCI USA Value Factor ETF",
    "IUSV": "iShares Core S&P US Value ETF",
    "IUSG": "iShares Core S&P US Growth ETF",
    "SCHG": "Schwab US Large-Cap Growth ETF",
    "SCHV": "Schwab US Large-Cap Value ETF",
    "SPYG": "SPDR Portfolio S&P 500 Growth ETF",
    "SPYV": "SPDR Portfolio S&P 500 Value ETF",
    # --- Dividend ---
    "VYM":  "Vanguard High Dividend Yield ETF",
    "VIG":  "Vanguard Dividend Appreciation ETF",
    "SCHD": "Schwab US Dividend Equity ETF",
    "DVY":  "iShares Select Dividend ETF",
    "SDY":  "SPDR S&P Dividend ETF",
    "HDV":  "iShares Core High Dividend ETF",
    "SPYD": "SPDR Portfolio S&P 500 High Dividend ETF",
    "DGRO": "iShares Core Dividend Growth ETF",
    "DGRW": "WisdomTree US Dividend Growth ETF",
    "VYMI": "Vanguard International High Dividend Yield ETF",
    "IDV":  "iShares International Select Dividend ETF",
    "SCHY": "Schwab International Dividend Equity ETF",
    "DIV":  "Global X SuperDividend ETF",
    # --- Fixed Income ---
    "AGG":  "iShares Core US Aggregate Bond ETF",
    "BND":  "Vanguard Total Bond Market ETF",
    "LQD":  "iShares iBoxx Investment Grade Corporate Bond ETF",
    "HYG":  "iShares iBoxx High Yield Corporate Bond ETF",
    "TIP":  "iShares TIPS Bond ETF",
    "SHY":  "iShares 1-3 Year Treasury Bond ETF",
    "IEI":  "iShares 3-7 Year Treasury Bond ETF",
    "TLT":  "iShares 20+ Year Treasury Bond ETF",
    "IEF":  "iShares 7-10 Year Treasury Bond ETF",
    "BIV":  "Vanguard Intermediate-Term Bond ETF",
    "BLV":  "Vanguard Long-Term Bond ETF",
    "BSV":  "Vanguard Short-Term Bond ETF",
    "MUB":  "iShares National Muni Bond ETF",
    "VTEB": "Vanguard Tax-Exempt Bond ETF",
    "SHV":  "iShares Short Treasury Bond ETF",
    "BIL":  "SPDR Bloomberg 1-3 Month T-Bill ETF",
    "SGOV": "iShares 0-3 Month Treasury Bond ETF",
    "JPST": "JPMorgan Ultra-Short Income ETF",
    "JNK":  "SPDR Bloomberg High Yield Bond ETF",
    "EMB":  "iShares JP Morgan USD Emerging Markets Bond ETF",
    "VCIT": "Vanguard Intermediate-Term Corporate Bond ETF",
    "VCSH": "Vanguard Short-Term Corporate Bond ETF",
    "VCLT": "Vanguard Long-Term Corporate Bond ETF",
    "BNDX": "Vanguard Total International Bond ETF",
    "BWX":  "SPDR Bloomberg International Treasury Bond ETF",
    # --- Real Estate ---
    "IYR":  "iShares US Real Estate ETF",
    "REET": "iShares Global REIT ETF",
    "SCHH": "Schwab US REIT ETF",
    "USRT": "iShares Core US REIT ETF",
    # --- Commodities ---
    "GLD":  "SPDR Gold Shares",
    "SLV":  "iShares Silver Trust",
    "IAU":  "iShares Gold Trust",
    "SGOL": "Aberdeen Standard Physical Gold Shares ETF",
    "USO":  "United States Oil Fund",
    "DBC":  "Invesco DB Commodity Index Tracking Fund",
    "GSG":  "iShares S&P GSCI Commodity-Indexed Trust",
    "PDBC": "Invesco Optimum Yield Diversified Commodity Strategy ETF",
    "LIT":  "Global X Lithium & Battery Tech ETF",
    "COPX": "Global X Copper Miners ETF",
    "MOO":  "VanEck Agribusiness ETF",
    "TAN":  "Invesco Solar ETF",
    "ICLN": "iShares Global Clean Energy ETF",
    "FAN":  "First Trust Global Wind Energy ETF",
    # --- Thematic / Innovation ---
    "ARKK": "ARK Innovation ETF",
    "ARKG": "ARK Genomic Revolution ETF",
    "ARKW": "ARK Next Generation Internet ETF",
    "ARKQ": "ARK Autonomous Technology & Robotics ETF",
    "ARKF": "ARK Fintech Innovation ETF",
    "ARKX": "ARK Space Exploration & Innovation ETF",
    "BOTZ": "Global X Robotics & AI ETF",
    "ROBO": "Robo Global Robotics & Automation ETF",
    "AIQ":  "Global X AI & Technology ETF",
    "IBIT": "iShares Bitcoin Trust ETF",
    "ETHA": "iShares Ethereum Trust ETF",
    # --- Covered Call / Options ---
    "JEPI": "JPMorgan Equity Premium Income ETF",
    "JEPQ": "JPMorgan Nasdaq Equity Premium Income ETF",
    "XYLD": "Global X S&P 500 Covered Call ETF",
    "QYLD": "Global X Nasdaq 100 Covered Call ETF",
    "RYLD": "Global X Russell 2000 Covered Call ETF",
    "DIVO": "Amplify CWP Enhanced Dividend Income ETF",
    # --- Healthcare ---
    "IHI":  "iShares US Medical Devices ETF",
    "IBB":  "iShares Biotechnology ETF",
    "XBI":  "SPDR S&P Biotech ETF",
    "LABU": "Direxion Daily S&P Biotech Bull 3X ETF",
    # --- Technology ---
    "QQQ":  "Invesco QQQ Trust",
    "SOXX": "iShares PHLX Semiconductor Sector ETF",
    "SMH":  "VanEck Semiconductor ETF",
    "IGV":  "iShares Expanded Tech-Software Sector ETF",
    # --- Consumer ---
    "IYK":  "iShares US Consumer Staples ETF",
    "PBJ":  "Invesco Food & Beverage ETF",
    "PEJ":  "Invesco Leisure & Entertainment ETF",
    # --- Energy ---
    "IYE":  "iShares US Energy ETF",
    "OIH":  "VanEck Oil Services ETF",
    "XOP":  "SPDR S&P Oil & Gas Exploration & Production ETF",
    # --- Financial ---
    "IYF":  "iShares US Financials ETF",
    "KBE":  "SPDR S&P Bank ETF",
    "KRE":  "SPDR S&P Regional Banking ETF",
    # --- Industrial ---
    "IYJ":  "iShares US Industrials ETF",
    "ITA":  "iShares US Aerospace & Defense ETF",
    "XAR":  "SPDR S&P Aerospace & Defense ETF",
    # --- Volatility / Hedge ---
    "SVXY": "ProShares Short VIX Short-Term Futures ETF",
    "SH":   "ProShares Short S&P500",
    "DOG":  "ProShares Short Dow30",
    "PSQ":  "ProShares Short QQQ",
    # --- Fixed Income — International ---
    "PCY":  "Invesco Emerging Markets Sovereign Debt ETF",
    # --- ESG ---
    "ESGU": "iShares ESG Aware MSCI USA ETF",
    "ESGD": "iShares ESG Aware MSCI EAFE ETF",
    "ESGE": "iShares ESG Aware MSCI Emerging Markets ETF",
    "SUSA": "iShares MSCI USA ESG Select ETF",
    "DSI":  "iShares MSCI KLD 400 Social ETF",
    # --- Multi-Asset ---
    "AOR":  "iShares Core Growth Allocation ETF",
    "AOM":  "iShares Core Moderate Allocation ETF",
    "AOA":  "iShares Core Aggressive Allocation ETF",
    "AOK":  "iShares Core Conservative Allocation ETF",
}


def fetch_etf_holdings(ticker: str, fund_name: str) -> pd.DataFrame | None:
    """Fetch full ETF holdings from SecuritiesDB free API."""
    url = f"https://securitiesdb.com/api/v1/etfs/{ticker}/holdings"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.HTTPError as e:
        if r.status_code == 404:
            log.warning("[%s] Not found on SecuritiesDB (404)", ticker)
        else:
            log.error("[%s] HTTP %d: %s", ticker, r.status_code, e)
        return None
    except Exception as e:
        log.error("[%s] Request failed: %s", ticker, e)
        return None

    payload = data.get("data")
    if not payload:
        log.warning("[%s] Empty response data", ticker)
        return None

    holdings_raw = payload.get("holdings", [])
    if not holdings_raw:
        log.warning("[%s] No holdings in response", ticker)
        return None

    rows = []
    for h in holdings_raw:
        rows.append({
            "snapshot_date": SNAPSHOT_DATE,
            "fund_ticker": ticker,
            "fund_name": fund_name,
            "holding_ticker": h.get("ticker"),
            "holding_name": h.get("name"),
            "weight_pct": h.get("weight_pct"),
            "sector": h.get("sector"),
            "market_cap": h.get("market_cap"),
            "piotroski_f": h.get("piotroski_f"),
            "altman_z": h.get("altman_z"),
            "source": "securitiesdb",
            "fetched_at": FETCHED_AT,
        })

    result = pd.DataFrame(rows)

    non_security = result["holding_ticker"].isna() | (result["holding_ticker"] == "-")
    dropped = non_security.sum()
    if dropped > 0:
        result = result[~non_security]
        log.info("[%s] Filtered %d non-security rows (cash, etc.)", ticker, dropped)

    total_holdings = payload.get("total_holdings", 0)
    log.info("[%s] Output: %d / %d holdings", ticker, len(result), total_holdings)
    return result


def fetch_all_etf_holdings(only_tickers: list[str] | None = None, limit: int | None = None) -> list[pd.DataFrame]:
    """Fetch holdings for ETFs in DEFAULT_ETF_UNIVERSE."""
    targets = only_tickers if only_tickers else list(DEFAULT_ETF_UNIVERSE.keys())
    if limit:
        targets = targets[:limit]

    frames = []
    failed = []
    skipped = []

    for i, ticker in enumerate(targets, 1):
        ticker = ticker.upper()
        fund_name = DEFAULT_ETF_UNIVERSE.get(ticker, "")

        if not fund_name and not only_tickers:
            log.warning("[%s] Not in ETF universe — skipping", ticker)
            skipped.append(ticker)
            continue

        log.info("[%d/%d] %s (%s)...", i, len(targets), ticker, fund_name or "custom")
        try:
            df = fetch_etf_holdings(ticker, fund_name)
            if df is not None:
                frames.append(df)
            else:
                failed.append(ticker)
        except Exception as e:
            log.error("[%s] FAILED: %s", ticker, e)
            failed.append(ticker)

        time.sleep(REQUEST_DELAY)

    if failed:
        log.warning("Failed (%d): %s", len(failed), ", ".join(failed))
    if skipped:
        log.info("Skipped (%d): %s", len(skipped), ", ".join(skipped))

    return frames


def write_to_iceberg(all_data: list[pd.DataFrame]) -> int:
    """Write ETF holdings to Iceberg, overwriting each fund_ticker partition."""
    import pyarrow as pa
    from pyiceberg.catalog import load_catalog
    from pyiceberg.expressions import EqualTo

    if not all_data:
        log.warning("[Iceberg] No data to write.")
        return 0

    combined = pd.concat(all_data, ignore_index=True)
    log.info("[Iceberg] Writing %d rows across %d ETFs...",
             len(combined), combined["fund_ticker"].nunique())

    try:
        catalog = load_catalog(
            "constituents",
            type="sql",
            uri=f"sqlite:///{CATALOG_DB.as_posix()}",
            warehouse=f"file://{ICEBERG_WAREHOUSE.as_posix()}",
        )
        table = catalog.load_table("constituents.etf_holdings")
    except Exception as e:
        log.error("[Iceberg] Failed to load catalog/table: %s", e)
        return 0

    df = combined.copy()
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"], errors="coerce").dt.date
    df["fetched_at"] = pd.Timestamp.now(tz="UTC")

    arrow_schema = pa.schema([
        pa.field("snapshot_date", pa.date32(), nullable=False),
        pa.field("fund_ticker", pa.string(), nullable=False),
        pa.field("fund_name", pa.string(), nullable=True),
        pa.field("holding_ticker", pa.string(), nullable=True),
        pa.field("holding_name", pa.string(), nullable=True),
        pa.field("weight_pct", pa.float64(), nullable=True),
        pa.field("sector", pa.string(), nullable=True),
        pa.field("market_cap", pa.float64(), nullable=True),
        pa.field("piotroski_f", pa.int64(), nullable=True),
        pa.field("altman_z", pa.float64(), nullable=True),
        pa.field("source", pa.string(), nullable=False),
        pa.field("fetched_at", pa.timestamp("us", tz="UTC"), nullable=False),
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
    for fund_ticker in sorted(df["fund_ticker"].unique()):
        try:
            fund_df = arrow_table.filter(
                pa.compute.equal(arrow_table.column("fund_ticker"), fund_ticker)
            )
            table.overwrite(fund_df, overwrite_filter=EqualTo("fund_ticker", fund_ticker))
            total_written += len(fund_df)
            log.info("[Iceberg]   %s: %d rows written", fund_ticker, len(fund_df))
        except Exception as e:
            log.error("[Iceberg]   %s: write FAILED: %s", fund_ticker, e)
            write_errors.append(fund_ticker)

    if write_errors:
        log.warning("[Iceberg] Write errors (%d): %s", len(write_errors), ", ".join(write_errors))

    if total_written > 0:
        log.info("[Iceberg] Partial written: %d rows",
                 total_written)

        try:
            import duckdb
            result = duckdb.sql(
                f"SELECT count(*) FROM read_parquet("
                f"'{ICEBERG_WAREHOUSE.as_posix()}/constituents/etf_holdings/**/*.parquet', "
                f"hive_partitioning=true)"
            ).fetchone()
            log.info("[Iceberg] Total rows in etf_holdings: %d", result[0])
        except Exception as e:
            log.warning("[Iceberg] Verification query failed: %s", e)

    return total_written


def main(
    etf_tickers: list[str] | None = None,
    limit: int | None = None,
):
    log.info("=" * 60)
    log.info("ETF Holdings Pipeline (SecuritiesDB) — %s", SNAPSHOT_DATE)
    log.info("=" * 60)

    if etf_tickers:
        log.info("Using %d custom tickers", len(etf_tickers))
    else:
        log.info("Using default universe: %d ETFs", len(DEFAULT_ETF_UNIVERSE))

    frames = fetch_all_etf_holdings(only_tickers=etf_tickers, limit=limit)

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        log.info("-" * 60)
        log.info("TOTAL: %d rows across %d ETFs",
                 len(combined), combined["fund_ticker"].nunique())
        for ft, count in combined.groupby("fund_ticker").size().items():
            log.info("  %-10s %d holdings", ft, count)
        log.info("-" * 60)
    else:
        log.warning("No data fetched.")
        return

    write_to_iceberg(frames)
    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ETF Holdings Pipeline — SecuritiesDB (free, no auth)"
    )
    parser.add_argument(
        "--etf-tickers",
        nargs="+",
        default=None,
        help="Specific ETF tickers to fetch (default: all in DEFAULT_ETF_UNIVERSE).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of ETFs to fetch (for testing).",
    )
    args = parser.parse_args()
    main(etf_tickers=args.etf_tickers, limit=args.limit)
