#!/usr/bin/env python3
"""
Gold Bar Holdings Pipeline:
  Fetches the daily allocated-gold bar list for GLD (SPDR Gold Shares) and
  SGOL (abrdn Physical Gold Shares ETF) -- the two remaining ETF-holdings
  gap tickers (see work-notes TODO.md). Both are physical bullion grantor
  trusts with no N-PORT filing and no securities portfolio: the custodian's
  bar list (one row per physical gold bar, identified by serial number) IS
  the trust's holdings.
  Writes to Iceberg table: constituents.fund_holdings (source='gld_barlist'
  / 'sgol_barlist')

  Data sources (both keyless, no WAF/rate limiting observed):
    - GLD:  custodian JPMorgan Chase Bank N.A. publishes a "Bullion
      Weightlist" PDF, linked from the SPDR product page
      (https://www.spdrgoldshares.com/usa/gld/) -- scraped dynamically each
      run since the URL is server-rendered in that page's static HTML.
      https://emea-markets.jpmorgan.com/metalicsWebAppJanus/
      publicUnauthenticated/SPDR_GOLD_TRUST_JPM_BARLIST.pdf
    - SGOL: custodian ICBC Standard Bank PLC publishes a bar list PDF
      linked from the fund's "Literature" tab. That tab is populated by a
      client-side XHR after Next.js hydration (not present in the page's
      static HTML -- confirmed via direct curl of the ?tab=literatureTab
      URL), so the URL below is hardcoded from a one-time browser-rendered
      discovery rather than scraped fresh each run. Filename carries no
      date/hash, so it should stay stable; if this ever 404s, re-discover
      via a real browser on the fund's Literature tab and update the
      constant.
      https://phoenix.icbcstandard.com/assets/public/barlists/
      Abrdn%20ETF%20Gold%20Bar%20List.pdf

  Both PDFs are parsed with PyMuPDF's plain get_text() (not find_tables() --
  neither PDF has bordered table grids, just column-ordered text flow) into
  fixed-width field groups, with page letterhead/header/footer lines
  filtered by exact literal match before grouping. Both parsers were
  validated against the PDFs' own printed totals (bar count + gross/fine
  troy ounces) before being trusted -- see SESSION_NOTES for the reconciled
  numbers.

  Schema mapping notes (no existing fund_holdings column fits a physical
  bar cleanly, so field reuse is documented here rather than inventing new
  columns for two tickers):
    - shares_held    = fine troy ounces of that bar (NOT a share count).
    - weight_pct      = that bar's fine oz / trust total fine oz * 100.
    - holding_name    = "Gold Bar #<serial>".
    - issuer_name     = refiner/brand name.
    - country         = GLD: the storage vault's country (bar-level vault
                        field). SGOL: the refiner's country (bar-level
                        brand field has no per-bar storage location).
    - asset_category  = "Physical Gold Bullion".
    - market_value_usd = left null. No per-bar USD valuation is published;
                        join against metals_pipeline's gold spot price
                        downstream if a dollar figure is needed.
    - cusip/isin/figi/sedol = null (bars have no security identifier).

  CLI:
    python gold_bar_holdings_pipeline.py               # both GLD and SGOL
    python gold_bar_holdings_pipeline.py --tickers GLD  # one ticker
    python gold_bar_holdings_pipeline.py --backfill     # same as default (snapshot)

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import re
import sys
import logging
import argparse
from datetime import date, datetime, timezone
from pathlib import Path

import requests
import pandas as pd
import fitz  # PyMuPDF

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

GLD_PRODUCT_URL = "https://www.spdrgoldshares.com/usa/gld/"
GLD_BARLIST_RE = re.compile(
    r'https://emea-markets\.jpmorgan\.com/metalicsWebAppJanus/'
    r'publicUnauthenticated/[A-Za-z0-9_%]+BARLIST\.pdf'
)
SGOL_BARLIST_URL = (
    "https://phoenix.icbcstandard.com/assets/public/barlists/"
    "Abrdn%20ETF%20Gold%20Bar%20List.pdf"
)

FUND_META = {
    "GLD": {"fund_name": "SPDR Gold Shares", "fund_cik": 1222333},
    "SGOL": {"fund_name": "abrdn Physical Gold Shares ETF", "fund_cik": 1450923},
}


def _download_pdf(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.content


def find_gld_barlist_url() -> str:
    """Scrape the current Bullion Weightlist PDF URL from the SPDR product page."""
    resp = requests.get(GLD_PRODUCT_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    m = GLD_BARLIST_RE.search(resp.text)
    if not m:
        raise RuntimeError(f"Could not find GLD bar list URL on {GLD_PRODUCT_URL}")
    return m.group(0)


# ---------------------------------------------------------------------------
# GLD: JPMorgan "BULLION WEIGHTLIST" PDF
# ---------------------------------------------------------------------------

_GLD_HEADER_LITERALS = {"Brand", "Bar No.", "Shape", "Gross Ounces", "Assay",
                         "Fine Ounces", "Year", "Vault"}
_GLD_VAULT_NAMES = {"JPM London V (VLT)", "JPM New York (VLN)"}
_GLD_VAULT_COUNTRY = {"JPM London V (VLT)": "United Kingdom",
                       "JPM New York (VLN)": "United States"}
_GLD_PAGE_FOOTER_RE = re.compile(r"^Page \d+ of \d+$")
_GLD_AS_AT_RE = re.compile(r"As at:(\d{2})-([A-Za-z]+)-(\d{4})")


def parse_gld_barlist(pdf_bytes: bytes) -> pd.DataFrame:
    """Parse the JPMorgan bullion weightlist into one row per gold bar."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    cover_text = doc[0].get_text()
    m = _GLD_AS_AT_RE.search(cover_text)
    if not m:
        raise RuntimeError("GLD bar list: could not find 'As at:' date on cover page")
    as_at = datetime.strptime(f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "%d-%B-%Y").date()

    rows = []
    buf = []
    for pno in range(1, len(doc)):
        lines = [l.strip() for l in doc[pno].get_text().splitlines() if l.strip()]
        i = 0
        while i < len(lines):
            line = lines[i]
            if (line in _GLD_HEADER_LITERALS or line.startswith("Group for Vault")
                    or line.startswith("Vault Name:") or line.startswith("Vault Location:")):
                i += 1
                continue
            if line == "Running Total :":
                i += 3  # label + gross running total + fine running total
                continue
            if line.startswith("Printed on") or _GLD_PAGE_FOOTER_RE.match(line):
                i += 1
                continue
            if line in _GLD_VAULT_NAMES:
                if len(buf) == 7:
                    brand, bar_no, shape, gross, assay, fine, year = buf
                elif len(buf) == 6:
                    brand, bar_no, shape, gross, assay, fine = buf
                    year = None
                else:
                    log.warning("[GLD] Unexpected field count (%d) on page %d, skipping: %s",
                                len(buf), pno, buf)
                    buf = []
                    i += 1
                    continue
                rows.append({
                    "holding_name": f"Gold Bar #{bar_no}",
                    "issuer_name": brand,
                    "country": _GLD_VAULT_COUNTRY.get(line),
                    "gross_oz": float(gross.replace(",", "")),
                    "fine_oz": float(fine.replace(",", "")),
                    "manufacture_year": int(year) if year else None,
                })
                buf = []
            else:
                buf.append(line)
            i += 1

    df = pd.DataFrame(rows)
    df["fund_ticker"] = "GLD"
    df["snapshot_date"] = as_at
    df["filing_date"] = as_at
    df["reporting_period_end"] = as_at
    df["source"] = "gld_barlist"
    log.info("[GLD] Parsed %d bars, %.3f fine oz total, as of %s",
              len(df), df["fine_oz"].sum(), as_at)
    return df


