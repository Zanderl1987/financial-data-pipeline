#!/usr/bin/env python3
"""
SSGA SPDR Holdings Pipeline:
  Fetches daily fund holdings for State Street SPDR ETFs from the official
  issuer website (www.ssga.com) as XLSX files. No auth, no rate limit docs.
  Writes to Iceberg table: constituents.fund_holdings (source='ssga:<TICKER>')

  Data source:
    GET https://www.ssga.com/library-content/products/fund-data/etfs/us/
        holdings-daily-us-en-{ticker}.xlsx
    Sheet 'holdings'; header row is row 4 (0-indexed); data rows follow.
    Row 2 carries the fund's holdings as-of date ("Holdings: As of 31-Aug-2026").

  Two layouts coexist:
    - Equity funds:  Name, Ticker, Identifier (CUSIP), SEDOL, Weight,
                     Sector, Shares Held, Local Currency
    - Bond funds:    Name, Identifier, SEDOL, Weight, Coupon, Par Value,
                     Market Value, Local Currency, [Maturity]  (no Ticker col)
  Layout is detected from the header row, so new funds need no code changes.

  Known gaps:
    - GLD (SPDR Gold Shares) is NOT served by this endpoint (404, JS-only
      product page) -- commodity trusts report bullion elsewhere.
    - Only US-domiciled SPDR ETFs use this URL pattern.

  CLI:
    python ssga_holdings_pipeline.py                    # default universe (27 SPDRs)
    python ssga_holdings_pipeline.py --tickers SPY XLE  # specific tickers
    python ssga_holdings_pipeline.py --backfill         # same as default (snapshot)

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import io
import re
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

FETCHED_AT = datetime.now(timezone.utc)

# Rate limits / retry (SSGA has served 404 HTML challenge pages under rapid
# request bursts; retry once after a pause before declaring a fund missing).
SSGA_SLEEP = 0.75
SSGA_RETRY_PAUSE = 8.0
SSGA_MAX_ATTEMPTS = 2

BASE_URL = (
    "https://www.ssga.com/library-content/products/fund-data/etfs/us/"
    "holdings-daily-us-en-{ticker}.xlsx"
)

# Non-security row filters (same spirit as the BlackRock pipeline)
NON_SECURITY_NAME_RE = re.compile(
    r"Cash|Derivative|Futures|Option|Swap\b", re.IGNORECASE
)

# State Street SPDR US-listed equity ETFs (verified 200 + parseable 2026-09-01).
SSGA_EQUITY_UNIVERSE = {
    # Broad market / style
    "SPY":  "SPDR S&P 500 ETF Trust",
    "DIA":  "SPDR Dow Jones Industrial Average ETF",
    "SPLG": "SPDR Portfolio S&P 500 ETF",
    "SPTM": "SPDR Total Stock Market ETF",
    "SPYG": "SPDR Portfolio S&P 500 Growth ETF",
    "SPYV": "SPDR Portfolio S&P 500 Value ETF",
    "SPYD": "SPDR Portfolio S&P 500 High Dividend ETF",
    "SDY":  "SPDR S&P Dividend ETF",
    # U.S. sector
    "XLE":  "Energy Select Sector SPDR Fund",
    "XLK":  "Technology Select Sector SPDR Fund",
    "XLF":  "Financial Select Sector SPDR Fund",
    "XLV":  "Health Care Select Sector SPDR Fund",
    "XLY":  "Consumer Discretionary Select Sector SPDR Fund",
    "XLI":  "Industrial Select Sector SPDR Fund",
    "XLC":  "Communication Services Select Sector SPDR Fund",
    "XLRE": "Real Estate Select Sector SPDR Fund",
    "XLP":  "Consumer Staples Select Sector SPDR Fund",
    "XLU":  "Utilities Select Sector SPDR Fund",
    "XLB":  "Materials Select Sector SPDR Fund",
    # Thematic / size
    "XBI":  "SPDR S&P Biotech ETF",
    "KRE":  "SPDR S&P Regional Banking ETF",
    "KBE":  "SPDR S&P Bank ETF",
    "XOP":  "SPDR S&P Oil & Gas Exploration & Production ETF",
    "XAR":  "SPDR S&P Aerospace & Defense ETF",
}

# State Street SPDR bond ETFs -- different Holdings layout (no Ticker col;
# Coupon/Par Value/Market Value + Maturity instead).
SSGA_BOND_UNIVERSE = {
    "BIL": "SPDR Bloomberg 1-3 Month T-Bill ETF",
    "JNK": "SPDR Bloomberg High Yield Bond ETF",
    "BWX": "SPDR Bloomberg International Treasury Bond ETF",
}


def _fetch_xlsx(ticker: str) -> bytes | None:
    """Download the daily-holdings XLSX for a fund, with one retry for the
    transient 404-HTML pages SSGA serves under bursts."""
    url = BASE_URL.format(ticker=ticker.lower())
    for attempt in range(1, SSGA_MAX_ATTEMPTS + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
        except Exception as e:
            log.error("[%s] Request error (attempt %d): %s", ticker, attempt, e)
            if attempt < SSGA_MAX_ATTEMPTS:
                time.sleep(SSGA_RETRY_PAUSE)
            continue

        if r.status_code == 200 and r.content[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
            return r.content
        if r.status_code == 404:
            # A 404 whose body is a real XLSX-reject is a genuine missing fund;
            # a 404 HTML body (e.g. 42KB of markup) is a challenge page -- retry.
            if len(r.content) < 4000 or r.content[:4] == b"PK\x03\x04":
                return None
            log.warning("[%s] 404 with HTML body (attempt %d) -- retrying...",
                        ticker, attempt)
        else:
            log.warning("[%s] HTTP %d (attempt %d)", ticker, r.status_code, attempt)
        if attempt < SSGA_MAX_ATTEMPTS:
            time.sleep(SSGA_RETRY_PAUSE)
    return None


def _parse_holdings_sheet(ticker: str, content: bytes):
    """Parse the 'holdings' sheet. Returns (snapshot_date, header, frame) or
    raises ValueError with a clear message."""
    xl = pd.ExcelFile(io.BytesIO(content))
    sheet = "holdings" if "holdings" in xl.sheet_names else xl.sheet_names[0]
    full = xl.parse(sheet, header=None)
    if full.shape[0] < 6:
        raise ValueError(f"{ticker}: sheet too small ({full.shape})")

    # As-of date from row 2 ("Holdings:  | As of 31-Aug-2026")
    robust = full.iloc[2].astype(str).tolist()
    asof = next((s for s in robust if "As of" in s), None)
    if asof:
        m = re.search(r"As of\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", asof)
        if m:
            try:
                snapshot_date = datetime.strptime(m.group(1), "%d-%b-%Y").date()
            except ValueError:
                snapshot_date = date.today()
        else:
            snapshot_date = date.today()
    else:
        snapshot_date = date.today()

    header_row = [str(c).strip() for c in full.iloc[4].tolist()]
    data = full.iloc[5:].reset_index(drop=True)
    data.columns = [f"c{i}" for i in range(len(header_row))]

    # Map column positions -> canonical names (fuzzy, case-insensitive)
    def _norm(s):
        return re.sub(r"[^a-z]", "", s.lower())

    colmap = {}
    for idx, h in enumerate(header_row):
        n = _norm(h)
        if not n:
            continue
        for key, pats in {
            "name":    ["name"],
            "ticker":  ["ticker"],
            "cusip":   ["identifier", "cusip"],
            "sedol":   ["sedol"],
            "weight":  ["weight"],
            "sector":  ["sector"],
            "shares":  ["sharesheld", "shareshold"],
            "currency": ["localcurrency", "currency"],
            "coupon":  ["coupon"],
            "par":     ["parvalue"],
            "mktval":  ["marketvalue"],
            "maturity": ["maturity"],
        }.items():
            if n in pats:
                colmap[key] = f"c{idx}"
                break
    if "name" not in colmap or "weight" not in colmap:
        raise ValueError(f"{ticker}: unexpected header {header_row}")

    return snapshot_date, colmap, data, header_row


def fetch_ssga_equity_holdings(ticker: str) -> pd.DataFrame:
    """Fetch a SPDR equity fund's daily holdings into the fund_holdings schema."""
    fund_name = SSGA_EQUITY_UNIVERSE[ticker]
    content = _fetch_xlsx(ticker)
    if content is None:
        raise RuntimeError(f"{ticker}: no XLSX returned for {ticker}")

    snapshot_date, colmap, data, header_row = _parse_holdings_sheet(ticker, content)

    df = data.copy()
    ticker_col = colmap.get("ticker")
    name_col = colmap["name"]
    weight_col = colmap["weight"]

    # Drop stray footer/total rows (no usable name or weight) and non-securities.
    before = len(df)
    keep_mask = df[name_col].notna()
    try:
        keep_mask &= pd.to_numeric(df[weight_col], errors="coerce").notna()
    except Exception:
        pass
    if ticker_col:
        keep_mask &= df[ticker_col].notna()
        keep_mask &= ~df[ticker_col].astype(str).str.strip().str.fullmatch("-+")
    df = df[keep_mask]
    df = df[~df[name_col].astype(str).str.contains(NON_SECURITY_NAME_RE, na=False)]
    dropped = before - len(df)
    if dropped > 0:
        log.info("[%s] Filtered %d non-security/stray rows", ticker, dropped)

    result = pd.DataFrame({
        "snapshot_date": snapshot_date,
        "fund_ticker": ticker,
        "fund_name": fund_name,
        "fund_cik": None,
        "holding_ticker": df[ticker_col].astype(str).str.strip() if ticker_col else None,
        "holding_name": df[name_col].astype(str).str.strip(),
        "cusip": df[colmap["cusip"]].astype(str).str.strip()
            if colmap.get("cusip") and colmap.get("cusip") in df.columns else None,
        "isin": None,
        "figi": None,
        "sedol": df[colmap["sedol"]].astype(str).str.strip()
            if colmap.get("sedol") and colmap.get("sedol") in df.columns else None,
        "weight_pct": pd.to_numeric(df[weight_col], errors="coerce")
            if weight_col in df.columns else None,
        "market_value_usd": None,
        "shares_held": pd.to_numeric(df[colmap["shares"]], errors="coerce")
            if colmap.get("shares") and colmap.get("shares") in df.columns else None,
        "asset_category": None,
        "sector": df[colmap["sector"]].astype(str).str.strip()
            if colmap.get("sector") and colmap.get("sector") in df.columns else None,
        "country": None,
        "issuer_name": None,
        "filing_date": None,
        "reporting_period_end": None,
        "source": f"ssga:{ticker}",
        "fetched_at": FETCHED_AT,
    })
    # Normalize CUSIP (SSGA pads to 9 chars with a trailing space) and drop
    # cells that are just placeholder dashes.
    if "cusip" in result.columns:
        result["cusip"] = result["cusip"].str.split(r"\s+").str[0]
        result.loc[result["cusip"].str.fullmatch(r"-+", na=False), "cusip"] = None
        result.loc[result["cusip"] == "nan", "cusip"] = None
    result.loc[result["holding_ticker"].astype(str).str.fullmatch(r"-+", na=False), "holding_ticker"] = None
    result = result[result["holding_ticker"].notna() & (result["holding_ticker"] != "")]

    log.info("[%s] Output: %d holdings (as of %s)", ticker, len(result), snapshot_date)
    return result


