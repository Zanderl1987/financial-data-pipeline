#!/usr/bin/env python3
"""
Does adding real technical-indicator features move meta-labeling's needle?

TASKS.md's conditional follow-up: "The logistic-regression ceiling may be
real -- if richer features don't move the needle, that's the point to
revisit sklearn ... Not yet tested empirically." This reruns
meta_label_tv_catalog_survey.py's same 33 hand-ported strategies on the
same 9-symbol universe, this time with indicator_cols=["rsi14", "macd",
"adx14", "atr14"] added to build_features() (see evaluation/meta_label.py's
2026-09-02 richer-feature-set commit), and compares each strategy's
win-rate lift against the trio-only baseline already captured in
experiments/2026-09-03_meta_label_tv_catalog_survey.md -- rather than
re-running that baseline (expensive, and its numbers are already real and
recorded).

Usage:
  C:\\ProgramData\\anaconda3\\python.exe experiments\\meta_label_indicator_features_comparison.py
"""

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_THIS_DIR))
sys.path.insert(0, _THIS_DIR)

from meta_label_tv_catalog_survey import (                 # noqa: E402
    build_cache, survey_one, MIN_KEPT_FOR_COMPARISON)

INDICATOR_COLS = ["rsi14", "macd", "adx14", "atr14"]

# win_rate_before/after from the 2026-09-03 trio-only baseline survey
# (experiments/2026-09-03_meta_label_tv_catalog_survey.md), for every
# strategy that was evaluable there (n_kept >= MIN_KEPT_FOR_COMPARISON).
BASELINE_WIN_RATE_DELTA = {
    "apex_fusion_confluence": -19.5,
    "bist30_sp500_atr_momentum_rider": 0.0,
    "bollinger_bands_simple": 0.1,
    "bps_v17_strong_trend_filter": 0.7,
    "capitulation_stretch_reversion": 0.0,
    "fractal_memory_strategy": 0.0,
    "fvg_bos_confirmation": 5.2,
    "ghocsiv7_gap_filling_strategy": 0.0,
    "ihvpg6ts_stop_loss_and_take_profit_in_example": 1.0,
    "ineficient_market_123_pattern": 0.0,
    "joey_stochrsi_atr": 1.0,
    "mrr_mean_reversion_range": -1.6,
    "optimized_keltner_channels_nifty": 24.3,
    "rsi_bb_inside_strategy": 0.1,
    "tomukas_sweep_reclaim_scalein": 0.1,
    "ultimate_prop_firm_artillery": -3.4,
}


def main() -> int:
    cache = build_cache()
    print(f"Universe: {len(cache)} symbols")
    print(f"Indicator features: {INDICATOR_COLS}")
    print("=" * 100)

    moved = 0
    same_direction = 0
    larger_magnitude = 0
    for slug in sorted(BASELINE_WIN_RATE_DELTA):
        out = survey_one(slug, cache, indicator_cols=INDICATOR_COLS)
        base_delta = BASELINE_WIN_RATE_DELTA[slug]
        if "reason" in out:
            print(f"  {slug:<45} SKIP: {out['reason']}  (baseline delta was {base_delta:+.1f}pp)")
            continue
        if out["win_rate_after"] is None or out["n_kept"] < MIN_KEPT_FOR_COMPARISON:
            print(f"  {slug:<45} SKIP: kept only {out['n_kept']} with indicators "
                  f"(baseline delta was {base_delta:+.1f}pp)")
            continue
        ind_delta = out["win_rate_after"] - out["win_rate_before"]
        moved += 1
        same_dir = (ind_delta > 0) == (base_delta > 0) or (ind_delta == 0 == base_delta)
        same_direction += int(same_dir)
        larger = abs(ind_delta) > abs(base_delta)
        larger_magnitude += int(larger)
        print(f"  {slug:<45} kept={out['n_kept']:>4}/{out['n_scored']:<4} "
              f"trio_delta={base_delta:>+6.1f}pp  indicator_delta={ind_delta:>+6.1f}pp  "
              f"{'same dir' if same_dir else 'FLIPPED'}  "
              f"{'bigger' if larger else 'smaller/equal'}")

    print("=" * 100)
    print(f"Compared: {moved}/{len(BASELINE_WIN_RATE_DELTA)}  "
          f"Same direction as trio-only: {same_direction}/{moved}  "
          f"Bigger-magnitude lift with indicators: {larger_magnitude}/{moved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
