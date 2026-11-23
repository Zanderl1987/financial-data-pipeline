"""Launch gate, laptop side: compare runner signals with the stored-table signals.

Run from the main clone (has storage/) with PYTHONPATH = the paper worktree:
    python -u -m paper.check_data_gate <runner_csv> [--universe F]
Pass if every strategy's agreement |A&B| / |A|B| is >= THRESHOLD.
"""
from __future__ import annotations

import argparse
import csv
import sys

from paper.gate_signals import (SIG_END, FETCH_START, read_universe, signal_rows,
                                symbols_path)
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


MAX_PRINT = 50


def _local_signals(universe: str):
    from analytics.technical import _load_ohlcv
    syms = read_universe(universe)
    frames = {}
    for s in syms:
        df = _load_ohlcv(s, PRICE_TABLE, FETCH_START.isoformat(), SIG_END.strftime('%Y-%m-%d'))
        if df is not None and len(df):
            frames[s] = df
    print(f"local frames: {len(frames)} of {len(syms)}", flush=True)
    out = {slug: set() for slug in STRATEGIES}
    for slug, sym, d, side in signal_rows(frames):
        out[slug].add((sym, d, side))
    return out, set(frames)


def main(runner_csv: str, universe: str = "paper/universe.txt",
         diff_out: str | None = None) -> bool:
    runner = _load_csv(runner_csv)
    runner_syms = set(read_universe(symbols_path(runner_csv)))
    local, local_syms = _local_signals(universe)
    common = runner_syms & local_syms
    print(f"symbols: local={len(local_syms)} runner={len(runner_syms)} common={len(common)}")
    print(f"missing from runner (excluded, not failures): {len(local_syms - runner_syms)}")
    print(f"missing from local (excluded, not failures): {len(runner_syms - local_syms)}")
    diff_out = diff_out or str(runner_csv) + ".disagreements.csv"
    ok = True
    with open(diff_out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "where", "symbol", "date", "side"])
        for slug in STRATEGIES:
            a = {x for x in local.get(slug, set()) if x[0] in common}
            b = {x for x in runner.get(slug, set()) if x[0] in common}
            ag = agreement(a, b)
            print(f"{slug}: agreement={ag:.4f} local={len(a)} runner={len(b)} "
                  f"both={len(a & b)} local_only={len(a - b)} runner_only={len(b - a)}")
            diffs = ([("local_only",) + x for x in sorted(a - b)]
                     + [("runner_only",) + x for x in sorted(b - a)])
            for d in diffs:
                w.writerow((slug,) + d)
            for d in diffs[:MAX_PRINT]:
                print(f"  {d[0]:11s} {d[1]} {d[2]} {d[3]}")
            if len(diffs) > MAX_PRINT:
                print(f"  ... {len(diffs) - MAX_PRINT} more in {diff_out}")
            ok &= ag >= THRESHOLD
    print("GATE PASS" if ok else "GATE FAIL")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--universe", default="paper/universe.txt")
    ap.add_argument("--diff-out", default=None)
    a = ap.parse_args()
    sys.exit(0 if main(a.csv, a.universe, a.diff_out) else 1)
