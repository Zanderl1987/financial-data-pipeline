#!/usr/bin/env python3
"""
SEC EDGAR filings-index pipeline.

Pulls filing metadata (form type, company, CIK, filed date, document URL)
from EDGAR's daily form indexes and maps CIKs to tickers. This is the event
stream for "what happens to a stock after it files X" studies: 8-Ks
(material events), 10-K/10-Q (annual/quarterly reports), S-1 (IPO
registrations), SC 13D/G (activist / >5% stakes), DEF 14A (proxies).

Requires EDGAR_USER_AGENT in .env (SEC fair-access policy).

CLI:
  python sec_filings_pipeline.py                # last 7 calendar days
  python sec_filings_pipeline.py --backfill     # last 90 days
  python sec_filings_pipeline.py --days 30      # explicit window
  python sec_filings_pipeline.py --forms "8-K,10-Q"

Output:
  storage/raw/sec_filings/year=YYYY/month=MM/sec_filings_{mode}_{date}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from storage_utils import write_partitioned

load_dotenv()

BASE_DIR = "storage/raw/sec_filings"
IDX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/form.{ymd}.idx"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
REQUEST_GAP = 0.15  # SEC fair-access: stay well under 10 req/s

DEFAULT_FORMS = ["8-K", "10-K", "10-Q", "10-K/A", "10-Q/A",
                 "S-1", "S-1/A", "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A",
                 "DEF 14A"]

HEADERS = {"User-Agent": os.environ.get("EDGAR_USER_AGENT", ""),
           "Accept-Encoding": "gzip, deflate"}


def load_ticker_map(session: requests.Session) -> pd.DataFrame:
    """CIK -> ticker/company map from SEC's canonical JSON."""
    resp = session.get(TICKER_MAP_URL, timeout=30)
    resp.raise_for_status()
    rows = list(resp.json().values())
    df = pd.DataFrame(rows).rename(columns={"cik_str": "cik", "ticker": "symbol",
                                            "title": "company_sec"})
    # one CIK can map to several share classes; keep the shortest ticker (primary)
    df = (df.sort_values("symbol", key=lambda s: s.str.len())
            .drop_duplicates("cik"))
    df["cik"] = df["cik"].astype(int)
    return df[["cik", "symbol"]]


def parse_form_idx(text: str) -> pd.DataFrame:
    """
    Parse a form.idx daily index. The header row wraps unpredictably, so
    instead of fixed-width slicing, parse each data row from the right:
    the last three whitespace-free tokens are file name, date filed, and CIK;
    what remains is "form  company" separated by a run of 2+ spaces (form
    types themselves can contain single spaces, e.g. "SC 13D", "DEF 14A").
    """
    import re
    lines = text.splitlines()
    sep_i = next((i for i, ln in enumerate(lines) if ln.startswith("---")), None)
    if sep_i is None:
        return pd.DataFrame()

    records = []
    for ln in lines[sep_i + 1:]:
        if not ln.strip():
            continue
        parts = ln.rsplit(None, 3)
        if len(parts) != 4 or not parts[2].isdigit():
            continue
        left, cik, filed, file_name = parts
        head = re.split(r"\s{2,}", left.strip(), maxsplit=1)
        if len(head) != 2:
            continue
        form, company = head
        records.append((form, company, cik, filed, file_name))
    df = pd.DataFrame(records, columns=["form", "company", "cik", "filed", "file_name"])
    if df.empty:
        return df
    df["filed"] = pd.to_datetime(df["filed"], format="%Y%m%d",
                                 errors="coerce").dt.strftime("%Y-%m-%d")
    df["cik"] = pd.to_numeric(df["cik"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["cik", "form"])
    return df


def fetch_day(session: requests.Session, day: datetime.date) -> pd.DataFrame:
    qtr = (day.month - 1) // 3 + 1
    url = IDX_URL.format(year=day.year, qtr=qtr, ymd=day.strftime("%Y%m%d"))
    resp = session.get(url, timeout=30)
    if resp.status_code in (403, 404):   # weekend/holiday or not yet published
        return pd.DataFrame()
    resp.raise_for_status()
    return parse_form_idx(resp.text)


def main() -> None:
    parser = argparse.ArgumentParser(description="SEC EDGAR daily filing index")
    parser.add_argument("--backfill", action="store_true", help="Last 90 days")
    parser.add_argument("--days", type=int, default=0, help="Explicit lookback window")
    parser.add_argument("--forms", default="", help="Comma-separated form types (default: notable set)")
    args = parser.parse_args()

    if not HEADERS["User-Agent"]:
        print("EDGAR_USER_AGENT missing from .env — SEC requires an identifying User-Agent. Skipping.")
        return

    days = args.days or (90 if args.backfill else 7)
    forms = ([f.strip().upper() for f in args.forms.split(",") if f.strip()]
             or DEFAULT_FORMS)
    mode = "backfill" if args.backfill else "incremental"

    now = datetime.datetime.utcnow()
    today = now.date()
    os.makedirs(BASE_DIR, exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)

    print(f"SEC EDGAR Filings Pipeline  mode={mode}  days={days}  forms={len(forms)}\n")
    print("[sec_filings]")
    ticker_map = load_ticker_map(session)
    print(f"  ticker map: {len(ticker_map):,} CIKs")

    frames = []
    for i in range(days):
        day = today - datetime.timedelta(days=i)
        if day.weekday() >= 5:
            continue
        try:
            df = fetch_day(session, day)
        except Exception as exc:
            print(f"  {day}: ERROR — {exc}")
            continue
        if df.empty:
            continue
        df = df[df["form"].str.upper().isin(forms)]
        print(f"  {day}: {len(df):,} filings")
        frames.append(df)
        time.sleep(REQUEST_GAP)

    if not frames:
        print("  No filings retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["cik"] = combined["cik"].astype(int)
    combined = combined.merge(ticker_map, on="cik", how="left")
    combined["url"] = "https://www.sec.gov/Archives/" + combined["file_name"]
    combined = combined.drop(columns=["file_name"])
    combined["fetched_at"] = now.isoformat()

    path = write_partitioned(combined, BASE_DIR,
                             f"sec_filings_{mode}_{now.strftime('%Y%m%d')}.parquet")
    with_ticker = combined["symbol"].notna().mean()
    print(f"  -> {path}  ({len(combined):,} rows, {with_ticker:.0%} mapped to tickers)")
    print(f"  form mix: {combined['form'].value_counts().head(8).to_dict()}")

    print("\n--- SEC FILINGS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