def fetch_ssga_bond_holdings(ticker: str) -> pd.DataFrame:
    """Fetch a SPDR bond fund's daily holdings (Coupon/Par/Maturity layout)."""
    fund_name = SSGA_BOND_UNIVERSE[ticker]
    content = _fetch_xlsx(ticker)
    if content is None:
        raise RuntimeError(f"{ticker}: no XLSX returned for {ticker}")

    snapshot_date, colmap, data, header_row = _parse_holdings_sheet(ticker, content)

    df = data.copy()
    name_col = colmap["name"]
    weight_col = colmap["weight"]

    before = len(df)
    keep_mask = df[name_col].notna()
    keep_mask &= pd.to_numeric(df[weight_col], errors="coerce").notna()
    df = df[keep_mask]
    dropped = before - len(df)
    if dropped > 0:
        log.info("[%s] Filtered %d stray rows", ticker, dropped)

    maturity = None
    if colmap.get("maturity") and colmap["maturity"] in df.columns:
        maturity = pd.to_datetime(df[colmap["maturity"]], errors="coerce").dt.date

    result = pd.DataFrame({
        "snapshot_date": snapshot_date,
        "fund_ticker": ticker,
        "fund_name": fund_name,
        "fund_cik": None,
        "holding_ticker": None,  # bond layout has no Ticker col
        "holding_name": df[name_col].astype(str).str.strip(),
        "cusip": df[colmap["cusip"]].astype(str).str.split(r"\s+").str[0]
            if colmap.get("cusip") and colmap["cusip"] in df.columns else None,
        "isin": None,
        "figi": None,
        "sedol": df[colmap["sedol"]].astype(str).str.strip()
            if colmap.get("sedol") and colmap["sedol"] in df.columns else None,
        "weight_pct": pd.to_numeric(df[weight_col], errors="coerce"),
        "market_value_usd": pd.to_numeric(df[colmap["mktval"]], errors="coerce")
            if colmap.get("mktval") and colmap["mktval"] in df.columns else None,
        "shares_held": None,
        "asset_category": None,
        "sector": None,
        "country": None,
        "issuer_name": None,
        "filing_date": None,
        "reporting_period_end": None,
        "par_value": pd.to_numeric(df[colmap["par"]], errors="coerce")
            if colmap.get("par") and colmap["par"] in df.columns else None,
        "maturity_date": maturity,
        "coupon_pct": pd.to_numeric(df[colmap["coupon"]], errors="coerce")
            if colmap.get("coupon") and colmap["coupon"] in df.columns else None,
        "duration": None,
        "ytm_pct": None,
        "source": f"ssga:{ticker}",
        "fetched_at": FETCHED_AT,
    })
    # Drop the placeholder-dash CUSIPs and any NaN names
    if "cusip" in result.columns:
        result.loc[result["cusip"].astype(str).str.fullmatch(r"-+", na=False), "cusip"] = None
        result.loc[result["cusip"] == "nan", "cusip"] = None
    result = result[result["holding_name"].notna() & (result["holding_name"] != "")]

    log.info("[%s] Output: %d holdings (as of %s)", ticker, len(result), snapshot_date)
    return result


