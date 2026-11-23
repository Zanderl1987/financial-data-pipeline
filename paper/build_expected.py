"""Build the pre-launch expected ranges for the six paper accounts.

Run from the main clone (has storage/) with PYTHONPATH = this worktree:
    python -u -m paper.build_expected [--max-symbols N] [--out F] [--universe-out F]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import paper
from paper.accounts import ACCOUNTS
from paper.expected import normal_band
from paper.sim import simulate_account
from paper.strategies import STRATEGIES

START, END, HORIZON = "2010-01-01", "2026-08-07", 63
_PKG_ROOT = Path(paper.__file__).resolve().parent.parent


def _trades_per_window(trades: pd.DataFrame, idx: pd.DatetimeIndex, h: int = HORIZON) -> float:
    if len(idx) <= h or trades.empty:
        return 0.0
    pos = idx.searchsorted(pd.to_datetime(trades["entry_date"]).to_numpy(), side="left")
    cnt = np.bincount(pos, minlength=len(idx) + 1)[: len(idx)]
    cs = np.concatenate([[0], np.cumsum(cnt)])
    win = cs[h:] - cs[:-h]          # trades entered in [t, t+h)
    return float(np.median(win))


def main(price_table: str = "yfinance_universe_prices",
         out: str = "paper/expected_ranges.json",
         universe_out: str = "paper/universe.txt",
         max_symbols: int | None = None):
    import query
    from analytics.technical import _load_ohlcv
    from evaluation.universe import clean_symbols
    from strategies import stage3

    t0 = time.time()
    print("paper package:", paper.__file__, flush=True)
    syms = clean_symbols(query.symbols(price_table), price_table=price_table)
    dev = sorted(s for s in syms if not stage3._is_holdout_symbol(s))
    if max_symbols is not None:
        dev = dev[:max_symbols]
    print(f"dev symbols: {len(dev)}", flush=True)

    frames = {}
    for i, s in enumerate(dev, 1):
        df = _load_ohlcv(s, price_table, None, END)
        if df is not None and len(df):
            frames[s] = df
        if i % 100 == 0:
            print(f"loaded {i}/{len(dev)} ({time.time() - t0:.0f}s)", flush=True)
    print(f"frames loaded: {len(frames)} ({time.time() - t0:.0f}s)", flush=True)

    accounts = {}
    for spec in ACCOUNTS:
        t1 = time.time()
        res = simulate_account(frames, STRATEGIES[spec.slug], start_equity=spec.start_equity,
                               slots=spec.slots, start=START, end=END)
        band = normal_band(res.equity, spec.start_equity, HORIZON)
        tr = res.trades
        accounts[spec.key] = {
            "band": {"h": [int(h) for h in band.index],
                     "p5": band.p5.tolist(), "p50": band.p50.tolist(),
                     "p95": band.p95.tolist()},
            "trades_per_63d_median": _trades_per_window(tr, res.equity.index),
            "cost_bps_mean": float(tr["cost_bps"].mean()) if len(tr) else 0.0,
            "short_share": float((tr["side"] == "short").mean()) if len(tr) else 0.0,
        }
        print(f"{spec.key}: {len(tr)} trades, {time.time() - t1:.0f}s "
              f"(total {time.time() - t0:.0f}s)", flush=True)

    sha = hashlib.sha256("\n".join(dev).encode()).hexdigest()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_PKG_ROOT,
                            capture_output=True, text=True).stdout.strip()
    method = {"window_start": START, "window_end": END, "horizon": HORIZON,
              "band": "p5-p95", "universe_sha256": sha, "code_commit": commit,
              "n_symbols": len(dev), "price_table": price_table,
              "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    Path(universe_out).write_text("\n".join(dev) + "\n", encoding="utf-8")
    Path(out).write_text(json.dumps({"method": method, "accounts": accounts}, indent=1),
                         encoding="utf-8")
    print(f"done in {time.time() - t0:.0f}s -> {out}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--price-table", default="yfinance_universe_prices")
    ap.add_argument("--out", default="paper/expected_ranges.json")
    ap.add_argument("--universe-out", default="paper/universe.txt")
    ap.add_argument("--max-symbols", type=int, default=None)
    a = ap.parse_args()
    main(a.price_table, a.out, a.universe_out, a.max_symbols)
