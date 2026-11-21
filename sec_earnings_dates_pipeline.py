#!/usr/bin/env python3
"""
Historical earnings announcement dates for the whole equity universe, from SEC
8-K filings tagged Item 2.02 ("Results of Operations and Financial Condition").

Why: the earnings event study (earnings_surprise_event_study.py) found a real
post-beat drift but runs on 31 mega-caps, because alpha_vantage_earnings is
capped at 25 calls/day. Every listed company files an Item 2.02 8-K when it
releases results, so EDGAR gives announcement dates for ~2,600 companies back
to the item's creation in August 2004 -- dates and exact timing, not EPS
surprise (that needs consensus estimates, which no free source has at depth).

Source: SEC EDGAR submissions API (keyless; EDGAR_USER_AGENT required, <=10 req/s)
  https://data.sec.gov/submissions/CIK##########.json      (recent ~1,000 filings)
  https://data.sec.gov/submissions/CIK##########-submissions-NNN.json  (older pages)
  Each filing carries `items` ("2.02,9.01") and `acceptanceDateTime` (UTC).
  Vetted 2026-11-21: AAPL returns 95 Item 2.02 8-Ks, 2004-10-13 .. 2026-11-02.

Timing: `session` comes from the EDGAR acceptance time in New York time:
  pre       before 09:30  -> the same day's open reacts
  intraday  09:30-16:00   -> the same day's close only partly reflects it
  post      16:00 onward  -> the next trading day reacts
Acceptance time is when the SEC received the 8-K. Companies usually wire the
press release minutes before filing, so it is a slightly late, never early,
estimate of when the news was public.

Caveats:
  - Universe is TODAY's Russell 3000 + S&P 500 (securities table), so firms
    that delisted are missing: a survivorship bias for any study over it.
  - Some companies file Item 2.02 for things other than quarterly results
    (preliminary numbers, restatement notices), so a company can have more
    than four rows a year. 8-K/A amendments are excluded.
  - One CIK can trade under several tickers (GOOG/GOOGL); each ticker gets a row.
  - A holding-company reorganization gives a company a NEW CIK, and its earlier
    filings stay under the old one (Disney 2019, Alphabet 2015, Exxon 2026).
    SEC publishes no predecessor map, so PREDECESSOR_CIKS is built and checked
    by hand -- see its comment for how.

CLI:
  python sec_earnings_dates_pipeline.py              # 8-K filers of the last 10 days
  python sec_earnings_dates_pipeline.py --backfill   # every universe company, full history
  python sec_earnings_dates_pipeline.py --symbols AAPL,MSFT --backfill

Outputs:
  storage/raw/sec_earnings/year=YYYY/month=MM/earnings_announcements_{mode}_{YYYYMMDD_HHMMSS}[_partNN].parquet

Exits 1 when nothing is fetched or more than 10% of companies fail.
"""

import argparse
import datetime
import os
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv

from storage_utils import write_partitioned

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
BASE_DIR = "storage/raw/sec_earnings"
REQUEST_GAP = 0.15          # SEC fair access: stay under 10 req/s
WRITE_EVERY = 250           # companies per file, so a timed-out backfill keeps its progress
INCREMENTAL_DAYS = 10
MAX_FAIL_SHARE = 0.10
NY = ZoneInfo("America/New_York")

