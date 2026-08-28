#!/usr/bin/env python3
"""
Schwab Universe Backfill — chunked, resumable full-history price pull across
the entire tracked symbol universe (symbol_universe.csv), not just the
63-symbol standard watchlist.

Reuses price_history_pipeline.fetch_symbol() (same fetch/backoff/derived-
columns logic as the watchlist pipeline) but writes in chunks and tracks
progress so a multi-hour run survives interruption -- a single-shot pull
across ~29k symbols would produce 100M+ rows, too large to hold in memory
or write in one pass.

CLI:
  python schwab_universe_backfill.py              # resume/continue full history
  python schwab_universe_backfill.py --chunk-size 250
  python schwab_universe_backfill.py --incremental --days 14   # catch-up to today
  python schwab_universe_backfill.py --incremental --days 14 --skip-empty-from schwab_universe_backfill_progress.json

Progress state: schwab_universe_backfill_progress.json (symbols already
written are skipped on restart). --incremental uses a date-stamped progress
file (schwab_universe_incremental_YYYY-MM-DD.json) so nightly catch-up runs
don't collide with the one-shot full backfill state.

Output:
  storage/raw/prices/year=YYYY/month=MM/prices_universe_batch###_*.parquet   (full)
  storage/raw/prices/year=YYYY/month=MM/prices_universe_incr###_*.parquet    (incremental)
"""

import argparse
import datetime
import json
import os
import time

import pandas as pd
import requests
import schwabdev
from dotenv import load_dotenv

from storage_utils import write_partitioned
from price_history_pipeline import fetch_symbol

load_dotenv()

API_KEY = os.environ["SCHWAB_API_KEY"]
APP_SECRET = os.environ["SCHWAB_APP_SECRET"]
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH = os.environ.get("SCHWAB_TOKEN_PATH", "tokens.db")

OUTPUT_DIR = os.path.join("storage", "raw", "prices")
UNIVERSE_FILE = "symbol_universe.csv"
PROGRESS_FILE = "schwab_universe_backfill_progress.json"
INCREMENTAL_PROGRESS_FILE = "schwab_universe_incremental_progress.json"

REQUEST_INTERVAL = 0.55
DEFAULT_CHUNK_SIZE = 250
DEFAULT_INCREMENTAL_DAYS = 14


def load_progress(progress_file):
    if os.path.exists(progress_file):
        with open(progress_file) as f:
            return json.load(f)
    return {"done": [], "empty": [], "failed": [], "batch_num": 0}


def save_progress(progress, progress_file):
    with open(progress_file, "w") as f:
        json.dump(progress, f, indent=2)