# ---------------------------------------------------------------------------
# SGOL: ICBC Standard Bank bar list PDF
# ---------------------------------------------------------------------------

_SGOL_LETTERHEAD = {
    "20 Gresham Street, London, EC2V 7JE", "www.icbcstandard.com",
    "Tel +44(0)20 3145 5000", "abrdn Gold ETF Trust", "Gold LBMA Good Delivery",
}
_SGOL_COL_HEADERS = {"Depository", "Quantity Product", "Brand", "Serial ID",
                      "Fineness", "GW Unit", "TW Unit", "Yom"}
_SGOL_TOTAL_LABELS = {"Total Bars", "Total GW", "Total TW"}
_SGOL_FOOTER_PREFIXES = ("Document date:", "Authorized by the Prudential",
                          "Authority.", "VAT No.", "ICBC Standard Bank Plc. Registered",
                          "Page ")
_SGOL_AS_AT_RE = re.compile(r"As at:\s*\w+,\s*([A-Za-z]+ \d{1,2}, \d{4})")


def parse_sgol_barlist(pdf_bytes: bytes) -> pd.DataFrame:
    """Parse the ICBC Standard bar list into one row per gold bar."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    cover_text = doc[0].get_text()
    m = _SGOL_AS_AT_RE.search(cover_text)
    if not m:
        raise RuntimeError("SGOL bar list: could not find 'As at:' date on cover page")
    as_at = datetime.strptime(m.group(1), "%b %d, %Y").date()

    rows = []
    buf = []
    for pno in range(len(doc)):
        lines = [l.strip() for l in doc[pno].get_text().splitlines() if l.strip()]
        i = 0
        while i < len(lines):
            line = lines[i]
            if (line in _SGOL_LETTERHEAD or line in _SGOL_COL_HEADERS
                    or line.startswith("As at:")
                    or line.startswith(_SGOL_FOOTER_PREFIXES)):
                i += 1
                continue
            if line in _SGOL_TOTAL_LABELS:
                i += 2  # label + value
                continue
            if line == "ICBC Standard Bank PLC" and buf:
                # Stray footer text leaked into buf between records -- discard.
                buf = []
            buf.append(line)
            if len(buf) == 8:
                depository, qty_product, brand_loc, serial, fineness, gw, tw, yom = buf
                brand_parts = [p.strip() for p in brand_loc.split(",")]
                issuer_name = brand_parts[0]
                country = brand_parts[-1] if len(brand_parts) > 1 else None
                rows.append({
                    "holding_name": f"Gold Bar #{serial}",
                    "issuer_name": issuer_name,
                    "country": country,
                    "gross_oz": float(gw.split()[0].replace(",", "")),
                    "fine_oz": float(tw.split()[0].replace(",", "")),
                    "manufacture_year": int(yom) if yom.strip().isdigit() else None,
                })
                buf = []
            i += 1

    df = pd.DataFrame(rows)
    df["fund_ticker"] = "SGOL"
    df["snapshot_date"] = as_at
    df["filing_date"] = as_at
    df["reporting_period_end"] = as_at
    df["source"] = "sgol_barlist"
    log.info("[SGOL] Parsed %d bars, %.3f fine oz total, as of %s",
              len(df), df["fine_oz"].sum(), as_at)
    return df


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

FETCHERS = {
    "GLD": lambda: parse_gld_barlist(_download_pdf(find_gld_barlist_url())),
    "SGOL": lambda: parse_sgol_barlist(_download_pdf(SGOL_BARLIST_URL)),
}


def fetch_all_gold_bar_holdings(only_tickers: list[str] | None = None) -> list[pd.DataFrame]:
    targets = only_tickers if only_tickers else list(FETCHERS.keys())
    frames = []
    for ticker in targets:
        ticker = ticker.upper()
        if ticker not in FETCHERS:
            log.warning("[%s] Not a gold bar trust in this pipeline -- skipping", ticker)
            continue
        try:
            df = FETCHERS[ticker]()
        except Exception as e:
            log.error("[%s] FAILED: %s", ticker, e)
            continue

        df["weight_pct"] = df["fine_oz"] / df["fine_oz"].sum() * 100
        df["shares_held"] = df["fine_oz"]
        df["asset_category"] = "Physical Gold Bullion"
        df["fund_name"] = FUND_META[ticker]["fund_name"]
        df["fund_cik"] = FUND_META[ticker]["fund_cik"]
        df["holding_ticker"] = None
        df["cusip"] = None
        df["isin"] = None
        df["figi"] = None
        df["sedol"] = None
        df["sector"] = None
        df["market_value_usd"] = None
        df = df.drop(columns=["gross_oz", "manufacture_year"])
        frames.append(df)
    return frames


def write_to_iceberg(all_data: list[pd.DataFrame]) -> int:
    """Write gold bar holdings into the shared constituents.fund_holdings table.

    Reuses fund_holdings_pipeline.write_to_iceberg(), which overwrites each
    fund_ticker partition -- same pattern as ssga_holdings_pipeline.py.
    """
    from fund_holdings_pipeline import write_to_iceberg as _fh_write
    return _fh_write(all_data)


def main(tickers: list[str] | None = None):
    log.info("=" * 60)
    log.info("Gold Bar Holdings Pipeline (GLD / SGOL) — %s", date.today())
    log.info("=" * 60)

    frames = fetch_all_gold_bar_holdings(only_tickers=tickers)

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        log.info("-" * 60)
        log.info("TOTAL: %d rows across %d funds", len(combined), combined["fund_ticker"].nunique())
        for ft, count in combined.groupby("fund_ticker").size().items():
            log.info("  %-10s %d bars", ft, count)
        log.info("-" * 60)
    else:
        log.warning("No data fetched.")
        return

    write_to_iceberg(frames)
    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Gold Bar Holdings Pipeline — GLD (JPMorgan) / SGOL (ICBC Standard)"
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="Specific tickers to fetch (default: both GLD and SGOL).",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Accepted for run_all compatibility (daily snapshot source, no history).",
    )
    args = parser.parse_args()
    main(tickers=args.tickers)
