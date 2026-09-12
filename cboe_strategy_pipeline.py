#!/usr/bin/env python3
"""
CBOE strategy benchmark index pipeline.

Downloads daily levels for CBOE's rules-based option-strategy benchmark
indices. These are not volatility gauges like VIX -- they are the total-return
levels of mechanically executed option strategies, computed daily by the
exchange and published without revision.

  PUT   — S&P 500 PutWrite (cash-secured ATM monthly puts), from 1991
  WPUT  — S&P 500 Weekly PutWrite
  PUTR  — S&P 500 PutWrite 30-delta
  BXM   — S&P 500 BuyWrite (covered call, ATM)
  BXMD  — S&P 500 BuyWrite 30-delta
  BXD   — DJIA BuyWrite
  BXN   — Nasdaq-100 BuyWrite
  CLL   — S&P 500 95-110 Collar
  CNDR  — S&P 500 Iron Condor, from 1986
  BFLY  — S&P 500 Iron Butterfly
  CMBO  — S&P 500 Covered Combo

Why this exists
---------------
The strategy catalog wants to evaluate index put-writing, but the store's
options history covers 4 symbols starting 2023-12 -- nowhere near enough to
reconstruct three decades of option trades. These index series sidestep the
problem entirely: the exchange already executed the strategy, daily, for 30+
years, under a fixed published rulebook. That is closer to out-of-sample
evidence than any backtest we could run, and it costs one HTTP request.

The catch, recorded here so it is not forgotten downstream: CBOE both designs
and publishes these indices, and they list the options the strategies trade.
The series are mechanical and unrevised, but the *selection* of which
strategies get a published benchmark is not neutral.

No API key required. Full history on every run; the source has no date
windowing.

CLI:
  python cboe_strategy_pipeline.py

Output:
  storage/raw/cboe_strategy/year=YYYY/month=MM/cboe_strategy_indices_{date}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd

from cboe_pipeline import fetch_index
from storage_utils import write_partitioned

BASE_DIR = "storage/raw/cboe_strategy"
REQUEST_GAP = 1.0

INDICES = [
    ("PUT", "PUT_History.csv"),
    ("WPUT", "WPUT_History.csv"),
    ("PUTR", "PUTR_History.csv"),
    ("BXM", "BXM_History.csv"),
    ("BXMD", "BXMD_History.csv"),
    ("BXD", "BXD_History.csv"),
    ("BXN", "BXN_History.csv"),
    ("CLL", "CLL_History.csv"),
    ("CNDR", "CNDR_History.csv"),
    ("BFLY", "BFLY_History.csv"),
    ("CMBO", "CMBO_History.csv"),
]

# What each index actually trades, so a consumer does not have to look it up.
DESCRIPTIONS = {
    "PUT": "S&P 500 PutWrite: cash-secured at-the-money monthly puts",
    "WPUT": "S&P 500 Weekly PutWrite: cash-secured ATM weekly puts",
    "PUTR": "S&P 500 30-delta PutWrite",
    "BXM": "S&P 500 BuyWrite: long index, short ATM monthly call",
    "BXMD": "S&P 500 BuyWrite, 30-delta call",
    "BXD": "DJIA BuyWrite",
    "BXN": "Nasdaq-100 BuyWrite",
    "CLL": "S&P 500 95-110 Collar: long index, long 95% put, short 110% call",
    "CNDR": "S&P 500 Iron Condor",
    "BFLY": "S&P 500 Iron Butterfly",
    "CMBO": "S&P 500 Covered Combo: short put plus covered call",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="CBOE strategy benchmark indices")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="accepted for symmetry with other pipelines; the source is always full history",
    )
    parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()

    os.makedirs(BASE_DIR, exist_ok=True)

    print("CBOE Strategy Index Pipeline\n")
    print("[cboe_strategy_indices]")

    frames = []
    for name, filename in INDICES:
        try:
            frame = fetch_index(name, filename)
            frame["description"] = DESCRIPTIONS.get(name)
            span = f"{frame['date'].min()}..{frame['date'].max()}" if len(frame) else "-"
            print(f"  {name}: {len(frame):,} rows  {span}")
            frames.append(frame)
        except Exception as exc:
            print(f"  {name}: ERROR — {exc}")
        time.sleep(REQUEST_GAP)

    if not frames:
        print("  No data retrieved")
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["fetched_at"] = fetched_at
    path = write_partitioned(
        combined, BASE_DIR, f"cboe_strategy_indices_{today_str}.parquet"
    )
    print(f"  -> {path}  ({len(combined):,} rows)")

    print("\n--- CBOE STRATEGY PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