def main(chunk_size=DEFAULT_CHUNK_SIZE, universe_file=UNIVERSE_FILE,
         progress_file=PROGRESS_FILE, incremental=False, days=None,
         skip_empty_from=None):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    universe = pd.read_csv(universe_file)
    all_symbols = universe["symbol"].tolist()
    progress = load_progress(progress_file)
    already_handled = set(progress["done"]) | set(progress["empty"]) | set(progress["failed"])

    if skip_empty_from:
        seed = load_progress(skip_empty_from)
        seeded_empty = set(seed["empty"])
        if seeded_empty:
            print(f"Seeding {len(seeded_empty)} known-empty symbols from "
                  f"{skip_empty_from} (skipped, not re-fetched).")
            already_handled |= seeded_empty

    remaining = [s for s in all_symbols if s not in already_handled]

    print(f"Universe: {len(all_symbols)} symbols total, {len(already_handled)} already "
          f"handled, {len(remaining)} remaining.")

    if not remaining:
        print("Nothing left to do.")
        return

    client = schwabdev.Client(
        app_key=API_KEY, app_secret=APP_SECRET,
        callback_url=CALLBACK_URL, tokens_db=TOKEN_PATH,
    )

    now = datetime.datetime.utcnow()
    end_ms = int(now.timestamp() * 1000)
    if incremental:
        lookback = (days or DEFAULT_INCREMENTAL_DAYS)
        start_dt = now - datetime.timedelta(days=lookback)
        start_ms = int(start_dt.timestamp() * 1000)
        print(f"Mode: INCREMENTAL (last {lookback} days, {start_dt.date()} -> "
              f"{now.date()}); daily bars only.")
    else:
        start_ms = int(datetime.datetime(1970, 1, 2).timestamp() * 1000)
        print("Mode: FULL HISTORY (from 1970 -- Schwab returns all it has).")
    today = now.strftime("%Y%m%d")

    for chunk_start in range(0, len(remaining), chunk_size):
        chunk = remaining[chunk_start:chunk_start + chunk_size]
        results = []
        for i, symbol in enumerate(chunk, 1):
            network_error = False
            df = None
            for attempt in range(1, 4):
                try:
                    df = fetch_symbol(client, symbol, start_ms, end_ms)
                    network_error = False
                    break
                except requests.exceptions.RequestException as e:
                    print(f"  Network error on {symbol} (attempt {attempt}/3): {e}")
                    network_error = True
                    time.sleep(15 * attempt)
            if df is not None and not df.empty:
                results.append(df)
                progress["done"].append(symbol)
            elif network_error:
                progress["failed"].append(symbol)
            else:
                progress["empty"].append(symbol)
            time.sleep(REQUEST_INTERVAL)

        if results:
            combined = pd.concat(results, ignore_index=True)
            progress["batch_num"] += 1
            tag = "incr" if incremental else "universe"
            filename = f"prices_{tag}_batch{progress['batch_num']:04d}_{today}.parquet"
            write_partitioned(combined, OUTPUT_DIR, filename)
            print(f"  batch {progress['batch_num']}: {len(combined)} rows, "
                  f"{len(results)}/{len(chunk)} symbols -> {filename}")
        else:
            print(f"  batch (no symbol name): 0/{len(chunk)} symbols returned data")

        save_progress(progress, progress_file)
        total_done = len(progress["done"]) + len(progress["empty"])
        print(f"  progress: {total_done}/{len(all_symbols)} symbols handled "
              f"({len(progress['done'])} with data, {len(progress['empty'])} empty)")

    print("\n--- UNIVERSE BACKFILL COMPLETE (this run) ---")
    print(f"Total done: {len(progress['done'])}, empty: {len(progress['empty'])}, "
          f"failed: {len(progress['failed'])}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chunked/resumable Schwab full-universe price backfill")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                         help=f"Symbols per write batch (default {DEFAULT_CHUNK_SIZE})")
    parser.add_argument("--universe-file", default=UNIVERSE_FILE,
                         help=f"CSV with a 'symbol' column (default {UNIVERSE_FILE})")
    parser.add_argument("--progress-file", default=PROGRESS_FILE,
                         help=f"Progress state JSON (default {PROGRESS_FILE})")
    parser.add_argument("--incremental", action="store_true",
                         help="Fetch only a trailing window (last --days) instead of full history. "
                              "Uses a date-stamped progress file so nightly runs don't collide with "
                              "the one-shot full backfill state.")
    parser.add_argument("--days", type=int, default=DEFAULT_INCREMENTAL_DAYS,
                         help=f"Trailing window in days for --incremental "
                              f"(default {DEFAULT_INCREMENTAL_DAYS})")
    parser.add_argument("--skip-empty-from", default=None,
                         help="Progress JSON whose 'empty' list is seeded as already-handled "
                              "(avoids re-fetching ~1.6k dead symbols on incremental runs).")
    args = parser.parse_args()
    progress_file = args.progress_file
    if args.incremental:
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        progress_file = f"schwab_universe_incremental_{today}.json"
    main(chunk_size=args.chunk_size, universe_file=args.universe_file,
         progress_file=progress_file, incremental=args.incremental,
         days=args.days, skip_empty_from=args.skip_empty_from)
