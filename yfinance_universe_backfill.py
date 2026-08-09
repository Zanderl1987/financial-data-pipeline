#!/usr/bin/env python3
"""
Yahoo Finance Universe Backfill — chunked, resumable, split-adjusted daily
OHLCV for the Russell 3000 universe.

Built 2026-08-08 to close a real gap found auditing a TV-rating backtest: the
Schwab-sourced `prices` table (query.py CATALOG, ~27.7k symbols) has NO
adjusted-close columns and Schwab's price_history API returns unadjusted
closes -- unadjusted stock splits showed up as multi-million-percent single-
day "returns" in backtests run against it (see
experiments/2026-08-08_tv-technical-rating-signal-eval.md). yfinance provides
a correctly split-adjusted `adj_close` for free with no per-symbol quota
(unlike Tiingo's free tier, capped at 500 unique symbols/month -- confirmed
live 2026-08-08, a hard NO-GO for Russell-3000-scale coverage on that plan).
Verified live before building this: yfinance's bulk yf.download() handled 50
symbols in ~2s and correctly reproduced AAPL's 2020 4:1 split and CARE's
2014 split (no spike) with no throttling at that batch size.

Universe: Russell 3000 constituents from the `securities` Iceberg table
(is_russell3000=true, ~2,298 symbols as of 2026-08-08) -- not the full
~29k-symbol symbol_universe.csv Schwab's backfill used; this is scoped to
what full-universe backtesting actually needs.

Uses yf.download()'s bulk multi-ticker mode (not one Ticker.history() call
per symbol) for speed: ~50 symbols per request, full available history
(period="max") per batch. Progress-tracked so a run surviving interruption
resumes rather than re-fetching completed symbols.

CLI:
  python yfinance_universe_backfill.py              # resume/continue
  python yfinance_universe_backfill.py --batch-size 50

Progress state: yfinance_universe_backfill_progress.json (symbols already
written are skipped on restart).

Output:
  storage/raw/yfinance/year=YYYY/month=MM/yfinance_universe_batch###_*.parquet
  (filename-prefixed so query.py's CATALOG glob for this table can't collide
  with the pre-existing market_history_*.parquet files in the same directory)
"""

import argparse
import datetime
import json
import os
import time

import pandas as pd
import yfinance as yf

from storage_utils import write_partitioned

OUTPUT_DIR = os.path.join("storage", "raw", "yfinance")
PROGRESS_FILE = "yfinance_universe_backfill_progress.json"

DEFAULT_BATCH_SIZE = 50
BATCH_PAUSE = 1.0   # seconds between yf.download() batches -- be polite


def load_progress(progress_file):
    if os.path.exists(progress_file):
        with open(progress_file) as f:
            return json.load(f)
    return {"done": [], "empty": [], "failed": [], "batch_num": 0}


def save_progress(progress, progress_file):
    with open(progress_file, "w") as f:
        json.dump(progress, f, indent=2)


def russell3000_symbols() -> "list[str]":
    import query as q
    df = q.sql("SELECT symbol FROM securities WHERE is_russell3000 = true")
    return sorted(df["symbol"].tolist())


def fetch_batch(symbols: "list[str]") -> "dict[str, pd.DataFrame]":
    """One bulk yf.download() call -> {symbol: tidy OHLCV frame}, empty dict
    entries omitted. auto_adjust=False so both raw `close` and split+dividend
    -adjusted `adj_close` are kept (matching tiingo_pipeline.py's convention
    -- analytics/technical.py::_load_ohlcv prefers adj_close when present)."""
    raw = yf.download(symbols, period="max", auto_adjust=False,
                      progress=False, group_by="ticker", threads=True)
    out = {}
    if raw.empty:
        return out
    # single-symbol download collapses the ticker level; normalize to the
    # same shape as the multi-symbol case so downstream code is uniform
    top_level = set(raw.columns.get_level_values(0)) if isinstance(raw.columns, pd.MultiIndex) else set()
    for sym in symbols:
        if isinstance(raw.columns, pd.MultiIndex):
            if sym not in top_level:
                continue
            df = raw[sym].copy()
        else:
            df = raw.copy() if len(symbols) == 1 else None
            if df is None:
                continue
        df = df.reset_index()
        df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
        if "close" not in df.columns:
            continue
        df = df.dropna(subset=["close"])
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.strftime("%Y-%m-%d")
        df["symbol"] = sym
        for col in ("open", "high", "low", "close", "adj_close", "volume"):
            if col not in df.columns:
                df[col] = pd.NA
            df[col] = pd.to_numeric(df[col], errors="coerce")
        out[sym] = df[["symbol", "date", "open", "high", "low", "close",
                       "adj_close", "volume"]]
    return out


def main(batch_size=DEFAULT_BATCH_SIZE, progress_file=PROGRESS_FILE):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    all_symbols = russell3000_symbols()
    progress = load_progress(progress_file)
    already_handled = set(progress["done"]) | set(progress["empty"]) | set(progress["failed"])
    remaining = [s for s in all_symbols if s not in already_handled]

    print(f"Russell 3000 universe: {len(all_symbols)} symbols total, "
         f"{len(already_handled)} already handled, {len(remaining)} remaining.")
    if not remaining:
        print("Nothing left to do.")
        return

    now = datetime.datetime.utcnow()
    today = now.strftime("%Y%m%d")

    for chunk_start in range(0, len(remaining), batch_size):
        chunk = remaining[chunk_start:chunk_start + batch_size]
        try:
            fetched = fetch_batch(chunk)
        except Exception as exc:
            print(f"  batch starting {chunk[0]}: ERROR — {exc}")
            progress["failed"].extend(chunk)
            save_progress(progress, progress_file)
            time.sleep(BATCH_PAUSE)
            continue

        results = []
        for sym in chunk:
            if sym in fetched:
                results.append(fetched[sym])
                progress["done"].append(sym)
            else:
                progress["empty"].append(sym)

        if results:
            combined = pd.concat(results, ignore_index=True)
            combined["fetched_at"] = now.isoformat()
            progress["batch_num"] += 1
            filename = f"yfinance_universe_batch{progress['batch_num']:04d}_{today}.parquet"
            write_partitioned(combined, OUTPUT_DIR, filename)
            print(f"  batch {progress['batch_num']}: {len(combined):,} rows, "
                 f"{len(results)}/{len(chunk)} symbols -> {filename}")
        else:
            print(f"  batch starting {chunk[0]}: 0/{len(chunk)} symbols returned data")

        save_progress(progress, progress_file)
        total_done = len(progress["done"]) + len(progress["empty"])
        print(f"  progress: {total_done}/{len(all_symbols)} symbols handled "
             f"({len(progress['done'])} with data, {len(progress['empty'])} empty, "
             f"{len(progress['failed'])} failed)")
        time.sleep(BATCH_PAUSE)

    print("\n--- YFINANCE UNIVERSE BACKFILL COMPLETE (this run) ---")
    print(f"Total done: {len(progress['done'])}, empty: {len(progress['empty'])}, "
         f"failed: {len(progress['failed'])}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Chunked/resumable Yahoo Finance Russell 3000 adjusted-close backfill")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Symbols per yf.download() batch (default {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--progress-file", default=PROGRESS_FILE,
                        help=f"Progress state JSON (default {PROGRESS_FILE})")
    args = parser.parse_args()
    main(batch_size=args.batch_size, progress_file=args.progress_file)
