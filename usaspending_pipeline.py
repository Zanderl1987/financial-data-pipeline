#!/usr/bin/env python3
"""
USAspending federal contract pipeline — keyless replacement for the 403'd
Finnhub /stock/usa-spending endpoint (free tier killed ~2026-08).

Pulls US federal contract-award data from api.usaspending.gov:

  CATALOG tables:
    usaspending_award_counts — daily contract counts by award type
                               (complete coverage, 1 cheap call per window)
    usaspending_top_awards   — largest new contract awards in each window
                               (recipient, agency, amount, description)

Source notes (probed live 2026-08-26):
  - POST /api/v2/search/spending_by_award_count/ -> counts per award type.
  - POST /api/v2/search/spending_by_award/       -> paginated award rows,
    max limit=100, page capped at 100 by the API; we sort by Award Amount
    descending and cap pages so each window stays bounded.
  - Award-level search floor is 2007-10-01 (older data only via bulk downloads).
  - Keyless; be polite (~0.35s gap). Time-period windows must be <= ~3 years.

CLI:
  python usaspending_pipeline.py                 # last 7 days
  python usaspending_pipeline.py --backfill      # monthly windows from 2015-01
  python usaspending_pipeline.py --start-date 2020-01-01

Outputs:
  storage/raw/usaspending/counts/year=YYYY/month=MM/usaspending_award_counts_{mode}_{date}.parquet
  storage/raw/usaspending/top_awards/year=YYYY/month=MM/usaspending_top_awards_{mode}_{date}.parquet
"""

import argparse
import datetime
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from storage_utils import write_partitioned

load_dotenv()

BASE_URL = "https://api.usaspending.gov/api/v2/search"
REQUEST_GAP = 0.35
RETRIES = 3
PAGE_LIMIT = 100
MAX_PAGES = 5  # top-500 largest awards per window
CONTRACT_TYPE_CODES = ["A", "B", "C", "D"]
# Count endpoint keys its results by NAME ("contracts"), not by letter code.
TYPE_NAMES = {"A": "contract", "B": "grant", "C": "direct_payment", "D": "loan"}
TYPE_RESULT_KEYS = {"A": "contracts", "B": "grants",
                    "C": "direct_payments", "D": "loans"}
DEFAULT_BACKFILL_START = "2015-01-01"

AWARD_FIELDS = [
    "Recipient Name",
    "Start Date",
    "End Date",
    "Award ID",
    "Award Amount",
    "Description",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Place of Performance State",
]


def _post(path: str, payload: dict) -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.post(f"{BASE_URL}/{path}/", json=payload, timeout=90,
                                 headers={"Content-Type": "application/json"})
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 5 * attempt
                print(f"    429 — backing off {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 * attempt)
    raise RuntimeError(f"{path} failed after {RETRIES} attempts ({last_exc})")


def _window_payload(start: str, end: str) -> dict:
    return {
        "filters": {
            "award_type_codes": CONTRACT_TYPE_CODES,
            "time_period": [{"start_date": start, "end_date": end}],
        },
        "sub_awards": False,
    }


def fetch_counts(start: str, end: str) -> pd.DataFrame:
    body = _post("spending_by_award_count", _window_payload(start, end))
    results = body.get("results", {})
    rows = []
    for code in CONTRACT_TYPE_CODES:
        rows.append({
            "window_start": start,
            "window_end": end,
            "award_type_code": code,
            "award_type": TYPE_NAMES.get(code, code),
            "award_count": results.get(TYPE_RESULT_KEYS.get(code, code), 0),
        })
    return pd.DataFrame(rows)


def fetch_top_awards(start: str, end: str) -> pd.DataFrame:
    payload = _window_payload(start, end)
    payload["fields"] = AWARD_FIELDS
    payload["page"] = 1
    payload["limit"] = PAGE_LIMIT
    payload["sort"] = "Award Amount"
    payload["order"] = "desc"

    frames = []
    for page in range(1, MAX_PAGES + 1):
        payload["page"] = page
        body = _post("spending_by_award", payload)
        results = body.get("results", [])
        if not results:
            break
        frames.append(pd.DataFrame(results))
        time.sleep(REQUEST_GAP)

    if not frames:
        return pd.DataFrame(columns=[c.lower().replace(" ", "_") for c in AWARD_FIELDS])
    df = pd.concat(frames, ignore_index=True)
    df.columns = [c.lower().replace(" ", "_") for c in df.columns]
    return df


def run_window(mode: str, start: str, end: str, today_str: str, fetched_at: str) -> None:
    counts_dir = "storage/raw/usaspending/counts"
    awards_dir = "storage/raw/usaspending/top_awards"

    try:
        counts = fetch_counts(start, end)
        counts["fetched_at"] = fetched_at
        # Window MUST be in the filename: all windows in one run share the same
        # fetched_at partition and would otherwise overwrite each other.
        path = write_partitioned(counts, counts_dir,
                                 f"usaspending_award_counts_{mode}_{start}_{end}.parquet")
        total = int(counts["award_count"].sum())
        print(f"  counts {start}..{end}: {total:,} awards -> {path}")
    except Exception as exc:
        print(f"  counts {start}..{end}: ERROR - {exc}")

    time.sleep(REQUEST_GAP)

    try:
        awards = fetch_top_awards(start, end)
        if awards.empty:
            print(f"  top_awards {start}..{end}: no data")
            return
        awards["window_start"] = start
        awards["window_end"] = end
        awards["fetched_at"] = fetched_at
        path = write_partitioned(awards, awards_dir,
                                 f"usaspending_top_awards_{mode}_{start}_{end}.parquet")
        print(f"  top_awards {start}..{end}: {len(awards):,} rows -> {path}")
    except Exception as exc:
        print(f"  top_awards {start}..{end}: ERROR - {exc}")


def month_windows(start_date: str, end_date: str) -> list[tuple[str, str]]:
    starts = pd.date_range(start_date, end_date, freq="MS")
    ends = starts + pd.offsets.MonthEnd(0)
    windows = []
    for s, e in zip(starts, ends):
        s_str, e_str = s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")
        if s < pd.Timestamp(end_date):
            windows.append((s_str, min(e_str, end_date)))
    return windows


def main() -> None:
    parser = argparse.ArgumentParser(description="USAspending federal contracts (keyless)")
    parser.add_argument("--backfill", action="store_true",
                        help=f"Monthly windows from {DEFAULT_BACKFILL_START}")
    parser.add_argument("--start-date", default=None, help="Backfill start YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=7, help="Incremental lookback days")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    end = now.strftime("%Y-%m-%d")

    mode = "backfill" if args.backfill else "incremental"
    if args.backfill or args.start_date:
        start = args.start_date or DEFAULT_BACKFILL_START
        windows = month_windows(start, end)
    else:
        start = (now - datetime.timedelta(days=args.days)).strftime("%Y-%m-%d")
        windows = [(start, end)]

    print(f"USAspending Pipeline  mode={mode}  windows={len(windows)}\n")
    print("[usaspending_award_counts + usaspending_top_awards]")
    for start_w, end_w in windows:
        run_window(mode, start_w, end_w, today_str, fetched_at)
        time.sleep(REQUEST_GAP)

    print("--- USASPENDING PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