def fetch_all_ssga_holdings(only_tickers: list[str] | None = None) -> list[pd.DataFrame]:
    """Fetch holdings for all SPDR funds in the SSGA universe."""
    targets = only_tickers if only_tickers else sorted(list(SSGA_EQUITY_UNIVERSE) + list(SSGA_BOND_UNIVERSE))
    frames = []
    failed = []
    for i, ticker in enumerate(targets, 1):
        ticker = ticker.upper()
        if ticker in SSGA_EQUITY_UNIVERSE:
            log.info("[%d/%d] %s (%s)...", i, len(targets), ticker, SSGA_EQUITY_UNIVERSE[ticker])
            try:
                frames.append(fetch_ssga_equity_holdings(ticker))
            except Exception as e:
                log.error("[%s] FAILED: %s", ticker, e)
                failed.append(ticker)
        elif ticker in SSGA_BOND_UNIVERSE:
            log.info("[%d/%d] %s (%s)...", i, len(targets), ticker, SSGA_BOND_UNIVERSE[ticker])
            try:
                frames.append(fetch_ssga_bond_holdings(ticker))
            except Exception as e:
                log.error("[%s] FAILED: %s", ticker, e)
                failed.append(ticker)
        else:
            log.warning("[%s] Not in SSGA universe -- skipping", ticker)
            continue
        time.sleep(SSGA_SLEEP)
    if failed:
        log.warning("Failed (%d): %s", len(failed), ", ".join(failed))
    return frames


