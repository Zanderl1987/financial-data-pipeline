#!/usr/bin/env python3
"""
Senate Lobbying Disclosure (LDA) filings pipeline — keyless replacement for
the 403'd Finnhub /stock/lobbying endpoint (free tier killed ~2026-08).

Pulls federal lobbying disclosure filings straight from the official source:

  CATALOG table: lda_lobbying_filings

Source notes (probed live 2026-08-26):
  - GET https://lda.senate.gov/api/v1/filings/?filing_year={YYYY}&page={N}
    Keyless JSON; page_size silently capped at 25 rows/page (~2.2k pages for
    a recent year). Follows `next` URLs (they point at the lda.gov mirror).
  - ~57k filings by late September, ~109k for a full year (2025). At ~1.7s
    per page plus REQUEST_GAP, a whole year is 2-4 hours of paging.
  - `filing_dt_posted_after=YYYY-MM-DD` filters server-side (verified
    2026-09-29: an unknown param returns all ~2M filings, this returns only
    the recent ones).
  - Be polite: REQUEST_GAP sleep between pages; retry/backoff on transport
    errors.

Incremental mode fetches only filings posted since the newest `dt_posted`
already stored (minus OVERLAP_DAYS; curated dedups on filing_uuid). Until
2026-09-29 it re-fetched the whole current year every run, which outgrew
run_all's 30-min timeout by August and failed every night, killed silently
because stdout was block-buffered into run_all's pipe. With no stored data it
falls back to the whole current year. Gap: a filing with a null dt_posted
(some amendments) never matches the date filter, so only --backfill picks
those up.

CLI:
  python lda_lobbying_pipeline.py                    # posted since last stored filing
  python lda_lobbying_pipeline.py --backfill         # 2019 -> current year, whole years
  python lda_lobbying_pipeline.py --start-year 2000  # deeper history, whole years

Outputs:
  storage/raw/lda_lobbying/year=YYYY/month=MM/lda_lobbying_filings_{mode}_{date}.parquet
"""

import argparse
import datetime
import glob
import os
import sys
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from storage_utils import write_partitioned

load_dotenv()

API_URL = "https://lda.senate.gov/api/v1/filings/"
BASE_DIR = "storage/raw/lda_lobbying"
REQUEST_GAP = 1.5
RETRIES = 6
PAGE_SIZE = 100          # server caps response rows at 25 regardless
MAX_PAGES_PER_YEAR = 5000  # hard safety stop
DEFAULT_BACKFILL_START_YEAR = 2019
ISSUES_TEXT_MAX = 1500
OVERLAP_DAYS = 2           # re-fetch a little before the newest stored filing


def _get_json(url: str) -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, timeout=60,
                                headers={"User-Agent": "financial-data-pipeline/1.0"})
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 15 * attempt  # LDO rate limiter is stateful; short waits don't clear it
                print(f"    429 - backing off {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:150]}")
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 * attempt)
    raise RuntimeError(f"GET {url} failed after {RETRIES} attempts ({last_exc})")


def _flatten(filing: dict) -> dict | None:
    """Map one API filing object to our snake_case row."""
    try:
        registrant = filing.get("registrant") or {}
        client = filing.get("client") or {}
        issues = filing.get("specific_issues") or []
        issues_text = " | ".join(
            str((i or {}).get("description") or "").strip() for i in issues if (i or {}).get("description")
        )[:ISSUES_TEXT_MAX]
        return {
            "filing_uuid":      filing.get("filing_uuid"),
            "filing_type":      filing.get("filing_type"),
            "filing_year":      filing.get("filing_year"),
            "filing_period":    filing.get("filing_period"),
            "income":           filing.get("income"),
            "expenses":         filing.get("expenses"),
            "dt_posted":        filing.get("dt_posted"),
            "termination_date": filing.get("termination_date"),
            "registrant_id":    registrant.get("id"),
            "registrant_name":  registrant.get("name"),
            "client_id":        client.get("id"),
            "client_name":      client.get("name"),
            "client_general_description": client.get("general_description"),
            "num_lobbyists":    len(filing.get("lobbyists") or []),
            "num_gov_entities": len(filing.get("gov_entities") or []),
            "num_issues":       len(issues),
            "issues_text":      issues_text,
        }
    except Exception as exc:  # noqa: BLE001 - one malformed row shouldn't kill a year
        print(f"    flatten ERROR ({exc}) on uuid={filing.get('filing_uuid')}")
        return None


