#!/usr/bin/env python3
"""
Schwab Portfolio Pipeline:
  Mirrors your actual Schwab account(s) into the local store —
  daily position snapshots plus transaction history — so backtests and
  analytics can run against real holdings.

  Two tables:
    schwab_positions    — daily snapshot of every position (+ one
                          _ACCOUNT_TOTAL pseudo-row per account carrying
                          liquidation value and cash balance)
    schwab_transactions — TRADE and DIVIDEND_OR_INTEREST activity

  Privacy: account numbers are masked to their last 4 digits; the stable
  account identifier stored is Schwab's own hash (first 8 chars). Parquet
  output lives under storage/ which is gitignored — nothing is committed.

CLI:
  python schwab_portfolio_pipeline.py                 # positions + last 30d txns
  python schwab_portfolio_pipeline.py --days 90
  python schwab_portfolio_pipeline.py --backfill      # txns in 1-yr chunks, 10 yrs
  python schwab_portfolio_pipeline.py --backfill --years 20

Output:
  storage/raw/schwab/positions/year=YYYY/month=MM/schwab_positions_{YYYYMMDD}.parquet
  storage/raw/schwab/transactions/year=YYYY/month=MM/schwab_transactions_{mode}_{YYYYMMDD}.parquet

Schemas:
  schwab_positions:
    date | account | account_last4 | account_type | symbol | asset_type |
    description | quantity | avg_price | market_value | day_pl | day_pl_pct |
    fetched_at
  schwab_transactions:
    account | activity_id | date | type | description | symbol | asset_type |
    quantity | price | amount | fees | net_amount | fetched_at
"""

import os
import time
import datetime
import argparse
import pandas as pd
import schwabdev
from dotenv import load_dotenv

from storage_utils import write_partitioned

load_dotenv()

API_KEY      = os.environ["SCHWAB_API_KEY"]
APP_SECRET   = os.environ["SCHWAB_APP_SECRET"]
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH   = os.environ.get("SCHWAB_TOKEN_PATH", "tokens.db")

POSITIONS_DIR    = os.path.join("storage", "raw", "schwab", "positions")
TRANSACTIONS_DIR = os.path.join("storage", "raw", "schwab", "transactions")

TXN_TYPES = ["TRADE", "DIVIDEND_OR_INTEREST"]
REQUEST_INTERVAL = 0.5


def _mask(account_number: str) -> str:
    s = str(account_number or "")
    return f"***{s[-4:]}" if len(s) >= 4 else "***"


def fetch_positions(client, today, fetched_at):
    """One row per position per account, plus an _ACCOUNT_TOTAL row."""
    response = client.account_details_all(fields="positions")
    if response.status_code != 200:
        print(f"  positions HTTP {response.status_code}: {response.text[:120]}")
        return []

    rows = []
    for entry in response.json():
        acct = entry.get("securitiesAccount", {})
        account = _mask(acct.get("accountNumber"))
        acct_type = acct.get("type")
        balances = acct.get("currentBalances", {}) or {}

        for pos in acct.get("positions", []) or []:
            inst = pos.get("instrument", {}) or {}
            qty = (pos.get("longQuantity") or 0) - (pos.get("shortQuantity") or 0)
            rows.append({
                "date":          today,
                "account":       account,
                "account_last4": account[-4:],
                "account_type":  acct_type,
                "symbol":        inst.get("symbol"),
                "asset_type":    inst.get("assetType"),
                "description":   inst.get("description"),
                "quantity":      qty,
                "avg_price":     pos.get("averagePrice"),
                "market_value":  pos.get("marketValue"),
                "day_pl":        pos.get("currentDayProfitLoss"),
                "day_pl_pct":    pos.get("currentDayProfitLossPercentage"),
                "fetched_at":    fetched_at,
            })

        rows.append({
            "date":          today,
            "account":       account,
            "account_last4": account[-4:],
            "account_type":  acct_type,
            "symbol":        "_ACCOUNT_TOTAL",
            "asset_type":    "TOTAL",
            "description":   "account liquidation value",
            "quantity":      None,
            "avg_price":     None,
            "market_value":  balances.get("liquidationValue"),
            "day_pl":        None,
            "day_pl_pct":    None,
            "fetched_at":    fetched_at,
        })
    return rows


