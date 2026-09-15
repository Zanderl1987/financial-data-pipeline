"""
strategies/ledger.py -- unified paper-trade / forward-book ledger.

Resolves "what should I hold right now" across the paper-trading strategies
into ONE schema, reading each strategy's own persisted artifacts, so the
ledger stays a read-mostly projection (nothing here places trades or
re-writes a strategy's state -- scheduling stays with the user).

Resolvers:
    carry_futures  reads storage/reports/eval/carry_paper/position_current.parquet
                   + book_state.json -- the authoritatively persisted weights of
                   the carry forward book (10% book-vol scaled, long-only).
    tv_survivor    reads the daily portfolio run's persisted OPEN BOOK first
                   (storage/eval_artifacts/tv_survivor_portfolio_daily/
                   open_holdings_current.parquet + open_book_state.json): the
                   true ADMITTED cohort pinned at the data edge, with the
                   allocation share (weight) HRP admission assigned. Since the
                   daily run overwrites this book every day, what the ledger
                   lists is exactly what the portfolio engine decided to hold
                   open -- no replay drift. Weeks when the daily run has not
                   refreshed (no file) fall back to a rule_open replay over the
                   live cache with weight=None (mirrored flag walk; see
                   _open_positions_from_flags).

OUTPUT
    storage/ledger/holdings_current.parquet   unified rows
    storage/ledger/ledger_state.json          per-strategy provenance + notes

COLUMNS
    strategy | symbol | side | weight | signal_date | entry_date | entry_price | source
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation import trades as ev_trades

COLUMNS = ["strategy", "symbol", "side", "weight",
           "signal_date", "entry_date", "entry_price", "source"]

CARRY_PAPER_DIR = os.path.join("storage", "reports", "eval", "carry_paper")
LEDGER_DIR = os.path.join("storage", "ledger")
TV_OPEN_BOOK_DIR = os.path.join("storage", "eval_artifacts",
                                "tv_survivor_portfolio_daily")


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def resolve_carry(paper_dir: "str | None" = None,
                  state_file: "str | None" = None,
                  pos_file: "str | None" = None) -> "tuple[list[dict], dict]":
    """Current carry forward-book holdings from the paper-trade artifacts."""
    paper_dir = paper_dir or CARRY_PAPER_DIR
    state_file = state_file or os.path.join(paper_dir, "book_state.json")
    pos_file = pos_file or os.path.join(paper_dir, "position_current.parquet")
    empty = {"n": 0, "data_edge": None, "signal_date": None,
             "note": "carry forward book not found -- expected book_state.json + "
                     "position_current.parquet; run "
                     "experiments/2026-09-13_carry-paper-trade.py first",
             "source_files": []}
    if not (os.path.exists(state_file) and os.path.exists(pos_file)):
        return [], empty
    with open(state_file, encoding="utf-8") as fh:
        state = json.load(fh)
    weights = pd.read_parquet(pos_file).iloc[:, 0].dropna()
    weights = weights[weights.abs() > 0.0]
    rows = [
        {"strategy": "carry_futures", "symbol": str(sym), "side": "long",
         "weight": round(float(w), 6),
         "signal_date": state.get("live_signal_date"),
         "entry_date": None, "entry_price": None,
         "source": "carry forward book (position_current.parquet)"}
        for sym, w in weights.items()
    ]
    note = ("long-only; weights are the 10% book-vol scaled positions in force, "
            "signal anchored on the last completed month")
    if state.get("construction"):
        note = note + "; " + state["construction"]
    meta = {
        "n": len(rows),
        "data_edge": state.get("last_refresh"),
        "signal_date": state.get("live_signal_date"),
        "pending_rebalance_signal_date": state.get("pending_rebalance_signal_date"),
        "note": note,
        "source_files": [state_file, pos_file],
    }
    return rows, meta


def _open_positions_from_flags(index, close, long_entry, long_exit,
                               short_entry, short_exit) -> "list[dict]":
    """Positions still open at the data edge under the engine's walk rules.

    Mirrors evaluation.trades._next_candidate exactly for the risk-free path
    (exits are purely the rule's exit flags): entries are consumed in
    (bar, side) order with a single cursor; a position whose exit signal
    fires at bar j closes at bar j+1 and the scan resumes at j+2; an entry
    whose exit never fires before the data ends is the symbol's CURRENT open
    position and the scan is terminal for the symbol. The daily paper-trade
    engine keeps such positions silently (closed-only rows), so this is the
    faithful source for the open book."""
    entries = sorted([(int(i), "long") for i in np.flatnonzero(long_entry)] +
                     [(int(i), "short") for i in np.flatnonzero(short_entry)])
    cursor = 0
    n = len(index)
    for i, side in entries:
        if i < cursor:
            continue
        exit_flags = long_exit if side == "long" else short_exit
        exit_sig = None
        for j in range(i + 1, n):
            if exit_flags[j]:
                exit_sig = j
                break
        if exit_sig is None:
            entry_date = str(index[i + 1].date()) if i + 1 < n else None
            entry_price = round(float(close.iloc[i + 1]), 6) if i + 1 < n else None
            return [{"side": side, "signal_date": str(index[i].date()),
                     "entry_date": entry_date, "entry_price": entry_price}]
        cursor = exit_sig + 2
    return []


def _read_open_book(open_book_dir: "str | None" = None) -> "tuple[list[dict], dict] | None":
    """The daily portfolio run's persisted admitted-open cohort, if present."""
    open_book_dir = open_book_dir or TV_OPEN_BOOK_DIR
    parquet_path = os.path.join(open_book_dir, "open_holdings_current.parquet")
    state_path = os.path.join(open_book_dir, "open_book_state.json")
    if not os.path.exists(parquet_path):
        return None
    ob = pd.read_parquet(parquet_path)
    if ob.empty:
        return None
    state = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    rows = []
    for _, r in ob.iterrows():
        weight = r.get("weight")
        weight = float(weight) if weight is not None and pd.notna(weight) else None
        row = {
            "strategy": "tv_survivor",
            "symbol": str(r["symbol"]),
            "side": str(r["side"]),
            "weight": round(weight, 6) if weight is not None else None,
            "signal_date": None if pd.isna(r.get("entry_signal_date")) or r.get("entry_signal_date") is None
                           else str(pd.Timestamp(r["entry_signal_date"]).date()),
            "entry_date": None if pd.isna(r.get("entry_date")) or r.get("entry_date") is None
                          else str(pd.Timestamp(r["entry_date"]).date()),
            "entry_price": None if pd.isna(r.get("entry_price")) or r.get("entry_price") is None
                           else round(float(r["entry_price"]), 6),
            "source": "admitted cohort pinned by the daily portfolio run "
                      "(open_holdings_current.parquet; allocation share = weight)",
        }
        rows.append(row)
    meta = {
        "n": len(rows),
        "slugs": state.get("strategies"),
        "data_edge": state.get("data_edge"),
        "date_range": state.get("date_range"),
        "note": ("true admitted cohort pinned at the daily portfolio run's data "
                 "edge (HRP/fractional allocation shares recorded at admission); "
                 "refreshed by each daily run"),
        "source_files": [parquet_path, state_path],
    }
    return rows, meta


def resolve_tv(cache: "dict | None" = None, slugs: "list[str] | None" = None,
               rule: "object | None" = None,
               open_book_dir: "str | None" = None) -> "tuple[list[dict], dict]":
    """TV survivor holdings currently open, from the daily run's open book
    when one is present, else a rule-open replay over the live cache.

    The persisted open book is the authoritative source (the admitted cohort
    with its allocation shares).  The flag replay is the fallback for
    windows when no daily run has written a book: positions the survivor
    union rule has left open at the cache's data edge, weight=None."""
    from strategies import portfolio as tv_portfolio
    persisted = _read_open_book(open_book_dir)
    if persisted is not None:
        return persisted
    if slugs is None and rule is not None:
        slugs = [rule.name]
    if slugs is None:
        slugs = tv_portfolio.survivor_slugs()
    if not slugs:
        return [], {"n": 0, "slugs": [], "data_edge": None,
                    "note": "no Stage 5 survivors -- catalog has no stage5 rows yet",
                    "source_files": []}
    if rule is None:
        rule = tv_portfolio.combine_survivors(slugs)
    if cache is None:
        cache = tv_portfolio._load_live_cache()
    rows = []
    for sym, df in cache.items():
        if df is None or df.empty or "close" not in df.columns:
            continue
        le, lx, se, sx = ev_trades.rule_flags(rule, df)
        for hit in _open_positions_from_flags(df.index, df["close"], le, lx, se, sx):
            rows.append({"strategy": "tv_survivor", "symbol": str(sym),
                         "side": hit["side"], "weight": None,
                         "signal_date": hit["signal_date"],
                         "entry_date": hit["entry_date"],
                         "entry_price": hit["entry_price"],
                         "source": "rule-open replay (no daily open book on disk); "
                                   "weights set by the next portfolio run"})
    edges = [df.index.max() for df in cache.values()
             if df is not None and not df.empty and "close" in df.columns]
    meta = {
        "n": len(rows),
        "slugs": sorted(slugs),
        "data_edge": str(max(edges).strftime("%Y-%m-%d")) if edges else None,
        "note": ("fallback replay of the survivor union rule entry/exit flags "
                 "(no open_holdings_current.parquet from a daily portfolio run); "
                 "only positions still open at the data edge are listed; "
                 "HRP weights are assigned at the next portfolio run"),
        "source_files": [],
    }
    return rows, meta


def current_holdings(strategies: "tuple[str, ...]" = ("carry", "tv"),
                     *, cache: "dict | None" = None) -> "tuple[pd.DataFrame, dict]":
    """Unified holdings across the requested strategies."""
    rows = []
    meta = {}
    for key in strategies:
        if key == "carry":
            r, m = resolve_carry()
        elif key == "tv":
            r, m = resolve_tv(cache=cache)
        else:
            raise ValueError(f"unknown strategy {key!r}")
        rows.extend(r)
        meta[key] = m
    return pd.DataFrame(rows, columns=COLUMNS), meta


def write_snapshot(df: pd.DataFrame, meta: dict, out_dir: "str | None" = None,
                   run_ts: "str | None" = None) -> dict:
    """Persist the unified holdings + per-strategy provenance to storage/ledger."""
    out_dir = out_dir or LEDGER_DIR
    os.makedirs(out_dir, exist_ok=True)
    run_ts = run_ts or _utc_ts()
    parquet_path = os.path.join(out_dir, "holdings_current.parquet")
    df.to_parquet(parquet_path, index=False)
    json_path = os.path.join(out_dir, "ledger_state.json")
    state = {"run_ts": run_ts,
             "report": "holdings the paper-trading strategies intend to hold now",
             "strategies": meta, "rows": int(len(df))}
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    return {"parquet": parquet_path, "json": json_path}


def _print_ledger(df: pd.DataFrame, meta: dict) -> None:
    if df.empty:
        print("Ledger: no current holdings.")
    else:
        print("CURRENT PAPER-TRADE HOLDINGS (unified ledger)")
        print(df.to_string(index=False))
    for key, m in meta.items():
        print(f"  {key}: n={m['n']} data_edge={m.get('data_edge')} {m['note']}")


def _parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", choices=["carry", "tv", "all"], default="all",
                    help="resolver(s) to run (default: all)")
    ap.add_argument("--write", action="store_true",
                    help="persist holdings + state to storage/ledger/")
    ap.add_argument("--symbols", nargs="+", default=None,
                    help="tv resolver: symbols (default: dev_cache universe)")
    ap.add_argument("--lookback", type=int, default=252,
                    help="tv resolver: lookback days for price history (default 252)")
    return ap.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    from strategies import portfolio as tv_portfolio
    cache = None
    if args.strategy in ("tv", "all"):
        cache = tv_portfolio._load_live_cache(symbols=args.symbols,
                                              lookback_days=args.lookback)
    keys = ("carry", "tv") if args.strategy == "all" else (args.strategy,)
    ledger_df, ledger_meta = current_holdings(keys, cache=cache)
    _print_ledger(ledger_df, ledger_meta)
    if args.write:
        info = write_snapshot(ledger_df, ledger_meta)
        print(f"  wrote {info['parquet']}")
        print(f"  wrote {info['json']}")