def _fetch_pages(url: str, label: str) -> pd.DataFrame:
    """Follow `next` links from url, flattening every filing."""
    rows: list[dict] = []
    for page in range(1, MAX_PAGES_PER_YEAR + 1):
        body = _get_json(url)
        if page == 1:
            print(f"    {label}: {body.get('count', '?')} filings")
        results = body.get("results", [])
        for filing in results:
            flat = _flatten(filing)
            if flat:
                rows.append(flat)
        url = body.get("next")
        if not url:
            break
        time.sleep(REQUEST_GAP)
        if page % 100 == 0:
            print(f"    {label}: page {page}, {len(rows):,} rows so far")
    return pd.DataFrame(rows)


def fetch_year(year: int) -> pd.DataFrame:
    """Pull every filing row for one year via paginated GET."""
    df = _fetch_pages(f"{API_URL}?filing_year={year}&page_size={PAGE_SIZE}", str(year))
    if not df.empty:
        df["filing_year"] = df["filing_year"].fillna(year).astype(int)
    return df


def fetch_posted_since(since: datetime.date) -> pd.DataFrame:
    """Pull filings posted on/after `since`, whatever their filing year."""
    url = f"{API_URL}?filing_dt_posted_after={since.isoformat()}&page_size={PAGE_SIZE}"
    return _fetch_pages(url, f"posted since {since}")


def latest_posted(base_dir: str = BASE_DIR) -> datetime.date | None:
    """Newest dt_posted date across stored raw files, or None if nothing stored."""
    newest = None
    for path in glob.glob(os.path.join(base_dir, "**", "*.parquet"), recursive=True):
        col = pd.read_parquet(path, columns=["dt_posted"])["dt_posted"]
        ts = pd.to_datetime(col, errors="coerce", utc=True).max()
        if pd.notna(ts) and (newest is None or ts > newest):
            newest = ts
    return None if newest is None else newest.date()


def main() -> None:
    parser = argparse.ArgumentParser(description="Senate LDA lobbying filings (keyless)")
    parser.add_argument("--backfill", action="store_true",
                        help=f"Full history from {DEFAULT_BACKFILL_START_YEAR}")
    parser.add_argument("--start-year", type=int, default=None)
    args = parser.parse_args()
    # run_all.py reads us through a pipe, where stdout is block-buffered: a
    # timeout kill would otherwise lose every progress line
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()

    stored = None if (args.backfill or args.start_year) else latest_posted()
    if stored is not None:
        since = stored - datetime.timedelta(days=OVERLAP_DAYS)
        print(f"LDA Lobbying Pipeline  mode=incremental  posted since {since} "
              f"(newest stored {stored})")
        # no try/except: a failed fetch should exit non-zero and show as FAIL
        df = fetch_posted_since(since)
        if df.empty:
            print("  no new filings")
        else:
            df["fetched_at"] = fetched_at
            out_name = f"lda_lobbying_filings_incremental_{today_str}_since{since:%Y%m%d}.parquet"
            path = write_partitioned(df, BASE_DIR, out_name)
            print(f"  {len(df):,} rows -> {path}")
        print(f"Total {len(df):,} rows")
        print("--- LDA LOBBYING PIPELINE COMPLETE ---")
        return

    end_year = now.year
    start_year = args.start_year or (DEFAULT_BACKFILL_START_YEAR if args.backfill else end_year)
    mode = "backfill" if args.backfill else "incremental"

    print(f"LDA Lobbying Pipeline  mode={mode}  years={start_year}-{end_year}")
    total_rows = 0
    for year in range(start_year, end_year + 1):
        # Per-year write + resume-skip: a full year is ~2k+ pages and can be
        # interrupted by rate limits or process death; don't lose finished years
        # and don't re-fetch them on retry.
        out_name = f"lda_lobbying_filings_{mode}_{today_str}_y{year}.parquet"
        out_path = os.path.join(BASE_DIR, f"year={end_year}",
                                f"month={now.month:02d}", out_name)
        if os.path.exists(out_path):
            print(f"[{year}] already fetched -> {out_path} (skipping)")
            total_rows += int(pd.read_parquet(out_path).shape[0])
            continue
        print(f"[{year}]")
        try:
            df = fetch_year(year)
        except Exception as exc:
            print(f"  {year}: ERROR - {exc}")
            continue
        if df.empty:
            print(f"  no data")
            continue
        df["fetched_at"] = fetched_at
        path = write_partitioned(df, BASE_DIR, out_name)
        total_rows += len(df)
        print(f"  {len(df):,} rows -> {path}")

    print(f"Total {total_rows:,} rows")
    print("--- LDA LOBBYING PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
