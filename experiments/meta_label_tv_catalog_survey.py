#!/usr/bin/env python3
"""
Meta-labeling survey across real TV catalog strategies.

TASKS.md's meta-labeling follow-up: "Run it across more of the TV catalog's
actual registered strategies (only smoke-tested against one ad hoc
SMA-crossover rule so far) to see which ones it actually helps versus which
it doesn't move." This script runs evaluation/meta_label.py's walk-forward
filter against every hand-ported strategy (strategies/ports/ --
translation_verified="unit_tested", the highest-confidence translations) on
a modest 25-symbol liquid universe, and reports win-rate / avg P&L before
vs. after filtering at threshold=0.5, for every strategy with enough trades
for walk_forward_meta_labels' min_train=50 requirement.

Deliberately NOT the full ~2,100-symbol campaign universe (dev_cache() in
strategies/stage3.py) -- this is a quick research survey, not a Stage 3
run; a 25-symbol liquid basket is plenty to see whether meta-labeling
moves win rate on real trades, and keeps runtime to minutes instead of
hours (see CLAUDE.md's event_backtest/wide-universe scaling caution,
which applies to the same per-symbol query pattern here).

Usage:
  C:\\ProgramData\\anaconda3\\python.exe experiments\\meta_label_tv_catalog_survey.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.technical import _load_ohlcv               # noqa: E402
from evaluation import meta_label as ev_meta               # noqa: E402
from evaluation import trades as ev_trades                 # noqa: E402
from strategies import ports as strategy_ports             # noqa: E402

PRICE_TABLE = "yfinance_universe_prices"
SYMBOLS = ["AAPL", "MSFT", "SPY", "XOM", "JPM", "KO", "PG", "AMZN", "NVDA",
          "GOOGL", "META", "JNJ", "WMT", "HD", "DIS", "INTC", "CSCO", "PFE",
          "MRK", "T", "VZ", "BA", "GE", "IBM", "CAT"]
MIN_TRADES = 60          # comfortably above walk_forward_meta_labels' min_train=50
THRESHOLD = 0.5
MIN_KEPT_FOR_COMPARISON = 10   # below this, a win-rate delta is noise, not a finding


def build_cache() -> dict:
    cache = {}
    for sym in SYMBOLS:
        df = _load_ohlcv(sym, PRICE_TABLE, start=None, end=None)
        if df is not None and not df.empty:
            cache[sym] = df
    return cache


def survey_one(slug: str, cache: dict) -> dict:
    try:
        rule = strategy_ports.load_rule(slug)
    except Exception as exc:
        return {"slug": slug, "reason": f"load_rule failed: {type(exc).__name__}: {exc}"}
    try:
        trades = ev_trades.simulate(rule, cache)
    except Exception as exc:
        return {"slug": slug, "reason": f"simulate failed: {type(exc).__name__}: {exc}"}
    if len(trades) < MIN_TRADES:
        return {"slug": slug, "reason": f"only {len(trades)} trades (< {MIN_TRADES})"}

    feats = ev_meta.build_features(trades, cache)
    # refit_every=100 (vs. the library default 20): a survey over strategies
    # with thousands to tens of thousands of trades on this small universe
    # made the default's refit cadence prohibitively slow (a single strategy
    # ran 450+ CPU-seconds and was still going). Refitting less often is a
    # real, defensible choice for a directional survey -- not a precision
    # production run -- and doesn't change what's being measured, only how
    # often the logistic model updates.
    scored = ev_meta.walk_forward_meta_labels(trades, feats, refit_every=100)
    result = ev_meta.evaluate_meta_filter(scored, threshold=THRESHOLD)
    if "meta_reason" in result:
        return {"slug": slug, "reason": result["meta_reason"]}

    u, f = result["unfiltered"], result["filtered"]
    return {"slug": slug, "n_trades": len(trades), "n_scored": result["n_scored"],
           "n_kept": result["n_kept"], "kept_fraction": result["kept_fraction"],
           "win_rate_before": u.get("win_rate_pct"),
           "win_rate_after": f.get("win_rate_pct"),
           "avg_pnl_before": u.get("avg_pnl_pct"),
           "avg_pnl_after": f.get("avg_pnl_pct")}


def main() -> int:
    cache = build_cache()
    print(f"Universe: {len(cache)}/{len(SYMBOLS)} symbols loaded from {PRICE_TABLE}")
    infos = sorted(strategy_ports.all_ports(), key=lambda i: i.slug)
    print(f"{len(infos)} hand-ported strategies to survey")
    print("=" * 100)

    moved, skipped = [], []
    for info in infos:
        out = survey_one(info.slug, cache)
        if "reason" in out:
            skipped.append(out)
            print(f"  {out['slug']:<45} SKIP: {out['reason']}")
            continue
        if out["win_rate_after"] is None or out["n_kept"] < MIN_KEPT_FOR_COMPARISON:
            out["reason"] = (f"kept only {out['n_kept']} trades at threshold "
                             f"(< {MIN_KEPT_FOR_COMPARISON}, too few for a "
                             f"reliable win-rate comparison)")
            skipped.append(out)
            print(f"  {out['slug']:<45} SKIP: {out['reason']}")
            continue
        wr_delta = out["win_rate_after"] - out["win_rate_before"]
        pnl_delta = out["avg_pnl_after"] - out["avg_pnl_before"]
        moved.append({**out, "wr_delta": wr_delta, "pnl_delta": pnl_delta})
        print(f"  {out['slug']:<45} n={out['n_trades']:>5} scored={out['n_scored']:>4} "
              f"kept={out['n_kept']:>4} ({100*out['kept_fraction']:.0f}%) "
              f"win_rate {out['win_rate_before']:>5}->{out['win_rate_after']:>5} "
              f"(Delta{wr_delta:+.1f}) avg_pnl%25 {out['avg_pnl_before']:>6}->"
              f"{out['avg_pnl_after']:>6} (Delta{pnl_delta:+.2f})")

    print("=" * 100)
    print(f"Surveyed: {len(infos)}  Evaluated: {len(moved)}  Skipped: {len(skipped)}")
    if moved:
        helped = [m for m in moved if m["wr_delta"] > 0]
        hurt = [m for m in moved if m["wr_delta"] < 0]
        print(f"Win-rate improved: {len(helped)}/{len(moved)}  "
              f"Win-rate worsened: {len(hurt)}/{len(moved)}  "
              f"Unchanged: {len(moved) - len(helped) - len(hurt)}/{len(moved)}")
        best = max(moved, key=lambda m: m["wr_delta"])
        worst = min(moved, key=lambda m: m["wr_delta"])
        print(f"Best win-rate delta: {best['slug']} ({best['wr_delta']:+.1f}pp)")
        print(f"Worst win-rate delta: {worst['slug']} ({worst['wr_delta']:+.1f}pp)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
