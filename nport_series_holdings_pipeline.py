#!/usr/bin/env python3
"""
N-PORT Series-Matched ETF Holdings Pipeline:
  Quarterly holdings for ETFs that have no usable daily issuer feed, read from
  SEC N-PORT-P filings and matched on each fund's SEC series_id. Writes to
  Iceberg table constituents.fund_holdings with source='edgar_nport:<TICKER>'.

  Covers (added 2026-11-13, when securitiesdb.com -- which etf_holdings_pipeline.py
  depended on -- began redirecting every URL to itself):
    - Vanguard equity ETFs (23). Vanguard publishes no keyless holdings file;
      its site API 404s.
    - ARK ETFs (6). assets.ark-funds.com serves daily CSVs, but its robots.txt
      disallows every automated client (`User-agent: * / Disallow: /`).
    - WisdomTree DGRW. wisdomtree.com robots.txt disallows /api.
  Each is a share class of an SEC-registered series, so N-PORT is the public,
  keyless, ToS-clean source for all of them.

  Why series matching instead of fund_holdings_pipeline.py's Fund(ticker):
  EdgarTools resolves an ETF ticker to a sibling share class of the same series
  (VOO -> the Admiral mutual-fund class), and report fetching then follows the
  wrong class. Walking the trust's NPORT-P filings newest-first and taking the
  first whose own series_id matches is the fix proven in
  vanguard_bond_holdings_pipeline.py, whose helpers this module reuses.

  Reporting cadence: N-PORT is quarterly with a ~60-day public lag, so these
  rows are point-in-time as of `reporting_period_end`, NOT `snapshot_date`.
  Daily runs rewrite the same quarter until a new one is filed (idempotent:
  Iceberg overwrite per fund_ticker).

  Series/CIK values from SEC company_tickers_mf.json (2026-11-13).

  CLI:
    python nport_series_holdings_pipeline.py                      # all funds
    python nport_series_holdings_pipeline.py --tickers ARKK VOO   # subset

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vanguard_bond_holdings_pipeline import (  # noqa: E402
    SNAPSHOT_DATE,
    _find_reports,
    _parse_report,
    log,
    write_to_iceberg,
)

# ticker -> (sponsor trust CIK, series_id, fund name)
FUNDS: dict[str, tuple[int, str, str]] = {
    # Vanguard Index Funds
    "VOO":  (36405, "S000002839", "Vanguard S&P 500 ETF"),
    "VTI":  (36405, "S000002848", "Vanguard Total Stock Market ETF"),
    "VTV":  (36405, "S000002840", "Vanguard Value ETF"),
    "VUG":  (36405, "S000002842", "Vanguard Growth ETF"),
    "VBK":  (36405, "S000002846", "Vanguard Small-Cap Growth ETF"),
    "VBR":  (36405, "S000002847", "Vanguard Small-Cap Value ETF"),
    # Vanguard World Fund (sector ETFs)
    "VGT":  (52848, "S000004452", "Vanguard Information Technology ETF"),
    "VFH":  (52848, "S000004449", "Vanguard Financials ETF"),
    "VDE":  (52848, "S000004448", "Vanguard Energy ETF"),
    "VHT":  (52848, "S000004450", "Vanguard Health Care ETF"),
    "VCR":  (52848, "S000004446", "Vanguard Consumer Discretionary ETF"),
    "VIS":  (52848, "S000004451", "Vanguard Industrials ETF"),
    "VOX":  (52848, "S000004443", "Vanguard Communication Services ETF"),
    "VAW":  (52848, "S000004441", "Vanguard Materials ETF"),
    "VPU":  (52848, "S000004445", "Vanguard Utilities ETF"),
    # Vanguard Specialized Funds
    "VIG":  (734383, "S000011322", "Vanguard Dividend Appreciation ETF"),
    "VNQ":  (734383, "S000002924", "Vanguard Real Estate ETF"),
    # Vanguard Whitehall Funds
    "VYM":  (1004655, "S000014011", "Vanguard High Dividend Yield ETF"),
    "VYMI": (1004655, "S000051872", "Vanguard International High Dividend Yield ETF"),
    # Vanguard Tax-Managed Funds
    "VEA":  (923202, "S000004386", "Vanguard FTSE Developed Markets ETF"),
    # Vanguard International Equity Index Funds
    "VWO":  (857489, "S000005786", "Vanguard FTSE Emerging Markets ETF"),
    "VEU":  (857489, "S000015871", "Vanguard FTSE All-World ex-US ETF"),
    # Vanguard STAR Funds
    "VXUS": (736054, "S000002932", "Vanguard Total International Stock ETF"),
    # ARK ETF Trust
    "ARKK": (1579982, "S000042977", "ARK Innovation ETF"),
    "ARKG": (1579982, "S000042975", "ARK Genomic Revolution ETF"),
    "ARKQ": (1579982, "S000042976", "ARK Autonomous Technology & Robotics ETF"),
    "ARKW": (1579982, "S000042978", "ARK Next Generation Internet ETF"),
    "ARKF": (1579982, "S000064752", "ARK Fintech Innovation ETF"),
    "ARKX": (1579982, "S000071318", "ARK Space & Defense Innovation ETF"),
    # WisdomTree Trust
    "DGRW": (1350487, "S000040816", "WisdomTree U.S. Quality Dividend Growth Fund"),
}


def fetch(tickers: list[str]) -> list:
    """Walk each sponsor trust once for the wanted series; return parsed frames."""
    wanted = {t.upper() for t in tickers}
    unknown = wanted - set(FUNDS)
    if unknown:
        log.warning("Unknown tickers ignored: %s", sorted(unknown))
    wanted &= set(FUNDS)

    by_trust: dict[int, set[str]] = {}
    for t in wanted:
        cik, sid, _ = FUNDS[t]
        by_trust.setdefault(cik, set()).add(sid)

    frames, failed = [], []
    for cik, series in sorted(by_trust.items()):
        try:
            reports = _find_reports(cik, series)
        except Exception as e:
            log.error("[CIK %s] Trust scan FAILED: %s", cik, e)
            reports = {}
        for t in sorted(wanted):
            t_cik, sid, name = FUNDS[t]
            if t_cik != cik:
                continue
            if sid not in reports:
                failed.append(t)
                continue
            try:
                frames.append(_parse_report(name, t, reports[sid]))
            except Exception as e:
                log.error("[%s] Parse FAILED: %s", t, e)
                failed.append(t)
    if failed:
        log.warning("Failed (%d): %s", len(failed), ", ".join(sorted(failed)))
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF holdings from SEC N-PORT, series-matched")
    parser.add_argument("--tickers", nargs="+", default=list(FUNDS),
                        help="tickers to fetch (default: all)")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("N-PORT Series-Matched Holdings Pipeline -- %s (%d tickers)",
             SNAPSHOT_DATE, len(args.tickers))
    log.info("=" * 60)

    frames = fetch(args.tickers)
    written = write_to_iceberg(frames)
    # Exit non-zero when nothing landed, so run_all reports FAIL instead of a
    # silent PASS -- the failure mode that hid Global X/VanEck for weeks.
    if written == 0:
        log.error("No rows written.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