def write_to_iceberg(all_data: list[pd.DataFrame]) -> int:
    """Write SSGA holdings into the shared constituents.fund_holdings table.

    Reuses fund_holdings_pipeline.write_to_iceberg(), which overwrites each
    fund_ticker partition. SPDR fund tickers don't collide with BlackRock
    (IVV/AGG/...) or EdgarTools N-PORT (VFIAX/...) fund tickers, so the rows
    coexist under distinct source tags ('ssga:<TICKER>').
    """
    from fund_holdings_pipeline import write_to_iceberg as _fh_write
    return _fh_write(all_data)


def main(tickers: list[str] | None = None):
    log.info("=" * 60)
    log.info("SSGA SPDR Holdings Pipeline (State Street) — %s", date.today())
    log.info("=" * 60)

    frames = fetch_all_ssga_holdings(only_tickers=tickers)

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        log.info("-" * 60)
        log.info("TOTAL: %d rows across %d funds", len(combined), combined["fund_ticker"].nunique())
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
        description="SSGA SPDR Holdings Pipeline — State Street (keyless XLSX)"
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="Specific SPDR tickers to fetch (default: all in SSGA universe).",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Accepted for run_all compatibility (SSGA is a daily snapshot source).",
    )
    args = parser.parse_args()
    main(tickers=args.tickers)