# current CIK -> [(old CIK, end)]: the same company's earlier filer, used for
# filings dated before `end` (the next filer's first Item 2.02 8-K). The end
# date matters: an old CIK can live on as a subsidiary and keep filing (Duke
# Energy Carolinas), and those later filings are not this company's releases.
#
# Built 2026-11-21. Candidates: the 129 companies with prices before mid-2004
# but no Item 2.02 8-K until 2006 or later. A predecessor had to (1) have carried
# a matching name (punctuation, legal suffixes and spaces ignored; SEC's
# cik-lookup-data.txt has every name each CIK has used) or be named in this
# company's 8-K12B/8-K12G3 as the issuer it succeeded, (2) have 4+ Item 2.02
# 8-Ks of its own, starting first, and (3) stop within 200 days before the
# successor's first. Exxon, Disney and Alphabet were known answers going in;
# all three were found. Every accepted match was then read by hand:
#   - FUN maps to CEDAR FAIR, not to old Six Flags (the name match): the 2024
#     merger kept Cedar Fair's ticker, so FUN's earlier prices are Cedar Fair's.
#   - DUK was added by hand: old Duke Energy Corp kept filing a few 8-Ks as a
#     subsidiary after 2006, which broke rule (3).
#   - Left out on purpose -- the old entity is not the same equity or ticker:
#     spin-offs whose parent lives on (AA/Howmet, CR/Crane NXT, FAF/CoreLogic),
#     buyouts and bankruptcies (old GM, Bright Horizons 2008, Swift), and
#     unrelated names (SELLAS/Phio, Del Monte/Big Heart, People/Match).
#   - Not fixable here: HR and CZR, where the merger partner's CIK took the
#     name and ticker, so the CIK's own pre-merger filings are another ticker's.
# The other ~80 late starters had no predecessor: later IPOs/spin-offs under a
# recycled ticker, or companies that didn't use Item 2.02 at first.
PREDECESSOR_CIKS: dict[int, list[tuple[int, str]]] = {
    1748824: [(1611702, "2019-08-01")],  # AAMI: BrightSphere Investment Group 
    1467373: [(1134538, "2009-10-01")],  # ACN: ACCENTURE LTD
    1520262: [(874663, "2011-11-03")],  # ALKS: ALKERMES INC
    1841666: [(6769, "2021-05-06")],  # APA: APACHE CORP
    1674862: [(1305014, "2016-11-08"), (7694, "2005-07-25")],  # ASH: ASHLAND LLC <- ASHLAND INC
    1701605: [(808362, "2017-07-28")],  # BKR: Baker Hughes Holdings LLC
    2012383: [(1364742, "2024-10-11"), (1060021, "2006-10-30")],  # BLK: BlackRock Finance, Inc. <- BlackRock Holdco 2, Inc.
    1390777: [(9626, "2007-07-20")],  # BNY: BANK OF NEW YORK CO INC
    1490906: [(1074433, "2011-02-09")],  # CFFN: CAPITOL FEDERAL FINANCIAL
    1739940: [(701221, "2019-05-02")],  # CI: Cigna Holding Co
    1958086: [(1000229, "2023-07-26")],  # CLB: CORE LABORATORIES N V
    2115119: [(1723596, "2026-07-30")],  # CLBK: Columbia Financial, Inc.
    1915657: [(48039, "2022-05-09")],  # DINO: HollyFrontier Corp
    1744489: [(1001039, "2019-05-08")],  # DIS: TWDC Enterprises 18 Corp.
    2119322: [(1438231, "2026-08-13"), (1089443, "2008-11-20")],  # DMRC: Old Digimarc CORP <- DIGIMARC CORP
    1326160: [(30371, "2006-05-02")],  # DUK: DUKE ENERGY CORP (now Duke Ene
    1551182: [(31277, "2013-02-05")],  # ETN: EATON CORP
    1999001: [(811532, "2024-08-08")],  # FUN: CEDAR FAIR L P
    1652044: [(1288776, "2015-10-22")],  # GOOGL: GOOGLE INC.
    1408075: [(886239, "2008-05-09")],  # GPK: GRAPHIC PACKAGING CORP
    1405495: [(354913, "2007-08-08")],  # IDCC: INTERDIGITAL INC.
    1707925: [(884905, "2019-05-10")],  # LIN: LINDE INC
    2052959: [(2006191, "2025-05-22")],  # LION: Lionsgate Studios Holding Corp
    1613103: [(64670, "2015-05-19")],  # MDT: MEDTRONIC INC
    1835632: [(1058057, "2021-06-07")],  # MRVL: MARVELL TECHNOLOGY GROUP LTD
    2152623: [(1593538, "2026-11-04")],  # NAVI: Navient LLC
    1587987: [(1094019, "2014-11-14")],  # NEWT: NEWTEK BUSINESS SERVICES, INC.
    2104052: [(45876, "2026-08-11")],  # NVRI: ENVIRI LLC
    1341439: [(777676, "2006-03-20")],  # ORCL: Oracle Systems
    1466593: [(75129, "2009-08-04")],  # OTTR: OTTER TAIL CORP
    2082866: [(1115055, "2026-01-21")],  # PNFP: PINNACLE FINANCIAL PARTNERS IN
    1585364: [(820096, "2014-02-06")],  # PRGO: PERRIGO CO
    1393311: [(318380, "2007-08-03")],  # PSA: PUBLIC STORAGE INC /CA
    1906324: [(353569, "2022-08-04")],  # QDEL: QUIDEL CORP /DE/
    1708055: [(1105472, "2018-03-01")],  # RBBN: Sonus, Inc.
    1451505: [(1083269, "2009-02-17")],  # RIG: TRANSOCEAN INTERNATIONAL Ltd
    1757898: [(1624899, "2019-05-13"), (815065, "2016-02-09")],  # STE: STERIS Ltd <- STERIS CORP
    1692115: [(92416, "2017-02-27")],  # SWX: SOUTHWEST GAS CORP
    1466258: [(1160497, "2009-07-24")],  # TT: INGERSOLL RAND CO LTD
    1396009: [(103973, "2008-02-14")],  # VMC: Legacy Vulcan Corp.
    2115436: [(34088, "2026-07-31")],  # XOM: EXXON MOBIL CORP
    1770450: [(108772, "2019-10-29")],  # XRX: XEROX CORP
}


