#!/usr/bin/env python3
"""
J.P. Morgan ETF Holdings Pipeline:
  Daily holdings for J.P. Morgan Asset Management ETFs from the issuer's own
  "download holdings" XLSX. Writes to Iceberg table constituents.fund_holdings
  with source='jpmorgan:<TICKER>'.

  Data source (keyless, no auth; verified 2026-11-13):
    GET https://am.jpmorgan.com/FundsMarketingHandler/excel
        ?type=dailyETFHoldings&cusip={fund_cusip}&country=us&role=adv&locale=en-US
  robots.txt disallows only the /asset-management/ page trees, not this handler.

  Layout: title row ("Holdings | <fund> - <cusip> | As of Date: MM/DD/YYYY"),
  then a header row starting "Ticker", then holdings, then a disclaimer footer.
  The header row is located by content, not position. snapshot_date is the
  file's own as-of date.

  All holding rows are kept, including money-market sweep and option/ELN rows
  (JEPI/JEPQ's income comes from them); `asset_category` carries the issuer's
  Security Type so consumers can filter.

  Added 2026-11-13 when securitiesdb.com (etf_holdings_pipeline.py's source)
  began redirecting every URL to itself.

  CLI:
    python jpmorgan_holdings_pipeline.py                  # all funds
    python jpmorgan_holdings_pipeline.py --tickers JEPI   # subset

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))

from vanguard_bond_holdings_pipeline import log, write_to_iceberg  # noqa: E402

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
URL = ("https://am.jpmorgan.com/FundsMarketingHandler/excel"
       "?type=dailyETFHoldings&cusip={cusip}&country=us&role=adv&locale=en-US")
REQUEST_GAP = 1.0

# ticker -> (fund CUSIP, fund name)
JPM_FUNDS = {
    "JEPI": ("46641Q332", "JPMorgan Equity Premium Income ETF"),
    "JEPQ": ("46654Q203", "JPMorgan Nasdaq Equity Premium Income ETF"),
    "JPST": ("46641Q837", "JPMorgan Ultra-Short Income ETF"),
}

_AS_OF_RE = re.compile(r"As of Date:\s*(\d{1,2}/\d{1,2}/\d{4})", re.I)


def fetch_xlsx(ticker: str) -> bytes:
    cusip, _ = JPM_FUNDS[ticker]
    r = requests.get(URL.format(cusip=cusip), headers=HEADERS, timeout=60)
    r.raise_for_status()
    if not r.content.startswith(b"PK"):
        raise RuntimeError(f"[{ticker}] not an XLSX (content-type {r.headers.get('Content-Type')})")
    return r.content


def parse_holdings(ticker: str, content: bytes) -> pd.DataFrame:
    raw = pd.read_excel(BytesIO(content), header=None)

    header_idx = next(
        (i for i in range(min(20, len(raw)))
         if str(raw.iloc[i, 0]).strip().lower() == "ticker"
         and "security description" in {str(c).strip().lower() for c in raw.iloc[i]}),
        None,
    )
    if header_idx is None:
        raise RuntimeError(f"[{ticker}] no 'Ticker' header row in the first 20 rows")

    as_of = None
    for i in range(header_idx):
        for cell in raw.iloc[i]:
            m = _AS_OF_RE.search(str(cell))
            if m:
                as_of = datetime.strptime(m.group(1), "%m/%d/%Y").date()
    if as_of is None:
        raise RuntimeError(f"[{ticker}] no 'As of Date' above the header row")

    df = raw.iloc[header_idx + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[header_idx]]
    # Holding rows have a description AND a market value; blank spacer rows and
    # the disclaimer footer have neither.
    mv = pd.to_numeric(df["Market Value (USD)"], errors="coerce")
    df = df[df["Security Description"].notna() & mv.notna()]
    if df.empty:
        raise RuntimeError(f"[{ticker}] header found but no holding rows")

    def num(col):
        return pd.to_numeric(df[col].astype(str).str.replace(r"[%,$]", "", regex=True),
                             errors="coerce")

    def text(col):
        s = df[col].astype(str).str.strip()
        return s.where(~s.isin(["nan", "NaN", "None", ""]), None)

    out = pd.DataFrame({
        "snapshot_date": as_of,
        "fund_ticker": ticker,
        "fund_name": JPM_FUNDS[ticker][1],
        "fund_cik": None,
        "holding_ticker": text("Ticker"),
        "holding_name": text("Security Description"),
        "cusip": None,
        "isin": None,
        "figi": None,
        "sedol": None,
        "weight_pct": num("% of Net Assets"),
        "market_value_usd": num("Market Value (USD)"),
        "shares_held": num("Shares/Par"),
        "asset_category": text("Security Type"),
        "sector": text("Sector"),
        "country": text("Country"),
        "issuer_name": text("Security Description"),
        "filing_date": None,
        "reporting_period_end": None,
        "source": f"jpmorgan:{ticker}",
        "fetched_at": datetime.now(timezone.utc),
        "par_value": None,
        "maturity_date": pd.to_datetime(df["Maturity Date"], format="%m/%d/%Y", errors="coerce").dt.date,
        "coupon_pct": num("Coupon"),
        "duration": None,
        "ytm_pct": None,
    })
    log.info("[%s] %d holdings as of %s (weight sum %.1f%%)",
             ticker, len(out), as_of, out["weight_pct"].sum())
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="J.P. Morgan ETF daily holdings")
    parser.add_argument("--tickers", nargs="+", default=list(JPM_FUNDS))
    args = parser.parse_args()

    frames, failed = [], []
    for t in [x.upper() for x in args.tickers]:
        if t not in JPM_FUNDS:
            log.warning("Unknown ticker ignored: %s", t)
            continue
        try:
            frames.append(parse_holdings(t, fetch_xlsx(t)))
        except Exception as e:
            log.error("[%s] FAILED: %s", t, e)
            failed.append(t)
        time.sleep(REQUEST_GAP)

    if failed:
        log.warning("Failed (%d): %s", len(failed), ", ".join(failed))
    written = write_to_iceberg(frames)
    # Non-zero when nothing landed, so run_all shows FAIL rather than a silent PASS.
    if written == 0:
        log.error("No rows written.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
