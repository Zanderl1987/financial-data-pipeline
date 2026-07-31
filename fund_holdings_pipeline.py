#!/usr/bin/env python3
"""
Fund Holdings Pipeline:
  Fetches ETF holdings (BlackRock varnish API, equity + fixed-income) and
  mutual fund holdings (EdgarTools N-PORT).
  Writes to Iceberg table: constituents.fund_holdings

  Data sources:
    1. BlackRock varnish API — iShares equity ETF holdings (XML Spreadsheet)
    2. BlackRock varnish API — iShares bond ETF holdings (AGG/LQD/HYG/TIP;
       different Holdings worksheet shape — no Ticker column, bond-specific
       fields instead: par_value, maturity_date, coupon_pct, duration, ytm_pct)
    3. EdgarTools — SEC N-PORT filings for mutual funds

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
    "IVW":  {"pid": "239725", "name": "iShares S&P 500 Growth ETF"},
    "IUSV": {"pid": "239715", "name": "iShares Core S&P US Value ETF"},
    "IUSG": {"pid": "239713", "name": "iShares Core S&P US Growth ETF"},
    # US Equity — Mid/Small Cap
    "IJH":  {"pid": "239763", "name": "iShares Core S&P Mid-Cap ETF"},
    "IJR":  {"pid": "239774", "name": "iShares Core S&P Small-Cap ETF"},
    "IWR":  {"pid": "239718", "name": "iShares Russell Mid-Cap ETF"},
    # US Equity — Small Cap Style
    "IWM":  {"pid": "239710", "name": "iShares Russell 2000 ETF"},
    "IWO":  {"pid": "239709", "name": "iShares Russell 2000 Growth ETF"},
    "IWN":  {"pid": "239712", "name": "iShares Russell 2000 Value ETF"},
    # US Equity — Factor
    "USMV": {"pid": "239695", "name": "iShares MSCI USA Min Vol Factor ETF"},
    "QUAL": {"pid": "256101", "name": "iShares MSCI USA Quality Factor ETF"},
    "MTUM": {"pid": "251614", "name": "iShares MSCI USA Momentum Factor ETF"},
    "SIZE": {"pid": "251465", "name": "iShares MSCI USA Size Factor ETF"},
    "VLUE": {"pid": "251616", "name": "iShares MSCI USA Value Factor ETF"},
    # US Sector
    "IYF":  {"pid": "239508", "name": "iShares US Financials ETF"},
    "IYW":  {"pid": "239522", "name": "iShares US Technology ETF"},
    "IBB":  {"pid": "239699", "name": "iShares Biotechnology ETF"},
    "IGV":  {"pid": "239771", "name": "iShares Expanded Tech-Software Sector ETF"},
    "ITA":  {"pid": "239502", "name": "iShares US Aerospace & Defense ETF"},
    "IHI":  {"pid": "239516", "name": "iShares US Medical Devices ETF"},
    "SOXX": {"pid": "239705", "name": "iShares PHLX Semiconductor Sector ETF"},
    # US Dividend
    "DVY":  {"pid": "239500", "name": "iShares Select Dividend ETF"},
    "DGRO": {"pid": "264623", "name": "iShares Core Dividend Growth ETF"},
    "HDV":  {"pid": "239563", "name": "iShares Core High Dividend ETF"},
    # International
    "IEFA": {"pid": "244049", "name": "iShares Core MSCI EAFE ETF"},
    "IEMG": {"pid": "244050", "name": "iShares Core MSCI Emerging Markets ETF"},
    "EFA":  {"pid": "239623", "name": "iShares MSCI EAFE ETF"},
    "EEM":  {"pid": "239637", "name": "iShares MSCI Emerging Markets ETF"},
    "ACWI": {"pid": "239600", "name": "iShares MSCI ACWI ETF"},
    "IXUS": {"pid": "244048", "name": "iShares Core MSCI Total International Stock ETF"},
    "SCZ":  {"pid": "239627", "name": "iShares MSCI EAFE Small-Cap ETF"},
    "IDV":  {"pid": "239499", "name": "iShares International Select Dividend ETF"},
    # ESG
    "ESGU": {"pid": "286007", "name": "iShares ESG Aware MSCI USA ETF"},
    "ESGD": {"pid": "283778", "name": "iShares ESG Aware MSCI EAFE ETF"},
    "ESGE": {"pid": "283777", "name": "iShares ESG Aware MSCI Emerging Markets ETF"},
    "SUSA": {"pid": "239692", "name": "iShares MSCI USA ESG Select ETF"},
    "DSI":  {"pid": "239667", "name": "iShares MSCI KLD 400 Social ETF"},
    # Multi-Asset
    "AOR":  {"pid": "239756", "name": "iShares Core Growth Allocation ETF"},
    "AOM":  {"pid": "239765", "name": "iShares Core Moderate Allocation ETF"},
    "AOA":  {"pid": "239729", "name": "iShares Core Aggressive Allocation ETF"},
    "AOK":  {"pid": "239733", "name": "iShares Core Conservative Allocation ETF"},
    # Commodities & Real Estate
    "IAU":  {"pid": "239561", "name": "iShares Gold Trust"},
    "SLV":  {"pid": "239855", "name": "iShares Silver Trust"},
    "REET": {"pid": "268752", "name": "iShares Global REIT ETF"},
    # Short Duration
    "SHV":  {"pid": "239466", "name": "iShares Short Treasury Bond ETF"},
    "NEAR": {"pid": "239854", "name": "iShares Short Duration Bond Active ETF"},
}

# Fixed-income iShares ETFs — same BlackRock varnish API, but a different
# Holdings worksheet shape: no Ticker column at all (bonds are identified by
# Name only), and bond-specific fields (Duration, YTM, Maturity, Coupon, ...)
# instead of a Ticker/Sector-only equity layout. Needs fetch_blackrock_bond_
# holdings() rather than fetch_blackrock_etf_holdings().
BOND_ETF_PID_MAP = {
    "AGG": {"pid": "239458", "name": "iShares Core U.S. Aggregate Bond ETF"},
    "LQD": {"pid": "239566", "name": "iShares iBoxx $ Investment Grade Corporate Bond ETF"},
    "HYG": {"pid": "239565", "name": "iShares iBoxx $ High Yield Corporate Bond ETF"},
    "TIP": {"pid": "239467", "name": "iShares TIPS Bond ETF"},
}

# Mutual funds to fetch via EdgarTools N-PORT
MUTUAL_FUND_UNIVERSE = {
    # --- Vanguard ---
    "VFIAX": "Vanguard 500 Index Fund",
    "VTSAX": "Vanguard Total Stock Market Index Fund",
    "VTIAX": "Vanguard Total International Stock Index Fund",
    "VBTLX": "Vanguard Total Bond Market Index Fund",
    "VGSLX": "Vanguard Real Estate Index Fund",
    "VWUSX": "Vanguard U.S. Growth Fund",
    "VWELX": "Vanguard Wellington Fund",
    "VWINX": "Vanguard Wellesley Income Fund",
    "VIGAX": "Vanguard Growth Index Fund",
    "VVIAX": "Vanguard Value Index Fund",
    "VIMAX": "Vanguard Mid-Cap Index Fund",
    "VSMAX": "Vanguard Small-Cap Index Fund",
    "VTMGX": "Vanguard Developed Markets Index Fund",
    "VEMAX": "Vanguard Emerging Markets Stock Index Fund",
    "VAIPX": "Vanguard Inflation-Protected Securities Fund",
    "VWIAX": "Vanguard Wellesley Income Fund Admiral",
    "VWENX": "Vanguard Wellington Fund Admiral",
    "VHCAX": "Vanguard Capital Appreciation Fund",
    "VTWNX": "Vanguard Target Retirement 2020 Fund",
    "VFIFX": "Vanguard Target Retirement 2050 Fund",
    "VMVAX": "Vanguard Mid-Cap Value Index Fund",
    # --- Fidelity ---
    "FXAIX": "Fidelity 500 Index Fund",
    "FSKAX": "Fidelity Total Market Index Fund",
    "FTIHX": "Fidelity Total International Index Fund",
    "FBALX": "Fidelity Blue Chip Growth Fund",
    "FCNTX": "Fidelity Contrafund",
    "FSPGX": "Fidelity Large Cap Growth Index Fund",
    "FSMDX": "Fidelity Mid Cap Index Fund",
    "FSSNX": "Fidelity Small Cap Index Fund",
    "FLPSX": "Fidelity Low-Priced Stock Fund",
    "FFNOX": "Fidelity Four-in-One Index Fund",
    "FZROX": "Fidelity ZERO Total Market Index Fund",
    "FNCMX": "Fidelity Nasdaq Composite Index Fund",
    # --- Schwab ---
    "SWPPX": "Schwab S&P 500 Index Fund",
    "SWTSX": "Schwab Total Stock Market Index Fund",
    "SWISX": "Schwab International Index Fund",
    "SWAGX": "Schwab U.S. Aggregate Bond Index Fund",
    "SWSSX": "Schwab Small-Cap Index Fund",
    # --- PIMCO ---
    "PTTDX": "PIMCO Total Return Fund Institutional",
    "PONAX": "PIMCO Income Fund A",
    "PRRIX": "PIMCO Real Return Fund Institutional",
    # --- American Funds ---
    "AGTHX": "American Funds Growth Fund of America A",
    "AIVSX": "American Funds Investment Company of America A",
    "ANWPX": "American Funds New Perspective Fund A",
    "CWGIX": "American Funds Capital World Growth and Income A",
    "SMCWX": "American Funds Small Cap World Fund A",
    # --- T. Rowe Price ---
    "PRGFX": "T. Rowe Price Growth Stock Fund",
    "PRWCX": "T. Rowe Price Capital Appreciation Fund",
    "PRNHX": "T. Rowe Price New Horizons Fund",
    "PRMTX": "T. Rowe Price Mid-Cap Growth Fund",
    # --- Dimensional (DFA) ---
    "DFUVX": "DFA US Vector Equity Fund Institutional",
    "DFVEX": "DFA US Targeted Value Fund Institutional",
    "DFEMX": "DFA Emerging Markets Core Equity Fund Institutional",
}


def _fetch_blackrock_holdings_rows(ticker: str, pid: str) -> list:
    """Fetch a fund's Holdings worksheet from the BlackRock varnish API and
    return it as a list of non-empty rows (each a list of cell strings)."""
    log.info("[%s] Fetching from BlackRock (pid=%s)...", ticker, pid)

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
    return rows_data


def fetch_blackrock_etf_holdings(ticker: str) -> pd.DataFrame:
    """Fetch equity ETF holdings from BlackRock varnish API."""
    info = ETF_PID_MAP[ticker]
    fund_name = info["name"]
    rows_data = _fetch_blackrock_holdings_rows(ticker, info["pid"])

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


def fetch_blackrock_bond_holdings(ticker: str) -> pd.DataFrame:
    """Fetch fixed-income ETF holdings from BlackRock varnish API.

    Bond funds' Holdings worksheet has no Ticker column (bonds are identified
    by Name only) and carries bond-specific fields (Duration, YTM, Maturity,
    Coupon, ...) instead of the equity layout's Ticker/Sector columns.
    """
    info = BOND_ETF_PID_MAP[ticker]
    fund_name = info["name"]
    rows_data = _fetch_blackrock_holdings_rows(ticker, info["pid"])

    # Find header row (bond sheets have no "ticker" column, so key off Name +
    # Weight + a bond-specific field instead)
    header_idx = None
    for i, row in enumerate(rows_data):
        row_text = " ".join(str(c).lower() for c in row if c)
        if "name" in row_text and "weight" in row_text and "maturity" in row_text:
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError(f"Could not find header row for {ticker}")

    headers_row = [str(c).strip() for c in rows_data[header_idx]]
    data_rows = rows_data[header_idx + 1:]
    df = pd.DataFrame(data_rows, columns=headers_row[: len(headers_row)])

    name_col = next((c for c in df.columns if c.lower() == "name"), df.columns[0])
    sector_col = next((c for c in df.columns if c.lower() == "sector"), None)
    asset_class_col = next((c for c in df.columns if "asset class" in c.lower()), None)
    weight_col = next((c for c in df.columns if "weight" in c.lower()), None)
    market_value_col = next((c for c in df.columns if c.lower() == "market value"), None)
    par_value_col = next((c for c in df.columns if c.lower() == "par value"), None)
    location_col = next((c for c in df.columns if c.lower() == "location"), None)
    maturity_col = next((c for c in df.columns if c.lower() == "maturity"), None)
    coupon_col = next((c for c in df.columns if "coupon" in c.lower()), None)
    duration_col = next((c for c in df.columns if c.lower() == "duration"), None)
    ytm_col = next((c for c in df.columns if c.lower() == "ytm (%)"), None)

    # Cash/derivative sweep line has Asset Class == "Money Market" (real
    # positions are "Fixed Income"); Sector == "Cash and/or Derivatives" too.
    before = len(df)
    if asset_class_col:
        df = df[~df[asset_class_col].astype(str).str.contains(
            "money market", case=False, na=False
        )]
    if sector_col:
        df = df[~df[sector_col].astype(str).str.contains(
            "cash and/or derivatives", case=False, na=False
        )]
    dropped = before - len(df)
    if dropped > 0:
        log.info("[BOND:%s] Filtered %d non-security rows (cash, etc.)", ticker, dropped)

    maturity = pd.to_datetime(df[maturity_col], errors="coerce").dt.date if maturity_col else None

    result = pd.DataFrame({
        "snapshot_date": SNAPSHOT_DATE,
        "fund_ticker": ticker,
        "fund_name": fund_name,
        "fund_cik": None,
        "holding_ticker": None,  # bonds have no ticker in this feed
        "holding_name": df[name_col].str.strip() if name_col else None,
        "cusip": None,
        "isin": None,
        "figi": None,
        "sedol": None,
        "weight_pct": pd.to_numeric(df[weight_col], errors="coerce") if weight_col else None,
        "market_value_usd": pd.to_numeric(df[market_value_col], errors="coerce") if market_value_col else None,
        "shares_held": None,
        "asset_category": df[asset_class_col].str.strip() if asset_class_col else None,
        "sector": df[sector_col].str.strip() if sector_col else None,
        "country": df[location_col].str.strip() if location_col else None,
        "issuer_name": df[name_col].str.strip() if name_col else None,
        "filing_date": None,
        "reporting_period_end": None,
        "par_value": pd.to_numeric(df[par_value_col], errors="coerce") if par_value_col else None,
        "maturity_date": maturity,
        "coupon_pct": pd.to_numeric(df[coupon_col], errors="coerce") if coupon_col else None,
        "duration": pd.to_numeric(df[duration_col], errors="coerce") if duration_col else None,
        "ytm_pct": pd.to_numeric(df[ytm_col], errors="coerce") if ytm_col else None,
        "source": f"blackrock:{ticker}",
        "fetched_at": FETCHED_AT,
    })
    log.info("[BOND:%s] Output: %d holdings", ticker, len(result))
    return result


def fetch_all_bond_etf_holdings(only_tickers: list[str] | None = None) -> list[pd.DataFrame]:
    """Fetch holdings for fixed-income ETFs in BOND_ETF_PID_MAP."""
    targets = only_tickers if only_tickers else list(BOND_ETF_PID_MAP.keys())
    frames = []
    for ticker in targets:
        if ticker not in BOND_ETF_PID_MAP:
            log.warning("[BOND:%s] Not in BOND_ETF_PID_MAP — skipping", ticker)
            continue
        try:
            frames.append(fetch_blackrock_bond_holdings(ticker))
        except Exception as e:
            log.error("[BOND:%s] FAILED: %s", ticker, e)
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

    # Prepare DataFrame
    df = combined.copy()
    for col in ["snapshot_date", "filing_date", "reporting_period_end", "maturity_date"]:
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
        # Fixed-income fields (bond ETFs only; null for equity/mutual-fund rows)
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

    # Overwrite per fund_ticker — replace today's data for each fund.
    # Batched into a single transaction so one pipeline run produces one new
    # metadata.json version instead of one per fund (was ~85/run -- the cause
    # of storage/iceberg/constituents/fund_holdings/metadata growing to 467+
    # files after only a handful of runs).
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
        log.info("[Iceberg] Partial written: %d rows",
                 total_written)

        # Verify
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(
    skip_etf: bool = False,
    skip_mf: bool = False,
    skip_bonds: bool = False,
    etf_tickers: list[str] | None = None,
    mf_tickers: list[str] | None = None,
    bond_tickers: list[str] | None = None,
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

    # Fixed-income ETF holdings via BlackRock (separate parser, see BOND_ETF_PID_MAP)
    if not skip_bonds:
        log.info("--- Bond ETF Holdings (BlackRock varnish API) ---")
        frames.extend(fetch_all_bond_etf_holdings(only_tickers=bond_tickers))
    else:
        log.info("--- Skipping bond ETF holdings ---")

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
        "--skip-bonds",
        action="store_true",
        help="Skip bond ETF holdings (BlackRock varnish API, BOND_ETF_PID_MAP).",
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
    parser.add_argument(
        "--bond-tickers",
        nargs="+",
        default=None,
        help="Specific bond ETF tickers to fetch (default: all in BOND_ETF_PID_MAP).",
    )
    args = parser.parse_args()
    main(
        skip_etf=args.skip_etf,
        skip_mf=args.skip_mf,
        skip_bonds=args.skip_bonds,
        etf_tickers=args.etf_tickers,
        mf_tickers=args.mf_tickers,
        bond_tickers=args.bond_tickers,
    )