def _headers() -> dict:
    return {"User-Agent": os.environ.get("EDGAR_USER_AGENT", ""),
            "Accept-Encoding": "gzip, deflate"}


# --------------------------------------------------------------- universe

def load_universe(symbols: "list[str] | None" = None) -> pd.DataFrame:
    """(symbol, cik) for today's Russell 3000 + S&P 500, or for given symbols."""
    import query as q
    where = ("symbol IN ({})".format(", ".join(f"'{s}'" for s in symbols)) if symbols
             else "(is_russell3000 OR is_sp500)")
    df = q.sql(f"SELECT DISTINCT symbol, CAST(cik AS BIGINT) AS cik FROM securities "
               f"WHERE cik IS NOT NULL AND {where}")
    return df.dropna().astype({"cik": "int64"})


# ------------------------------------------------------------------ fetch

def _get_json(session: requests.Session, name: str) -> dict:
    last: Exception | None = None
    for attempt in range(1, 5):
        try:
            resp = session.get(SUBMISSIONS_URL.format(name=name), timeout=60)
            if resp.status_code == 404:
                raise FileNotFoundError(name)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except FileNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001 - retry transport/HTTP errors
            last = exc
            time.sleep(2 * attempt)
        finally:
            time.sleep(REQUEST_GAP)
    raise RuntimeError(f"{name}: failed after 4 attempts ({last})")


def earnings_filings(block: dict) -> pd.DataFrame:
    """Item 2.02 8-Ks out of one submissions block (the `recent` dict or an
    older page -- both are column-oriented dicts of equal-length lists)."""
    cols = ["accessionNumber", "filingDate", "acceptanceDateTime", "form", "items"]
    if not block or any(c not in block for c in cols):
        return pd.DataFrame()
    df = pd.DataFrame({c: block[c] for c in cols})
    items = df["items"].fillna("").str.split(",").apply(lambda xs: [x.strip() for x in xs])
    return df[(df["form"] == "8-K") & items.apply(lambda xs: "2.02" in xs)]


def _filer_filings(session: requests.Session, filer: int, full_history: bool,
                   before: "str | None" = None) -> pd.DataFrame:
    j = _get_json(session, f"CIK{filer:010d}.json")
    frames = [earnings_filings(j.get("filings", {}).get("recent", {}))]
    if full_history:
        for page in j.get("filings", {}).get("files", []):
            frames.append(earnings_filings(_get_json(session, page["name"])))
    df = pd.concat(frames, ignore_index=True)
    if before is not None:
        df = df[df["filingDate"] < before]
    df["filer_cik"] = filer
    return df


def company_filings(session: requests.Session, cik: int, full_history: bool) -> pd.DataFrame:
    """Item 2.02 8-Ks of the company now registered as `cik`, including those filed
    under its predecessor CIKs. Predecessors are read in full only on a backfill:
    they have stopped filing, so an incremental run has nothing new to find there."""
    frames = [_filer_filings(session, cik, full_history)]
    if full_history:
        frames += [_filer_filings(session, old, True, before=end)
                   for old, end in PREDECESSOR_CIKS.get(cik, [])]
    df = pd.concat(frames, ignore_index=True)
    df["cik"] = cik
    return df


# ------------------------------------------------------------------ shape

def classify_session(accepted_utc: pd.Series) -> pd.Series:
    et = accepted_utc.dt.tz_convert(NY)
    minutes = et.dt.hour * 60 + et.dt.minute
    out = pd.Series("post", index=accepted_utc.index, dtype="string")
    out[minutes < 16 * 60] = "intraday"
    out[minutes < 9 * 60 + 30] = "pre"
    out[accepted_utc.isna()] = pd.NA
    return out


