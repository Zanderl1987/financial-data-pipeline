# Meta-labeling survey across real TV catalog strategies

**Date:** 2026-09-03
**Script:** `experiments/meta_label_tv_catalog_survey.py`
**Data:** `yfinance_universe_prices`, 9-symbol universe (META, JNJ, HD, INTC, CSCO,
PFE, BA, GE, CAT — the intersection of a 25-symbol liquid-name wishlist with what
this table actually carries), all 33 hand-ported (`translation_verified=
"unit_tested"`) TV catalog strategies

## Question

TASKS.md's meta-labeling follow-up: "Run it across more of the TV catalog's actual
registered strategies (only smoke-tested against one ad hoc SMA-crossover rule so
far) to see which ones it actually helps versus which it doesn't move."

## Method

For each hand-ported strategy: simulate real trades on the 9-symbol universe (no
costs, LEGACY engine defaults), build meta-label features
(`meta_label.build_features`), score walk-forward out-of-sample
(`walk_forward_meta_labels`, `refit_every=100` — coarser than the library default of
20, needed to keep 33 strategies' worth of logistic refits tractable on this
universe; a directional survey, not a precision production run), and compare
`trade_summary()` on the full vs. threshold-0.5-filtered set. Strategies with fewer
than 60 total trades, or where the filter kept fewer than 10 trades, are reported as
skipped rather than compared — a win-rate delta on 6 trades is noise, not a finding.

## Results

**33 surveyed → 16 evaluated, 17 skipped** (13 kept too few trades post-filter to
trust a comparison, 3 had under 60 trades total, 1 had zero trades on this universe).

Of the 16 evaluated:

| Outcome | Count |
|---|---|
| Win rate improved | 8 |
| Win rate worsened | 3 |
| Unchanged (kept ~100%) | 5 |

**The "unchanged" group is itself a finding, not a null result to discard**: 5
strategies (`bist30_sp500_atr_momentum_rider`, `capitulation_stretch_reversion`,
`fractal_memory_strategy`, `ineficient_market_123_pattern`, `rsi_bb_inside_strategy`)
had the meta-filter keep 100% (or effectively 100%) of scored trades at threshold
0.5 — the classifier never got confident enough in either direction to reject
anything. That is a legitimate "this signal's trailing-return/vol/SMA features carry
no information about which of THIS strategy's trades will win" result, not a bug.

**The extremes are a caution, not a win**: the best delta
(`optimized_keltner_channels_nifty`, +24.3pp) kept only 11/988 scored trades (1%);
the worst (`apex_fusion_confluence`, -19.5pp) kept only 25/2306 (1%). Both sit right
at this survey's 10-trade reliability floor — a 1%-kept filter on a few thousand
trades is closer to cherry-picking than a robust edge, in either direction. Read
past them to the moderate, reasonably-sized results:

| Strategy | n kept / scored | win rate | Δ win rate | Δ avg pnl% |
|---|---:|---:|---:|---:|
| `fvg_bos_confirmation` | 162/9,692 (2%) | 46.0→51.2 | **+5.2pp** | +0.52 |
| `joey_stochrsi_atr` | 2,757/3,840 (72%) | 52.4→53.4 | +1.0pp | +0.04 |
| `ihvpg6ts_stop_loss_and_take_profit_in_example` | 469/4,026 (12%) | 47.8→48.8 | +1.0pp | +0.16 |
| `ultimate_prop_firm_artillery` | 547/3,119 (18%) | 49.3→45.9 | -3.4pp | -0.39 |
| `mrr_mean_reversion_range` | 214/418 (51%) | 47.4→45.8 | -1.6pp | -0.08 |

These five have kept-sets large enough (162-2,757 trades) to be a real signal rather
than noise, and they still split roughly evenly between helped and hurt.

## Verdict

**Meta-labeling's lift is real but strategy-specific, confirming the TASKS.md
decision to keep it opt-in rather than a standard part of every evaluation.** There
is no blanket "meta-labeling helps" or "meta-labeling doesn't help" — on this
9-symbol universe it meaningfully helped 1 of 33 strategies
(`fvg_bos_confirmation`, at a trustworthy sample size), mildly helped 2 more, mildly
hurt 2, did nothing measurable for at least 5 (not because it failed, but because
those strategies' outcomes aren't predictable from this feature set), and produced
extreme-looking deltas for 2 more that are not reliable at this sample size. A
strategy-by-strategy decision — try it, look at `n_kept`, not just the win-rate
delta — is the right process, not a repo-wide default.

**Follow-up for a real production check**: this ran on a 9-symbol universe because
that's the actual intersection of a liquid-name wishlist with `yfinance_universe_
prices`'s real coverage (2,285 symbols total, but not the specific mega-caps
originally picked). A wider universe (or `strategies/stage3.py`'s own `dev_cache()`)
would give every strategy more trades and push more of the 17 skipped ones into
"evaluable" — worth doing before drawing a final conclusion on any single strategy,
though the cross-strategy pattern above (inconsistent, sample-size-dependent lift)
is unlikely to reverse.
