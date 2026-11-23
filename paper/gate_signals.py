"""Launch gate, runner side: entry signals computed from freshly fetched Yahoo bars.

Runs on the GitHub runner (no storage/). Writes (strategy, symbol, date, side)
for every entry signal dated SIG_START..SIG_END. Logs print counts only.

    python -m paper.gate_signals --out gate_signals.csv [--universe paper/universe.txt]
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
from pathlib import Path

import pandas as pd

from paper.data import fetch_daily_bars
from paper.strategies import STRATEGIES

FETCH_START = date(2021, 8, 7)
FETCH_END = date(2026, 8, 8)           # yfinance end is exclusive: last bar 2026-08-07
SIG_START = pd.Timestamp("2026-02-09")  # window ends at the last date the stored table
SIG_END = pd.Timestamp("2026-08-07")    # covers the whole universe


def signal_rows(frames: dict) -> list[tuple]:
    """(strategy, symbol, date, side) for every entry signal in the gate window."""
    rows = []
    for slug, spec in STRATEGIES.items():
        for sym in sorted(frames):
            df = frames[sym]
            ent = spec.entries(df)
            for side in ("long", "short"):
                s = ent[side]
                s = s[(s.index >= SIG_START) & (s.index <= SIG_END) & s.to_numpy(bool)]
                for d in s.index:
                    rows.append((slug, sym, d.strftime("%Y-%m-%d"), side))
    return rows


def symbols_path(out: str) -> str:
    """Sidecar listing the symbols Yahoo actually served (the CSV alone cannot say)."""
    return str(out) + ".symbols.txt"


def read_universe(path: str) -> list[str]:
    return [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]


def main(out: str, universe: str = "paper/universe.txt"):
    syms = read_universe(universe)
    print(f"universe: {len(syms)} symbols", flush=True)
    frames = fetch_daily_bars(syms, FETCH_START, FETCH_END)
    print(f"symbols fetched: {len(frames)}  missing: {len(syms) - len(frames)}", flush=True)
    rows = signal_rows(frames)
    Path(symbols_path(out)).write_text("\n".join(sorted(frames)) + "\n", encoding="utf-8")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "symbol", "date", "side"])
        w.writerows(rows)
    for slug in STRATEGIES:
        print(f"{slug}: {sum(1 for r in rows if r[0] == slug)} signals", flush=True)
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--universe", default="paper/universe.txt")
    a = ap.parse_args()
    main(a.out, a.universe)
