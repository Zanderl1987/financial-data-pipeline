"""Daily live job: decide and submit market-on-close orders for the six paper
accounts. stdout is one line per account and nothing else (the repo is public).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from paper.accounts import ACCOUNTS
from paper.broker import Broker
from paper.data import fetch_daily_bars
from paper.decide import accrued_borrow_fee, decide_orders
from paper.runlog import append_run
from paper.strategies import STRATEGIES

UNIVERSE = Path(__file__).with_name("universe.txt")
WINDOW_START = time(9, 35)
WINDOW_END = time(15, 40)
MAX_NOTES = 50


def _in_window(now_et: datetime, close_et: datetime | None) -> bool:
    end = datetime.combine(now_et.date(), WINDOW_END, tzinfo=now_et.tzinfo)
    if close_et is not None:
        end = min(end, close_et - timedelta(minutes=20))
    start = datetime.combine(now_et.date(), WINDOW_START, tzinfo=now_et.tzinfo)
    return start <= now_et <= end


def _creds(spec):
    return os.environ[spec.env_key], os.environ[spec.env_secret]


def run(today: date | None = None, dry_run: bool = False, broker_factory=Broker,
        log=append_run, fetch=fetch_daily_bars) -> int:
    try:
        first = broker_factory(*_creds(ACCOUNTS[0]))
        now_et, is_open_day = first.clock()
        close_et = first.close_et(now_et.date()) if hasattr(first, "close_et") else None
    except Exception as e:
        print("clock: error, 0 orders")
        print(f"clock error: {type(e).__name__}", file=sys.stderr)
        return 1
    if not is_open_day or not _in_window(now_et, close_et):
        return 0
    today = today or now_et.date()
    prev = first.prev_trading_day(today)

    universe = [s.strip() for s in UNIVERSE.read_text().splitlines() if s.strip()]
    start = today - timedelta(days=5 * 365)
    frames = fetch(universe, start, today + timedelta(days=1), today=today)

    repo = os.environ.get("PAPER_LOG_REPO", "")
    token = os.environ.get("PAPER_LOG_TOKEN", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    rc = 0
    for acct in ACCOUNTS:
        status, n_orders, notes, skip = "error", 0, [], False
        try:
            broker = broker_factory(*_creds(acct))
            if broker.orders_today(f"pt|{acct.key}|{today:%Y%m%d}"):
                status, skip = "already_done", True
            else:
                equity, lots = broker.fee_inputs()
                fee_adj = equity - accrued_borrow_fee(lots, today)
                dec = decide_orders(
                    acct, STRATEGIES[acct.slug], frames, broker.held(), today,
                    prev, fee_adj, broker.shortable,
                    broker.recently_exited(prev), len(universe))
                notes = list(dec.notes)
                if dry_run:
                    status, n_orders = "dry_run", len(dec.orders)
                else:
                    status = dec.status
                    for intent in dec.orders:
                        try:
                            broker.submit(intent)
                            n_orders += 1
                        except Exception as e:
                            notes.append(
                                f"submit_failed:{intent.symbol}:{type(e).__name__}")
        except Exception as e:
            status, n_orders, notes = "error", 0, [type(e).__name__]
            rc = 1
        print(f"{acct.key}: {status}, {n_orders} orders")
        if skip:
            continue
        if len(notes) > MAX_NOTES:
            notes = notes[:MAX_NOTES] + [f"...+{len(notes) - MAX_NOTES} more"]
        try:
            log({"date": f"{today:%Y-%m-%d}", "account": acct.key, "status": status,
                 "n_orders": n_orders, "notes": notes, "run_id": run_id},
                repo, token)
        except Exception as e:
            print(f"log write failed: {type(e).__name__}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    sys.exit(run(dry_run=ap.parse_args().dry_run))
