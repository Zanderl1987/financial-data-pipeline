#!/usr/bin/env python3
"""
US Treasury Fiscal Data Pipeline.

Pulls from https://fiscaldata.treasury.gov — no API key required.

Datasets fetched:
  - Debt to the Penny (daily) — total public debt outstanding,
    broken into debt held by public and intragovernmental holdings
  - Average Interest Rates on US Treasury Securities — by security type
    (bills, notes, bonds, TIPS, FRNs) and maturity range
  - Treasury Securities Auctions — historical auction results including
    offering amount, bid-to-cover ratio, high rate, CUSIP, issue/maturity dates
  - Daily Treasury Statement (DTS) — operating cash balance (TGA), withdrawals,
    deposits, and net position for each business day

CLI:
  python treasury_pipeline.py             # incremental (last 365 days)
  python treasury_pipeline.py --backfill  # full available history

Outputs:
  storage/raw/treasury/debt/treasury_debt_{mode}_{YYYYMMDD}.parquet
  storage/raw/treasury/auctions/treasury_auctions_{mode}_{YYYYMMDD}.parquet
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

BASE_URL = "https://api.fiscaldata.treasury.gov/services/api/v1"
BASE_DIR = os.path.join("storage", "raw", "treasury")
PAGE_SIZE = 10000
REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3


def get_with_backoff(url, params=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 30 * attempt
                print(f"  429 rate limit. Waiting {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(15 * attempt)
    return None


def fetch_paginated(endpoint, params=None, date_filter=None):
    """
    Fetch all pages of a Treasury Fiscal Data endpoint.
    date_filter: ISO string like '2024-01-01' — filters record_date >= this
    """
    params = dict(params or {})
    params["page[size]"] = PAGE_SIZE
    params["page[number]"] = 1
    if date_filter:
        params["filter"] = f"record_date:gte:{date_filter}"

    all_rows = []
    url = f"{BASE_URL}/{endpoint}"

    while True:
        data = get_with_backoff(url, params)
        if not data:
            break
        rows = data.get("data", [])
        all_rows.extend(rows)

        meta = data.get("meta", {})
        total_pages = meta.get("total-pages", 1)
        current_page = params["page[number]"]
        if current_page >= total_pages or not rows:
            break

        params["page[number]"] = current_page + 1
        time.sleep(REQUEST_INTERVAL)

    return all_rows


# ---------------------------------------------------------------------------
# Dataset fetchers
# ---------------------------------------------------------------------------

def fetch_debt_to_penny(date_filter=None):
    """
    Debt to the Penny — daily total public debt outstanding.
    Fields: record_date, debt_held_public_amt, intragov_hold_amt, tot_pub_debt_out_amt
    """
    rows = fetch_paginated(
        "accounting/od/debt_to_penny",
        params={"sort": "record_date"},
        date_filter=date_filter,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    numeric_cols = ["debt_held_public_amt", "intragov_hold_amt", "tot_pub_debt_out_amt"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.rename(columns={
        "record_date":             "date",
        "debt_held_public_amt":    "debt_held_public",
        "intragov_hold_amt":       "debt_intragovernmental",
        "tot_pub_debt_out_amt":    "debt_total",
    })
    return df


def fetch_avg_interest_rates(date_filter=None):
    """
    Average Interest Rates on US Treasury Securities.
    Fields: record_date, security_type_desc, security_desc, avg_interest_rate_amt
    Covers bills, notes, bonds, TIPS, FRNs, total marketable, total nonmarketable
    """
    rows = fetch_paginated(
        "accounting/od/avg_interest_rates",
        params={"sort": "record_date"},
        date_filter=date_filter,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "avg_interest_rate_amt" in df.columns:
        df["avg_interest_rate_amt"] = pd.to_numeric(df["avg_interest_rate_amt"], errors="coerce")
    df = df.rename(columns={
        "record_date":            "date",
        "security_type_desc":     "security_type",
        "security_desc":          "security_name",
        "avg_interest_rate_amt":  "avg_interest_rate",
    })
    return df


def fetch_auctions(date_filter=None):
    """
    Treasury Securities Auctions — historical results.
    Includes offering amount, bid-to-cover, high rate, CUSIP, issue/maturity dates.
    """
    rows = fetch_paginated(
        "accounting/od/securities_auctions",
        params={"sort": "-auction_date"},
        date_filter=date_filter,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    numeric_cols = [
        "offering_amt", "total_accepted", "total_tendered",
        "bid_to_cover_ratio", "high_rate", "int_rate",
        "price_per100", "allotted_at_high",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "auction_date" in df.columns:
        df = df.rename(columns={"auction_date": "date"})
    return df


def fetch_dts_operating_cash(date_filter=None):
    """
    Daily Treasury Statement — closing balance of Treasury General Account (TGA)
    and daily operating cash summary.
    """
    rows = fetch_paginated(
        "accounting/dts/dts_table_1",
        params={"sort": "record_date"},
        date_filter=date_filter,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    numeric_cols = [c for c in df.columns if "amt" in c.lower() or "balance" in c.lower()]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "record_date" in df.columns:
        df = df.rename(columns={"record_date": "date"})
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="US Treasury Fiscal Data pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full available history")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"

    if args.backfill:
        date_filter = None
        print("Mode: BACKFILL (full history)")
    else:
        cutoff = (now - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
        date_filter = cutoff
        print(f"Mode: INCREMENTAL (from {cutoff})")

    debt_dir = os.path.join(BASE_DIR, "debt")
    auctions_dir = os.path.join(BASE_DIR, "auctions")
    for d in [debt_dir, auctions_dir]:
        os.makedirs(d, exist_ok=True)

    # ---- Debt to the Penny ----
    print("\n[treasury_debt] Fetching debt-to-penny + avg interest rates...")
    debt_df = fetch_debt_to_penny(date_filter)
    rates_df = fetch_avg_interest_rates(date_filter)

    debt_frames = []
    if not debt_df.empty:
        debt_df["dataset"] = "debt_to_penny"
        debt_df["fetched_at"] = now.isoformat()
        debt_frames.append(debt_df)
        print(f"  Debt to penny: {len(debt_df):,} rows")

    if not rates_df.empty:
        rates_df["dataset"] = "avg_interest_rates"
        rates_df["fetched_at"] = now.isoformat()
        debt_frames.append(rates_df)
        print(f"  Avg interest rates: {len(rates_df):,} rows")

    if debt_frames:
        combined_debt = pd.concat(debt_frames, ignore_index=True)
        path = write_partitioned(
            combined_debt, debt_dir,
            f"treasury_debt_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(combined_debt):,} total rows)")
    else:
        print("  No debt data returned.")

    # ---- Auctions ----
    print("\n[treasury_auctions] Fetching auction results...")
    auctions_df = fetch_auctions(date_filter)
    if not auctions_df.empty:
        auctions_df["fetched_at"] = now.isoformat()
        path = write_partitioned(
            auctions_df, auctions_dir,
            f"treasury_auctions_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(auctions_df):,} rows)")
    else:
        print("  No auction data returned.")

    # ---- Daily Treasury Statement ----
    print("\n[treasury_debt] Fetching DTS operating cash balance (TGA)...")
    dts_df = fetch_dts_operating_cash(date_filter)
    if not dts_df.empty:
        dts_df["dataset"] = "dts_operating_cash"
        dts_df["fetched_at"] = now.isoformat()
        path = write_partitioned(
            dts_df, debt_dir,
            f"treasury_dts_{mode}_{today_str}.parquet",
        )
        print(f"  -> {path}  ({len(dts_df):,} rows)")
    else:
        print("  No DTS data returned (endpoint may not be available).")

    print("\n--- TREASURY PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