def shape(raw: pd.DataFrame, universe: pd.DataFrame, fetched_at: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    accepted = pd.to_datetime(raw["acceptanceDateTime"], utc=True, errors="coerce")
    out = pd.DataFrame({
        "cik": raw["cik"].astype("int64"),
        "filer_cik": (raw["filer_cik"] if "filer_cik" in raw else raw["cik"]).astype("int64"),
        "accession_number": raw["accessionNumber"],
        "filing_date": pd.to_datetime(raw["filingDate"], errors="coerce").dt.strftime("%Y-%m-%d"),
        "accepted_at": accepted.dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "accepted_et": accepted.dt.tz_convert(NY).dt.strftime("%Y-%m-%d %H:%M:%S"),
        "session": classify_session(accepted),
        "items": raw["items"],
    })
    out = out.merge(universe, on="cik", how="inner")      # one row per ticker of the CIK
    out = out.drop_duplicates(["symbol", "accession_number"])
    out["fetched_at"] = fetched_at
    cols = ["symbol", "cik", "filer_cik", "accession_number", "filing_date", "accepted_at",
            "accepted_et", "session", "items", "fetched_at"]
    return out[cols].sort_values(["symbol", "filing_date"]).reset_index(drop=True)


# -------------------------------------------------------------------- run

def recent_8k_filers(session: requests.Session, days: int) -> set:
    """CIKs that filed any 8-K in the last `days` days, from EDGAR's daily index."""
    from sec_filings_pipeline import fetch_day
    today = datetime.date.today()
    ciks: set = set()
    for back in range(days + 1):
        idx = fetch_day(session, today - datetime.timedelta(days=back))
        time.sleep(REQUEST_GAP)
        if not idx.empty:
            ciks |= set(idx.loc[idx["form"] == "8-K", "cik"].astype("int64"))
    return ciks


def run(universe: pd.DataFrame, ciks: list, full_history: bool, mode: str,
        fetched_at: str, session: requests.Session) -> int:
    # date AND time: a second run on the same day (e.g. --symbols) must not
    # overwrite the first run's part files
    stamp = fetched_at[:19].replace("-", "").replace(":", "").replace("T", "_")
    failed, written, batch = [], 0, []

    def flush(part: int) -> None:
        nonlocal written, batch
        if not batch:
            return
        df = shape(pd.concat(batch, ignore_index=True), universe, fetched_at)
        batch = []
        if df.empty:
            return
        suffix = f"_part{part:02d}" if full_history else ""
        path = write_partitioned(df, BASE_DIR,
                                 f"earnings_announcements_{mode}_{stamp}{suffix}.parquet")
        written += len(df)
        print(f"  wrote {len(df):,} rows -> {path}")

    for i, cik in enumerate(ciks, 1):
        try:
            batch.append(company_filings(session, cik, full_history))
        except Exception as exc:  # noqa: BLE001 - count it, keep going
            failed.append(cik)
            print(f"  CIK {cik}: {exc}")
        if i % WRITE_EVERY == 0:
            print(f"  {i:,}/{len(ciks):,} companies")
            flush(i // WRITE_EVERY)
    flush(len(ciks) // WRITE_EVERY + 1)

    print(f"  companies: {len(ciks):,}  failed: {len(failed)}  rows written: {written:,}")
    if ciks and len(failed) / len(ciks) > MAX_FAIL_SHARE:
        print(f"  FAILED: {len(failed)} of {len(ciks)} companies failed")
        return 1
    # A quiet incremental week can legitimately have no Item 2.02 filings;
    # a backfill that writes nothing is broken.
    if full_history and written == 0:
        print("  FAILED: backfill wrote no rows")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="SEC 8-K Item 2.02 earnings announcement dates")
    parser.add_argument("--backfill", action="store_true", help="full filing history per company")
    parser.add_argument("--symbols", default="", help="comma-separated tickers (default: R3000+S&P500)")
    parser.add_argument("--days", type=int, default=INCREMENTAL_DAYS,
                        help="incremental: look at 8-K filers of the last N days")
    args = parser.parse_args()

    if not _headers()["User-Agent"]:
        print("EDGAR_USER_AGENT missing from .env -- SEC requires an identifying User-Agent.")
        sys.exit(1)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or None
    universe = load_universe(symbols)
    if universe.empty:
        print("  FAILED: empty universe (is the securities table populated?)")
        sys.exit(1)

    session = requests.Session()
    session.headers.update(_headers())
    mode = "backfill" if args.backfill else "incremental"
    fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    all_ciks = sorted(set(universe["cik"]))
    if args.backfill or symbols:
        ciks = all_ciks
    else:
        filers = recent_8k_filers(session, args.days)
        ciks = [c for c in all_ciks if c in filers]

    print(f"SEC Earnings Dates  mode={mode}  universe={len(all_ciks):,} companies  "
          f"fetching={len(ciks):,}")
    code = run(universe, ciks, args.backfill, mode, fetched_at, session)
    print("--- SEC EARNINGS DATES PIPELINE COMPLETE ---" if code == 0
          else "--- SEC EARNINGS DATES PIPELINE FAILED ---")
    sys.exit(code)


if __name__ == "__main__":
    main()
