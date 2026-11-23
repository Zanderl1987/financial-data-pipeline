"""Launch gate, laptop side: compare runner signals with the stored-table signals.

Run from the main clone (has storage/) with PYTHONPATH = the paper worktree:
    python -u -m paper.check_data_gate <runner_csv> [--universe F]
Pass if every strategy's agreement |A&B| / |A|B| is >= THRESHOLD.
"""
from __future__ import annotations

import argparse
import csv
import sys

from paper.gate_signals import (FETCH_END, FETCH_START, read_universe, signal_rows)
from paper.strategies import STRATEGIES

THRESHOLD = 0.99
PRICE_TABLE = "yfinance_universe_prices"


def agreement(a: set, b: set) -> float:
    u = a | b
    return 1.0 if not u else len(a & b) / len(u)


def _load_csv(path: str) -> dict[str, set]:
    out = {slug: set() for slug in STRATEGIES}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["strategy"], set()).add((r["symbol"], r["date"], r["side"]))
    return out


def _local_signals(universe: str) -> dict[str, set]:
    from analytics.technical import _load_ohlcv
    syms = read_universe(universe)
    frames = {}
    for s in syms:
        df = _load_ohlcv(s, PRICE_TABLE, FETCH_START.isoformat(), FETCH_END.isoformat())
        if df is not None and len(df):
            frames[s] = df
    print(f"local frames: {len(frames)} of {len(syms)}", flush=True)
    out = {slug: set() for slug in STRATEGIES}
    for slug, sym, d, side in signal_rows(frames):
        out[slug].add((sym, d, side))
    return out


def main(runner_csv: str, universe: str = "paper/universe.txt") -> bool:
    runner = _load_csv(runner_csv)
    local = _local_signals(universe)
    ok = True
    for slug in STRATEGIES:
        a, b = local.get(slug, set()), runner.get(slug, set())
        ag = agreement(a, b)
        print(f"{slug}: agreement={ag:.4f} local={len(a)} runner={len(b)} "
              f"both={len(a & b)} local_only={len(a - b)} runner_only={len(b - a)}")
        for x in sorted(a - b):
            print(f"  local_only  {x[0]} {x[1]} {x[2]}")
        for x in sorted(b - a):
            print(f"  runner_only {x[0]} {x[1]} {x[2]}")
        ok &= ag >= THRESHOLD
    print("GATE PASS" if ok else "GATE FAIL")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--universe", default="paper/universe.txt")
    a = ap.parse_args()
    sys.exit(0 if main(a.csv, a.universe) else 1)
