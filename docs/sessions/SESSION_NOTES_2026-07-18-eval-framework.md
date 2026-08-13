# Session Notes — 2026-07-18 (unified evaluation framework brainstorming)

**Branch:** master
**Session model:** Claude Fable 5

## Context (catch-up at session start)

- TV-rating backtest branch (`tv-rating-backtest`) confirmed MERGED to master; worktree
  cleaned up. First-pass verdict on record: rating is mildly contrarian, |IC| < 0.02
  noise floor at every horizon, large t-stats are sample-size artifacts; trade sim
  +$378k net is entirely long-side over a bull sample with zero costs — not a
  tradeable edge as-is. Formal experiment-writeup still pending (TODO).
- Flag surfaced: `custom_index_tool/EARNINGS_PULL_FAILED.txt` (2026-07-16) — fiscal
  quarter derivation hard-stopped on live verification (NVDA 2026-01-31 and
  2026-04-30 both derived 2026Q4). Labeled study blocked until derivation is fixed.
  Not addressed this session; still open.

## Goal of this session

Brainstorm (superpowers:brainstorming, with signal-eval skill loaded) a unified,
highly robust backtesting/evaluation framework: accepts multiple signal types,
runs a rigorous statistical-significance battery, leverages the pipeline's curated
data, and accumulates baselines across runs. Zander's larger vision: evaluate TV
technical, text sentiment, volatility, and eventually a new price-volume signal
family through one system.

## Decisions so far (brainstorming Q&A)

1. **Decomposition**: framework core FIRST. Price-volume relationship research is
   its own future project (in-depth on its own) — recorded as TODO, not in v1.
2. **Input types (all three in v1)**: continuous signals (symbol/day scores),
   trading rules/systems (entry/exit → discrete trades), discrete events.
   TODO: conditional/compound scenario testing (e.g. "sentiment bad AND earnings
   miss by X", "rates rise X") — seed exists in `event_backtest.scenario()`.
3. **Stats depth: full research-grade.** Tier 1 parametric (daily cross-sectional
   IC t-stats, bucket-spread t-tests, excess vs SPY) → Tier 2 resampling
   (bootstrap CIs on spreads/Sharpe, permutation nulls for trade systems,
   Benjamini-Hochberg across the signals×horizons grid) → Tier 3 research-grade
   (walk-forward IS/OOS splits, regime conditioning, deflated Sharpe /
   White's reality check). Spec will phase the tiers so each is verifiable alone.
4. **Output: two-stage compute→HTML report PLUS persistent results registry**
   (append-only parquet) so every measured baseline (sentiment IC≈0.01, TV rating
   mildly-contrarian, …) is queryable and new signals auto-compare against
   predecessors.
5. **Architecture: Approach A** — new `evaluation/` package with typed contracts
   (Signal / TradeRule / EventSet), thin modules wrapping the battle-tested
   primitives (`event_backtest.load_close`/`event_study`, `backtest.backtest`,
   generalized `tv_rating_eval.evaluate_signal`/`simulate_trades`), single
   `stats.py` for the whole significance battery, `registry.py`, `runner.py`,
   CLI pair `evaluate.py` + `generate_eval_report.py`. Existing scripts untouched
   in v1; nothing wired into run_all.py/curated.py. Chosen over (B) growing
   tv_rating_eval.py into a monolith and (C) config-driven YAML runner —
   C's config layer is a post-v1 TODO once the interface stabilizes.
   Rationale: one tested implementation of every statistic, small independently
   testable units, reuse of PIT-safe primitives that already survived review.
6. **Defaults**: universe defaults to the 69-symbol tiingo_prices set (parameterized);
   v1 acceptance = re-run existing signals (9 factors + TV rating + sentiment)
   through the framework and reproduce known baselines.

## Running TODO list (accumulated this session)

- Price-volume relationship signal family (own brainstorm → spec → build cycle).
- Conditional/compound scenario testing on top of `event_backtest.scenario()`.
- Config-driven declarative runner (Approach C layer) after interface stabilizes.
- TV-rating experiment writeup (experiment-writeup skill).
- Fix fiscal-quarter derivation in custom_index_tool (EARNINGS_PULL_FAILED.txt).

## State

Architecture section approved ("proceed with A"). Design presentation in progress —
remaining sections: contracts detail, data flow, stats battery detail, registry
schema, error handling, testing. Then: spec to
`docs/superpowers/specs/2026-07-18-unified-eval-framework-design.md`, self-review,
user review gate, writing-plans.