def _flatten_transaction(txn: dict, account: str, fetched_at: str) -> dict:
    """Pick the priced instrument leg; sum currency/fee legs into fees."""
    symbol = asset_type = None
    quantity = price = None
    fees = 0.0
    for item in txn.get("transferItems", []) or []:
        inst = item.get("instrument", {}) or {}
        itype = inst.get("assetType")
        if itype and itype not in ("CURRENCY",) and inst.get("symbol"):
            symbol = inst.get("symbol")
            asset_type = itype
            quantity = item.get("amount")
            price = item.get("price")
        elif item.get("feeType"):
            fees += abs(item.get("cost") or 0)
    return {
        "account":     account,
        "activity_id": txn.get("activityId"),
        "date":        (txn.get("tradeDate") or txn.get("time") or "")[:10],
        "type":        txn.get("type"),
        "description": txn.get("description"),
        "symbol":      symbol,
        "asset_type":  asset_type,
        "quantity":    quantity,
        "price":       price,
        "amount":      txn.get("netAmount"),
        "fees":        round(fees, 4) or None,
        "net_amount":  txn.get("netAmount"),
        "fetched_at":  fetched_at,
    }


def fetch_transactions(client, account_hash, account, start_dt, end_dt, fetched_at):
    """Schwab caps each request at a 1-year span — chunk accordingly."""
    rows = []
    chunk_start = start_dt
    while chunk_start < end_dt:
        chunk_end = min(chunk_start + datetime.timedelta(days=364), end_dt)
        for txn_type in TXN_TYPES:
            response = client.transactions(account_hash, chunk_start, chunk_end, txn_type)
            if response.status_code == 200:
                got = response.json() or []
                if got:
                    print(f"    {chunk_start.date()} -> {chunk_end.date()} "
                          f"{txn_type}: {len(got)}")
                rows.extend(_flatten_transaction(t, account, fetched_at) for t in got)
            else:
                print(f"    txns HTTP {response.status_code} "
                      f"({chunk_start.date()}, {txn_type}): {response.text[:120]}")
            time.sleep(REQUEST_INTERVAL)
        chunk_start = chunk_end + datetime.timedelta(seconds=1)
    return rows


def main(days=30, backfill=False, years=10):
    for d in (POSITIONS_DIR, TRANSACTIONS_DIR):
        os.makedirs(d, exist_ok=True)
    client = schwabdev.Client(
        app_key=API_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        tokens_db=TOKEN_PATH,
    )

    today = datetime.date.today().isoformat()
    fetched_at = datetime.datetime.utcnow().isoformat()
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d")
    mode = "backfill" if backfill else "incremental"
    print(f"Schwab Portfolio Pipeline  date={today}  mode={mode}")

    # account list (hash needed for transactions endpoint)
    response = client.linked_accounts()
    if response.status_code != 200:
        raise SystemExit(f"linked_accounts HTTP {response.status_code}: "
                         f"{response.text[:200]}")
    accounts = [(a["hashValue"], _mask(a.get("accountNumber")))
                for a in response.json()]
    print(f"  linked accounts: {len(accounts)}")

    # ── positions snapshot ──────────────────────────────────────────────
    pos_rows = fetch_positions(client, today, fetched_at)
    if pos_rows:
        pos_df = pd.DataFrame(pos_rows)
        f = write_partitioned(pos_df, POSITIONS_DIR,
                              f"schwab_positions_{stamp}.parquet")
        n_real = (pos_df["symbol"] != "_ACCOUNT_TOTAL").sum()
        print(f"  [schwab_positions] {n_real} positions across "
              f"{len(accounts)} account(s) -> {f}")

    # ── transactions ────────────────────────────────────────────────────
    end_dt = datetime.datetime.utcnow()
    if backfill:
        start_dt = end_dt - datetime.timedelta(days=365 * years)
    else:
        start_dt = end_dt - datetime.timedelta(days=days)

    txn_rows = []
    for account_hash, account in accounts:
        print(f"  transactions for {account} "
              f"({start_dt.date()} -> {end_dt.date()}):")
        txn_rows.extend(fetch_transactions(
            client, account_hash, account, start_dt, end_dt, fetched_at))

    if txn_rows:
        txn_df = pd.DataFrame(txn_rows).drop_duplicates(subset=["activity_id"])
        f = write_partitioned(txn_df, TRANSACTIONS_DIR,
                              f"schwab_transactions_{mode}_{stamp}.parquet")
        print(f"  [schwab_transactions] {len(txn_df)} transactions -> {f}")
    else:
        print("  [schwab_transactions] none in window")

    print("\n--- SCHWAB PORTFOLIO PIPELINE COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Schwab positions + transactions mirror")
    parser.add_argument("--days", type=int, default=30,
                        help="Transaction lookback in days (default 30)")
    parser.add_argument("--backfill", action="store_true",
                        help="Pull transaction history in 1-year chunks")
    parser.add_argument("--years", type=int, default=10,
                        help="Years of history for --backfill (default 10)")
    args = parser.parse_args()
    main(days=args.days, backfill=args.backfill, years=args.years)
