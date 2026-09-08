"""
strategies/portfolio.py -- Phase 8 (post-campaign portfolio combination) for
the TV strategy catalog campaign.

Stage 5 produced three holdout survivors (all holdout_pnl_p=0.0099 < 0.05):
    bollinger_bands_simple, optimized_doji_breakout_short, rsi_bb_inside_strategy
and Stage 6 labeled them "promising" in the catalog. Phase 8 of the
backtest-rigor audit says: combine those survivors into ONE correlation-aware
portfolio via evaluation/hrp.py + the multi-symbol single-pass trade engine
(evaluation/trades.py's _simulate_single_pass) rather than trading each in
isolation, and paper-trade that combined portfolio forward before any real
capital follows it (see work-notes TODO.md item 6, Phase 8).

WHY THE ENGINE SHAPE IS RIGHT FOR THIS: the single-pass engine
(_simulate_single_pass) already admits candidates across MANY symbols in
interleaved chronological order with a shared capital budget, and its
Sizing.mode="hrp" sizes each admission from a fresh HRP call over the
set of concurrently-open symbols (trades._hrp_size -> hrp.hrp_weights).
What it does NOT do is run N independent rules at once -- it runs ONE rule
over many symbols. So the phase-8 build here is a COMBINED RULE whose
entry/exit signals are the per-side union of the three survivors' rules
(built by combine_survivors, below), run through the engine once, on the
same holdout cache and cost model Stage 5 used (so the portfolio faces
identical data + costs), but with the portfolio machinery (capital budget +
HRP sizing) toggled on. Trading each survivor separately with the same
capital would simply run three books; this runs one correlation-aware book.

One important semantic difference the union introduces, stated plainly:
with a union rule, a symbol is "in play" if ANY survivor's signal fires --
positions are never stepwise-duplicated per algorithm. That is the intent
of a combined book, not an accident of the merge. Each survivor no longer
gets its own independent notional; instead one shared capital pool is
allocated HRP-wise across whatever is concurrently held.

MAIN FUNCTIONS
    survivor_slugs([catalog]) -> [str]     Stage 5 survivors from the catalog
    combine_survivors(slugs, [pfloor], [cost]) -> TradeRule
                                           union rule: per-side OR of entries+exits
    portfolio_config(pfloor)               ExecutionConfig: campaign costs +
                                           PortfolioLimits + Sizing(mode='hrp')
    run_combined(cache, [slugs], [config], [notional], [capital])
        -> (trades_df, summary_dict)
    run_paper_trade([--confirm])           CLI: run on holdout cache, write
                                           trades to storage/eval_artifacts/...

Everything uses the exact cost model Stage 3/5 use (cost_config), so the
portfolio results are on the same cost basis as the individual survivors.
The run is paper -- nothing here places a real trade, and results are
written as an eval artifact, not into the campaign catalog's stage ladder.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation import execution as ev_execution
from evaluation import trades as ev_trades
from evaluation.contracts import TradeRule
from strategies.catalog import build_catalog_rows
from strategies.stage3 import (
    PRIMARY_COST_BPS,
    PRICE_FLOOR,
    cost_config,
    load_rule_for,
    with_price_floor,
)
from strategies.stage5 import HOLDOUT_START, holdout_cache

ARTIFACT_DIR = os.path.join("storage", "eval_artifacts", "tv_survivor_portfolio")


def survivor_slugs() -> "list[str]":
    """Stage 5 survivors -- catalog rows where the holdout actually ran.
    Uses the same field Stage 5 writes (`stage == "stage5"`), which Stage 6's
    verdict column already derives from; filtering on stage directly keeps
    this independent of verdict wording."""
    catalog = build_catalog_rows()
    if catalog.empty or "stage" not in catalog.columns:
        return []
    return sorted(catalog[catalog["stage"] == "stage5"]["strategy_id"].tolist())


def _union_entry_exit(slugs: "list[str]", pfloor: float) -> TradeRule:
    """Build the per-side union TradeRule from the given survivor slugs.

    The three survivors are long/short mixed:
        bollinger_bands_simple        side="both"
        optimized_doji_breakout_short side="short"
        rsi_bb_inside_strategy        side="both"
    so the union must OR the LONG signals from the both/long rules and OR
    the SHORT signals from the short/both rules independently.

    Returns a TradeRule with side="both" (a position can only ever be opened
    by the side that actually fired, satisfying TradeRule's contract that
    side="both" defines long AND short rules).
    """
    rules = [with_price_floor(load_rule_for(s)[0], floor=pfloor) for s in slugs]

    def _combine(getter):
        """getter(rule) -> callable; returns a callable that ORs the signals."""
        fns = [getter(r) for r in rules if getter(r) is not None]
        if not fns:
            return lambda df: pd.Series(False, index=df.index)

        def _or_signals(df):
            acc = None
            for fn in fns:
                s = pd.Series(fn(df), index=df.index).astype(bool)
                acc = s if acc is None else (acc | s)
            return acc

        return _or_signals

    long_entries = _combine(lambda r: r.entries if r.side in ("long", "both") else None)
    long_exits = _combine(lambda r: r.exits if r.side in ("long", "both") else None)
    short_entries = _combine(lambda r: r.short_entries if r.side == "both"
                             else (r.entries if r.side == "short" else None))
    short_exits = _combine(lambda r: r.short_exits if r.side == "both"
                           else (r.exits if r.side == "short" else None))

    name = "+".join(slugs)
    return TradeRule(name=name, side="both",
                     entries=long_entries, exits=long_exits,
                     short_entries=short_entries,
                     short_exits=short_exits)


def combine_survivors(slugs: "list[str]", pfloor: float = PRICE_FLOOR):
    """Union TradeRule over the survivor slugs. Public wrapper for tests."""
    return _union_entry_exit(slugs, pfloor)


def portfolio_config(cost_bps: float = PRIMARY_COST_BPS) -> ev_execution.ExecutionConfig:
    """Campaign costs + PortfolioLimits + Sizing(mode='hrp').

    Uses the same per-side costs as Stage 3/5 (cost_config), so results sit
    on the identical cost basis. Adds the portfolio machinery Stage 3/5 do
    NOT use (they run each rule unconstrained): a capital budget, an HRP
    sizing mode, and a concurrency cap.

    Fraction choice is load-bearing and was corrected from the initial
    1.0 after the first full run exposed what a 1.0 book means in
    trades._hrp_size: with zero other open positions the candidate takes
    the WHOLE `fraction * equity` budget, so a fraction of 1.0 produced a
    serial one-name-at-a-time book (the capital gate could never admit a
    second name) -- the opposite of Phase 8's correlation-aware multi-name
    intent. Setting fraction=0.4 caps a single-name book at 40% of capital
    and lets HRP spread the remaining budget across the decorrelated names
    of the concurrently-open cohort. max_concurrent=8 bounds the HRP
    cohort (covariance stays cheap across the ~60k candidate admissions in
    a full-universe run) without capping capacity above what the capital
    gate already enforces naturally at this fraction."""
    from evaluation.execution import PortfolioLimits, Sizing

    base = cost_config(cost_bps)
    return dataclasses.replace(
        base,
        sizing=Sizing(mode="hrp", fraction=0.4, hrp_lookback=126),
        limits=PortfolioLimits(capital=1_000_000.0,  # paper book, documented size
                               max_concurrent=8),
    )


def run_combined(cache: dict, slugs: "list[str] | None" = None,
                 config=None, notional: "float | None" = None) -> tuple:
    """Simulate the combined survivor rule over `cache` (holdout split, by
    default the same one Stage 5 used) under the portfolio config. Returns
    (trades_df, summary_dict)."""
    if slugs is None:
        slugs = survivor_slugs()
    rule = combine_survivors(slugs)
    cfg = config if config is not None else portfolio_config()
    trades = ev_trades.simulate(rule, cache, notional=notional, config=cfg)
    summary = ev_trades.trade_summary(trades)
    summary["strategies"] = slugs
    return trades, summary


def run_paper_trade(confirm: bool = False, write: bool = False) -> tuple:
    """Run the combined portfolio on the Stage 5 holdout cache (paper only).
    With confirm=True the simulation actually runs; write=True persists
    trades + summary to storage/eval_artifacts/tv_survivor_portfolio/."""
    slugs = survivor_slugs()
    if not slugs:
        print("No Stage 5 survivors -- nothing to combine yet.")
        return pd.DataFrame(), {}

    if not confirm:
        print(f"{len(slugs)} survivor(s): {slugs}")
        print("Pass confirm=True to simulate the combined portfolio "
              "(paper only, nothing real is traded).")
        return pd.DataFrame(), {}

    cache = holdout_cache()
    trades, summary = run_combined(cache, slugs)
    print("\nCOMBINED SURVIVOR PORTFOLIO (paper, HRP-sized, shared capital)")
    print(f"  strategies: {', '.join(slugs)}")
    summary_keys = ["n_trades", "n_long", "n_short", "total_pnl_dollars",
                    "win_rate_pct", "avg_pnl_pct", "median_days_held",
                    "n_symbols"]
    for k in summary_keys:
        print(f"  {k}: {summary.get(k)}")

    if write:
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        trades.to_parquet(os.path.join(ARTIFACT_DIR, f"trades_{ts}.parquet"),
                          index=False)
        import json
        meta_path = os.path.join(ARTIFACT_DIR, f"summary_{ts}.json")
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump({"run_ts": ts, "strategies": slugs,
                       "holdout_start": HOLDOUT_START, **{k: summary.get(k)
                        for k in summary_keys}}, fh, indent=2)
        print(f"  wrote trades -> {ARTIFACT_DIR}/trades_{ts}.parquet")
        print(f"  wrote summary -> {meta_path}")

    return trades, summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--confirm-run", action="store_true",
                    help="actually run the paper-trade simulation")
    ap.add_argument("--write", action="store_true",
                    help="persist trades + summary as eval artifacts")
    args = ap.parse_args()
    run_paper_trade(confirm=args.confirm_run, write=args.write)