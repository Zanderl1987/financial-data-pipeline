#!/usr/bin/env python3
"""
Congressional Trades Pipeline.

Downloads US House and Senate stock trade disclosures aggregated from official
financial disclosure filings. No API key required.

Data sources (community-maintained aggregators of official SEC/Congress disclosures):
  Senate: senate-stock-watcher-data.s3-us-west-2.amazonaws.com
  House:  house-stock-watcher-data.s3-us-west-2.amazonaws.com

Both sources pull from official government disclosures (Senate eFD, House Clerk).

CLI:
  python congressional_trades_pipeline.py             # all available disclosures
  python congressional_trades_pipeline.py --backfill  # same (full dataset in one file)

Outputs:
  storage/raw/congressional_trades/senate/congressional_senate_{mode}_{YYYYMMDD}.parquet
  storage/raw/congressional_trades/house/congressional_house_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import os

import pandas as pd
import requests
from storage_utils import write_partitioned

SENATE_URL = ("https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com"
              "/aggregate/all_transactions.json")
HOUSE_URL  = ("https://house-stock-watcher-data.s3-us-west-2.amazonaws.com"
              "/data/all_transactions.json")

BASE_DIR   = os.path.join("storage", "raw", "congressional_trades")
SENATE_DIR = os.path.join(BASE_DIR, "senate")
HOUSE_DIR  = os.path.join(BASE_DIR, "house")
MAX_RETRIES = 3


def _get_json(url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=120)
            if resp.status_code == 200:
                return resp.json()
            print(f"  HTTP {resp.status_code} (attempt {attempt})")
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
    return None


def _to_date(raw):
    if not raw:
        return None
    try:
        return pd.to_datetime(raw, errors="coerce").strftime("%Y-%m-%d")
    except Exception:
        return None


def parse_senate(raw, fetched_at):
    rows = []
    # Senate JSON: list of {senator, transactions: [...]}
    for item in (raw if isinstance(raw, list) else []):
        senator = item.get("senator", "")
        for txn in item.get("transactions", []):
            rows.append({
                "chamber":           "senate",
                "member_name":       senator,
                "ticker":            (txn.get("ticker") or "").upper().strip() or None,
                "asset_description": txn.get("asset_description"),
                "transaction_type":  txn.get("type"),
                "transaction_date":  _to_date(txn.get("transaction_date")),
                "amount_range":      txn.get("amount"),
                "comment":           txn.get("comment"),
                "fetched_at":        fetched_at,
            })
    df = pd.DataFrame(rows)
    if not df.empty and "transaction_date" in df.columns:
        df["date"] = df["transaction_date"]
    return df


def parse_house(raw, fetched_at):
    rows = []
    # House JSON: flat list of transaction dicts
    for txn in (raw if isinstance(raw, list) else []):
        rows.append({
            "chamber":           "house",
            "member_name":       txn.get("representative"),
            "ticker":            (txn.get("ticker") or "").upper().strip() or None,
            "asset_description": txn.get("asset_description"),
            "transaction_type":  txn.get("type"),
            "transaction_date":  _to_date(txn.get("transaction_date")),
            "disclosure_date":   _to_date(txn.get("disclosure_date")),
            "amount_range":      txn.get("amount"),
            "district":          txn.get("district"),
            "state":             txn.get("state"),
            "party":             txn.get("party"),
            "fetched_at":        fetched_at,
        })
    df = pd.DataFrame(rows)
    if not df.empty and "transaction_date" in df.columns:
        df["date"] = df["transaction_date"]
    return df


def main():
    parser = argparse.ArgumentParser(
        description="Congressional stock trade disclosures (keyless)")
    parser.add_argument("--backfill", action="store_true",
                        help="Full history (same as default — all disclosures in one fetch)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"

    os.makedirs(SENATE_DIR, exist_ok=True)
    os.makedirs(HOUSE_DIR, exist_ok=True)

    print("Congressional Trades Pipeline  (keyless, official disclosure aggregator)")

    # ── Senate ─────────────────────────────────────────────────────────────────
    print("\n[senate_trades]  Fetching senate disclosures...")
    raw_senate = _get_json(SENATE_URL)
    if raw_senate:
        df_senate = parse_senate(raw_senate, fetched_at)
        df_senate = df_senate[df_senate["member_name"].notna()]
        path = write_partitioned(df_senate, SENATE_DIR,
                                 f"congressional_senate_{mode}_{today_str}.parquet")
        print(f"  -> {path}  ({len(df_senate):,} rows, "
              f"{df_senate['member_name'].nunique()} senators)")
    else:
        print("  Senate data unavailable.")

    # ── House ──────────────────────────────────────────────────────────────────
    print("\n[house_trades]  Fetching house disclosures...")
    raw_house = _get_json(HOUSE_URL)
    if raw_house:
        df_house = parse_house(raw_house, fetched_at)
        df_house = df_house[df_house["member_name"].notna()]
        path = write_partitioned(df_house, HOUSE_DIR,
                                 f"congressional_house_{mode}_{today_str}.parquet")
        print(f"  -> {path}  ({len(df_house):,} rows, "
              f"{df_house['member_name'].nunique()} representatives)")
    else:
        print("  House data unavailable.")

    print("\n--- CONGRESSIONAL TRADES PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
