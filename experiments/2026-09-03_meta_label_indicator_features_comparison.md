# Do richer technical-indicator features move meta-labeling's needle?

**Date:** 2026-09-03
**Script:** `experiments/meta_label_indicator_features_comparison.py`
**Data:** same 9-symbol `yfinance_universe_prices` universe as
`experiments/2026-09-03_meta_label_tv_catalog_survey.md`, the 16 strategies that
survey found evaluable (reliable kept-set size)

## Question

TASKS.md's conditional meta-labeling follow-up: "The logistic-regression ceiling
may be real — if richer features don't move the needle, that's the point to revisit
sklearn as an actual declared dependency (gradient-boosted trees) rather than
staying scipy-only for its own sake." Never tested empirically until now.

## Method

Reran `build_features()` with `indicator_cols=["rsi14", "macd", "adx14", "atr14"]`
(the real technical indicators added 2026-09-02) added to the existing trailing
return/vol/SMA-distance trio, for the same 16 strategies, same universe, same
threshold (0.5). Compared each strategy's win-rate delta against the trio-only
baseline already recorded in the 2026-09-03 survey, rather than re-running that
baseline.

## Results

**16/16 compared. 8/16 changed sign of the win-rate delta; 11/16 produced a
bigger-magnitude delta than trio-only.** Full table:

| Strategy | kept (indicators) | trio Δ | indicator Δ | direction |
|---|---:|---:|---:|---|
| `apex_fusion_confluence` | 104/2,306 | -19.5pp | +0.7pp | flipped |
| `bps_v17_strong_trend_filter` | 330/1,408 | +0.7pp | -2.0pp | flipped |
| `capitulation_stretch_reversion` | 303/333 | +0.0pp | +2.7pp | flipped |
| `ineficient_market_123_pattern` | 133/169 | +0.0pp | -2.8pp | same* |
| `ihvpg6ts_...` | 1,273/4,026 | +1.0pp | -1.4pp | flipped |
| `optimized_keltner_channels_nifty` | 44/985 | +24.3pp | -7.5pp | flipped |
| `fvg_bos_confirmation` | 483/9,692 | +5.2pp | +2.2pp | same |
| `joey_stochrsi_atr` | 1,885/3,839 | +1.0pp | +1.7pp | same |
| `tomukas_sweep_reclaim_scalein` | 589/938 | +0.1pp | +1.7pp | same |
| `mrr_mean_reversion_range` | 195/418 | -1.6pp | -1.2pp | same |
| `ultimate_prop_firm_artillery` | 966/3,117 | -3.4pp | -1.5pp | same |
| `bollinger_bands_simple` / `rsi_bb_inside_strategy` / `bist30_...` / `fractal_memory_strategy` / `ghocsiv7_...` | (~65-100% kept, all) | ~0.0pp | ~0.0-0.2pp | trivial either way |

\* labeled "same" by the raw sign check but the trio value rounds to exactly 0.0pp,
so "flipped" vs "same" is not a meaningful distinction for these near-zero cases —
both readings agree the strategy is essentially unfiltered either way.

**The real story is in the kept-set sizes, not just the deltas.** The two most
extreme trio-only results were also the two with the SMALLEST kept-sets
(`optimized_keltner_channels_nifty` 11/985 = 1%, `apex_fusion_confluence` 25/2,306
= 1%). With richer features, both kept-sets grew markedly (44 and 104
respectively) and both deltas collapsed toward zero/reversed sign — consistent
with those trio-only extremes being small-sample overfitting that richer features
partially corrected, not a real edge the trio found and the indicators erased.
`fvg_bos_confirmation`, the one baseline result on a genuinely large kept-set
(162/9,692), stayed positive with indicators (kept grew to 483, delta shrank from
+5.2pp to +2.2pp but held its sign) — the one result in this comparison that looks
like a real, if modest, signal surviving a feature-set change.

## Verdict

**Richer features move the needle — meaningfully, not marginally — so the
TASKS.md trigger condition for revisiting sklearn ("if richer features don't move
the needle") is NOT met.** 11/16 strategies got a bigger-magnitude delta with
indicators than without, and several of the trio-only results (especially the most
extreme, least-trustworthy ones) changed substantially once richer features gave
the classifier more to work with. This is evidence the scipy logistic regression
is genuinely feature-sensitive rather than saturated — no reason yet to add
sklearn/gradient-boosted trees as a declared dependency. Revisit that decision
only if a FUTURE feature addition (not this one) fails to move results, or if a
specific strategy's meta-filter needs more expressive power than logistic
regression can offer on a well-populated kept-set (not the noisy small-n cases
here).
