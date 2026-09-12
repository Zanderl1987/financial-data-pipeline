#!/usr/bin/env python3
"""
Delisting reference pipeline (Sharadar TICKERS via Nasdaq Data Link).

Downloads the complete security master for Sharadar's US equity universe:
every company the dataset has ever priced, alive or dead, with a stable company
identifier, the first and last date it traded, and an explicit delisted flag.

Why this exists
---------------
Survivorship bias is the single biggest constraint on the strategy catalog:
326 of its 341 records are cross-sectional equity strategies and every one is
ineligible because our price panels contain almost no dead companies. Testing
value, distress, small-cap or leverage signals on a survivor-only panel biases
results upward, worst exactly where the deleted names are the ones that failed.

This pipeline does NOT fix that -- the price history for delisted names sits
behind a paid Sharadar SEP subscription. What it does is turn the problem from
an assumption into a measurement. With a complete delisting map you can ask of
any panel: of the companies that were actually trading on date D, how many do
you contain? That number is the bias, and until now nobody could compute it.

Measured 2026-09-12, this table holds 20,966 companies of which 14,641 (70%)
are delisted, 8,811 of them before 2016. The free alternative we already had --
Tiingo's supported_tickers -- holds 913 pre-2016 delistings, and 60 before 2010
against Sharadar's 6,812. That gap is why this source was chosen.

Two structural facts worth knowing before using it
--------------------------------------------------
1. `permaticker` is a stable company identifier and `ticker` is NOT. Tickers
   are recycled: WM was Washington Mutual and is now Waste Management; WB was
   Wachovia and is now Weibo; GM's series begins 2010 because the pre-
   bankruptcy company is a different entity. A panel keyed on ticker silently
   splices one company's history onto another's, which is worse than a gap
   because it looks continuous. Always join on permaticker.
2. Coverage begins 1998. `firstpricedate` reads 1986-01-01 for many older
   companies, but the delisting counts are empty before 1998 (2 in 1997, 455 in
   1998), so 1986 is a floor value and not a real trading date.

Requires NASDAQ_DATA_LINK_API_KEY. The TICKERS table is available on the free
tier; SEP/SF1/DAILY return only a 2018 sample without a subscription.

CLI:
  python delisting_reference_pipeline.py              # SEP universe (equities)
  python delisting_reference_pipeline.py --table SFP  # funds/ETFs universe
  python delisting_reference_pipeline.py --table all

Output:
  storage/raw/delisting_reference/year=YYYY/month=MM/
      delisting_reference_{table}_{YYYYMMDD}.parquet
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

API_KEY = os.environ.get("NASDAQ_DATA_LINK_API_KEY", "")
ENDPOINT = "https://data.nasdaq.com/api/v3/datatables/SHARADAR/TICKERS.json"
BASE_DIR = "storage/raw/delisting_reference"

PAGE_SIZE = 10000
REQUEST_INTERVAL = 0.25
MAX_RETRIES = 4
TIMEOUT = 180

# Sharadar splits its universe across price tables. SEP is US equities, SFP is
# funds and ETFs. They are fetched separately because the record counts and the
# questions asked of them differ.
TABLES = ("SEP", "SFP")

# Kept from the source. The rest of the security master (SIC codes, sector
# labels, company websites) is real data but none of it answers the
# survivorship question, and a narrower table is a narrower thing to maintain.
COLUMNS = [
    "permaticker", "ticker", "name", "exchange", "isdelisted", "category",
    "cusips", "siccode", "sicsector", "famaindustry", "sector", "industry",
    "scalemarketcap", "currency", "location",
    "firstpricedate", "lastpricedate", "firstquarter", "lastquarter",
    "relatedtickers", "lastupdated",
]


class MissingCredentials(RuntimeError):
    pass


def fetch_table(table):
    """Page through the whole security master for one Sharadar price table."""
    if not API_KEY:
        raise MissingCredentials(
            "NASDAQ_DATA_LINK_API_KEY is not set; the TICKERS table needs a "
            "(free) Nasdaq Data Link account"
        )

    rows, columns, cursor, page = [], None, None, 0
    while True:
        params = {"api_key": API_KEY, "qopts.per_page": PAGE_SIZE, "table": table}
        if cursor:
            params["qopts.cursor_id"] = cursor

        payload = _get_with_retry(params)
        datatable = payload["datatable"]
        columns = columns or [c["name"] for c in datatable["columns"]]
        rows.extend(datatable["data"])
        page += 1
        cursor = (payload.get("meta") or {}).get("next_cursor_id")
        print(f"    page {page}: +{len(datatable['data']):,} -> {len(rows):,}")
        if not cursor:
            break
        time.sleep(REQUEST_INTERVAL)

    frame = pd.DataFrame(rows, columns=columns)
    return frame[[c for c in COLUMNS if c in frame.columns]]


def _get_with_retry(params):
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.get(ENDPOINT, params=params, timeout=TIMEOUT)
        if response.status_code == 429:
            print(f"    429 rate limited, backing off {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Nasdaq Data Link still rate limiting after {MAX_RETRIES} tries")


def summarise(frame, table):
    """Print the coverage facts that decide whether a panel is usable."""
    dates = pd.to_datetime(frame["lastpricedate"], errors="coerce")
    delisted = frame["isdelisted"].eq("Y")
    dead_dates = dates[delisted].dropna()

    print(f"  {table}: {len(frame):,} companies, {int(delisted.sum()):,} delisted "
          f"({delisted.mean():.0%})")
    for label, cutoff in (("2016", "2016-01-01"), ("2010", "2010-01-01"),
                          ("2003", "2003-01-01")):
        print(f"     delisted before {label}: {int((dead_dates < cutoff).sum()):,}")

    # Ticker reuse is the trap this table exists to prevent, so it is measured
    # every run rather than left for a consumer to discover.
    #
    # Note where the hazard actually lives. WITHIN this table there is never a
    # collision: Sharadar keys each company by its FINAL ticker plus a distinct
    # permaticker, so Washington Mutual is WAMUQ and Waste Management keeps WM.
    # The collision appears when joining to a panel keyed on the ticker as it
    # traded AT THE TIME, which is what every price panel we hold is. The
    # `relatedtickers` column carries those historical symbols -- WAMUQ lists
    # WM, LEHMQ lists LEH -- so the number below is the count that matters: a
    # dead company whose old symbol is a live company's symbol today.
    live_symbols = set(frame.loc[~delisted, "ticker"].dropna())
    hazards = 0
    for related in frame.loc[delisted, "relatedtickers"].dropna():
        if live_symbols & set(str(related).split()):
            hazards += 1
    print(f"     delisted companies whose historical ticker is a LIVE ticker "
          f"today: {hazards:,}")
    print(f"     (join on permaticker, never on ticker)")


def main():
    parser = argparse.ArgumentParser(description="Sharadar delisting reference")
    parser.add_argument("--table", choices=list(TABLES) + ["all"], default="SEP",
                        help="SEP = US equities (default), SFP = funds/ETFs")
    parser.add_argument("--backfill", action="store_true",
                        help="accepted for interface consistency; this source is "
                             "a full snapshot every run, so it has no effect")
    arguments = parser.parse_args()

    now = datetime.datetime.utcnow()
    fetched_at = now.isoformat()
    today = now.strftime("%Y%m%d")

    print("Delisting Reference Pipeline (Sharadar TICKERS)\n")
    print("[delisting_reference]")

    wanted = TABLES if arguments.table == "all" else (arguments.table,)
    frames = []
    for table in wanted:
        print(f"  fetching {table} universe")
        frame = fetch_table(table)
        if frame.empty:
            print(f"  {table}: no rows returned")
            continue
        frame.insert(0, "price_table", table)
        summarise(frame, table)
        frames.append(frame)

    if not frames:
        print("  No data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["fetched_at"] = fetched_at
    suffix = arguments.table.lower()
    path = write_partitioned(
        combined, BASE_DIR, f"delisting_reference_{suffix}_{today}.parquet"
    )
    print(f"\n  -> {path}  ({len(combined):,} rows)")
    print("\n--- DELISTING REFERENCE PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
