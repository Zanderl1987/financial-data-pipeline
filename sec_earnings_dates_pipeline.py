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
    SEC publishes no predecessor map, so PREDECESSOR_CIKS is hand-checked; each
    entry was confirmed against the old CIK's submissions JSON. As of
    2026-11-21 about 10% of companies listed before 2004 (113 of 1,142, an upper
    bound: recycled tickers inflate it) still have no 8-K 2.02 before 2008 --
    candidates for more entries, found by comparing first filing vs first price.

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

# current CIK -> CIKs the same company filed under before a reorganization.
# Add only after checking the old CIK's submissions JSON (name, formerNames, 8-Ks).
PREDECESSOR_CIKS: dict[int, list[int]] = {
    2115436: [34088],     # Exxon Mobil: new holding company 2026; old EXXON MOBIL CORP
    1744489: [1001039],   # Walt Disney: new holding company 2019; old TWDC Enterprises 18 (WALT DISNEY CO/)
    1652044: [1288776],   # Alphabet: formed 2015; old GOOGLE INC.
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


def _filer_filings(session: requests.Session, filer: int, full_history: bool) -> pd.DataFrame:
    j = _get_json(session, f"CIK{filer:010d}.json")
    frames = [earnings_filings(j.get("filings", {}).get("recent", {}))]
    if full_history:
        for page in j.get("filings", {}).get("files", []):
            frames.append(earnings_filings(_get_json(session, page["name"])))
    df = pd.concat(frames, ignore_index=True)
    df["filer_cik"] = filer
    return df


def company_filings(session: requests.Session, cik: int, full_history: bool) -> pd.DataFrame:
    """Item 2.02 8-Ks of the company now registered as `cik`, including those filed
    under its predecessor CIKs. Predecessors are read in full only on a backfill:
    they have stopped filing, so an incremental run has nothing new to find there."""
    filers = [cik] + (PREDECESSOR_CIKS.get(cik, []) if full_history else [])
    df = pd.concat([_filer_filings(session, f, full_history) for f in filers], ignore_index=True)
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
