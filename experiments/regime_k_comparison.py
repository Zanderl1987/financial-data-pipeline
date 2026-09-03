#!/usr/bin/env python3
"""
k=2 vs k=3 Statistical Jump Model regime comparison (evaluation/regime.py).

TASKS.md's ask, verbatim: "Try k=3 (e.g. calm/choppy/crisis) and compare
against k=2 ... rather than assuming more regimes is better just because
it's prettier." This script is that comparison, run on real SPY history.

WHY NOT LITERAL CPCV (robustness.cpcv_splits): CPCV's whole mechanism is
combinatorial, non-contiguous train/test groups with purge+embargo gaps
between them. fit_jump_model()'s entire reason to exist over a plain
k-means is the jump_penalty term, which charges a cost for the state
changing between CONSECUTIVE CALENDAR DAYS. Feed it a CPCV fold with
chunks removed and array-position-adjacency silently stops meaning
calendar-adjacency -- the jump penalty would be penalizing transitions
across gaps of days/weeks as if they were single-day flips, corrupting
the exact mechanic being fit. A combinatorial CV is the wrong tool for an
adjacency-dependent model; this uses a plain chronological walk-forward
split instead (first half fit, second half fit, compare), and says so
here rather than mislabeling it CPCV to check a box.

Usage:
  C:\\ProgramData\\anaconda3\\python.exe experiments\\regime_k_comparison.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import event_backtest as eb                        # noqa: E402
from evaluation import regime as ev_regime          # noqa: E402


def _fmt_stats(stats: dict) -> str:
    lines = []
    for j in sorted(stats):
        s = stats[j]
        lines.append(f"    regime {j}: n_days={s['n_days']:>5}  "
                     f"ann_return={s['ann_return_pct']:>7}%  "
                     f"ann_vol={s['ann_vol_pct']:>6}%")
    return "\n".join(lines)


def run_k(returns, k: int, seed: int = 0) -> dict:
    out = ev_regime.label_regimes(returns, k=k, seed=seed)
    return out


def main() -> int:
    close = eb.load_close("SPY", start="1993-01-01")
    if close is None or close.empty:
        print("X no SPY history available")
        return 1
    returns = close.pct_change().dropna()
    n = len(returns)
    print(f"SPY daily returns: {n} days, {returns.index.min():%Y-%m-%d} .. "
          f"{returns.index.max():%Y-%m-%d}")
    print("=" * 78)

    print("FULL-SAMPLE FIT (in-sample, per regime.py's own PIT caveat)")
    full = {}
    for k in (2, 3):
        out = run_k(returns, k)
        full[k] = out
        if out.get("regime_reason"):
            print(f"  k={k}: {out['regime_reason']}")
            continue
        print(f"  k={k}: converged={out['params']['converged']} "
              f"n_switches={out['n_switches']}")
        print(_fmt_stats(out["regime_stats"]))
    print("=" * 78)

    print("CHRONOLOGICAL WALK-FORWARD STABILITY (first half vs second half, "
          "see module docstring for why this replaces CPCV here)")
    half = n // 2
    first, second = returns.iloc[:half], returns.iloc[half:]
    for k in (2, 3):
        print(f"  k={k}:")
        for label, seg in (("first half", first), ("second half", second)):
            out = run_k(seg, k)
            if out.get("regime_reason"):
                print(f"    {label}: {out['regime_reason']}")
                continue
            spread = (out["regime_stats"][k - 1]["ann_return_pct"]
                     - out["regime_stats"][0]["ann_return_pct"])
            print(f"    {label}: n_switches={out['n_switches']} "
                  f"best-minus-worst regime spread={spread:.1f}pp/yr")
            print(_fmt_stats(out["regime_stats"]))
    print("=" * 78)
    print("VERDICT: read the printed comparison and update "
          "work-notes/financial-data-pipeline/TASKS.md by hand -- this "
          "script reports, it does not auto-decide.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
