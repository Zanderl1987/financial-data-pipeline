#!/usr/bin/env python3
"""
US Treasury Fiscal Data Pipeline.

Pulls from https://fiscaldata.treasury.gov — no API key required.
Base URL: https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2

Datasets fetched:
  treasury_debt table:
    - Debt to the Penny (daily) — total public debt outstanding split into
      debt held by public and intragovernmental holdings
    - Average Interest Rates on US Treasury Securities — by security type
      (bills, notes, bonds, TIPS, FRNs) and maturity range
    - Interest Expense (monthly FYTD) — government interest expense by
      expense category and group
    - Statement of Net Cost (annual) — gross cost, earned revenue, net cost
      per agency; overall fiscal deficit signal

  treasury_auctions table:
    - Record-Setting Auction Data — historical firsts/bests per auction
      (highest offering, lowest rate, best bid-to-cover, etc.)

Note: Full Treasury Securities Auctions Data requires Enterprise API access
and is not available on the public endpoint.

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

BASE_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2"
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
            elif resp.status_code == 404:
                print(f"  404 Not Found: {url}")
                return None
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(15 * attempt)
    return None


def fetch_paginated(endpoint, sort_field="record_date", date_filter=None):
    """Fetch all pages of a Treasury Fiscal Data endpoint."""
    params = {
        "page[size]":   PAGE_SIZE,
        "page[number]": 1,
        "sort":         sort_field,
    }
    if date_filter:
        params["filter"] = f"record_date:gte:{date_filter}"

    url = f"{BASE_URL}/{endpoint}"
    all_rows = []

    while True:
        data = get_with_backoff(url, params)
        if not data:
            break
        rows = data.get("data", [])
        all_rows.extend(rows)

        meta = data.get("meta", {}).get("pagination", data.get("meta", {}))
        total_pages = meta.get("total_pages", meta.get("total-pages", 1))
        current = params["page[number]"]
        if current >= total_pages or not rows:
            break
        params["page[number]"] = current + 1
        time.sleep(REQUEST_INTERVAL)

    return all_rows


# ---------------------------------------------------------------------------
# Dataset fetchers
# ---------------------------------------------------------------------------

def fetch_debt_to_penny(date_filter=None):
    """Daily total public debt outstanding."""
    rows = fetch_paginated("accounting/od/debt_to_penny",
                           sort_field="record_date", date_filter=date_filter)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    numeric_cols = [c for c in df.columns if c.endswith("_amt") or "debt" in c.lower()]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.rename(columns={"record_date": "date"}) if "record_date" in df.columns else df


def fetch_avg_interest_rates(date_filter=None):
    """Average interest rates on US Treasury securities by type."""
    rows = fetch_paginated("accounting/od/avg_interest_rates",
                           sort_field="record_date", date_filter=date_filter)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in df.columns:
        if "rate" in col.lower() or "amt" in col.lower():
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.rename(columns={"record_date": "date"}) if "record_date" in df.columns else df


def fetch_interest_expense(date_filter=None):
    """Monthly FYTD government interest expense by category."""
    rows = fetch_paginated("accounting/od/interest_expense",
                           sort_field="record_date", date_filter=date_filter)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in df.columns:
        if "amt" in col.lower():
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.rename(columns={"record_date": "date"}) if "record_date" in df.columns else df


def fetch_net_cost(date_filter=None):
    """Annual Statement of Net Cost — gross cost, earned revenue, net cost per agency."""
    rows = fetch_paginated("accounting/od/statement_net_cost",
                           sort_field="record_date", date_filter=date_filter)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in df.columns:
        if "amt" in col.lower() or "bil_amt" in col.lower():
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.rename(columns={"record_date": "date"}) if "record_date" in df.columns else df


def fetch_record_auctions():
    """Record-setting auction data — no date filter (full dataset is small)."""
    rows = fetch_paginated("accounting/od/record_setting_auction",
                           sort_field="record_date", date_filter=None)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in df.columns:
        if any(x in col.lower() for x in ("rate", "amt", "ratio", "offer", "bid")):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.rename(columns={"record_date": "date"}) if "record_date" in df.columns else df


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

    # ---- Debt table: debt_to_penny + avg_interest_rates + interest_expense + net_cost ----
    print("\n[treasury_debt] Fetching debt, rates, interest expense, net cost...")
    debt_frames = []

    debt_df = fetch_debt_to_penny(date_filter)
    if not debt_df.empty:
        debt_df["dataset"] = "debt_to_penny"
        debt_frames.append(debt_df)
        print(f"  debt_to_penny:      {len(debt_df):,} rows")

    rates_df = fetch_avg_interest_rates(date_filter)
    if not rates_df.empty:
        rates_df["dataset"] = "avg_interest_rates"
        debt_frames.append(rates_df)
        print(f"  avg_interest_rates: {len(rates_df):,} rows")

    expense_df = fetch_interest_expense(date_filter)
    if not expense_df.empty:
        expense_df["dataset"] = "interest_expense"
        debt_frames.append(expense_df)
        print(f"  interest_expense:   {len(expense_df):,} rows")

    netcost_df = fetch_net_cost(date_filter)
    if not netcost_df.empty:
        netcost_df["dataset"] = "statement_net_cost"
        debt_frames.append(netcost_df)
        print(f"  statement_net_cost: {len(netcost_df):,} rows")

    if debt_frames:
        combined = pd.concat(debt_frames, ignore_index=True)
        combined["fetched_at"] = now.isoformat()
        path = write_partitioned(combined, debt_dir,
                                 f"treasury_debt_{mode}_{today_str}.parquet")
        print(f"  -> {path}  ({len(combined):,} total rows)")
    else:
        print("  No debt data returned.")

    # ---- Auctions table: record-setting auction data ----
    print("\n[treasury_auctions] Fetching record-setting auction data...")
    rec_df = fetch_record_auctions()
    if not rec_df.empty:
        rec_df["dataset"] = "record_setting_auction"
        rec_df["fetched_at"] = now.isoformat()
        path = write_partitioned(rec_df, auctions_dir,
                                 f"treasury_auctions_{mode}_{today_str}.parquet")
        print(f"  -> {path}  ({len(rec_df):,} rows)")
    else:
        print("  No auction data returned.")

    print("\n--- TREASURY PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
