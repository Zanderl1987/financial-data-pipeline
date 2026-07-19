# Session Notes — 2026-07-19 (unified eval framework: plan writing, resumed after restart)

**Branch:** master
**Session model:** Claude Fable 5
**Continues:** SESSION_NOTES_2026-07-18-eval-framework.md (brainstorming + design)

## State found at session start (machine restarted mid-session yesterday/today)

- Design spec COMMITTED: `docs/superpowers/specs/2026-07-18-unified-eval-framework-design.md`
  (commit dd23e12). Approved architecture = Approach A (`evaluation/` package).
- Plan file `docs/superpowers/plans/2026-07-19-unified-eval-framework.md` was
  **truncated by the restart**: header + Global Constraints + File Structure +
  **Task 1 only** (of 12). Ends at Task 1's commit step, 265 lines. Untracked
  (never committed).
- NO implementation exists yet — no `evaluation/` directory, no
  `tests/test_evaluation.py`. All Task-1 checkboxes unchecked.
- No stall flags (PULL_STALLED / QUALITY_FAIL / EARNINGS_PULL_FAILED absent from
  this repo root; the custom_index_tool fiscal-quarter flag from 07-16 is a
  separate repo and still open per yesterday's notes).
- Working tree carries a pile of uncommitted work from the SEPARATE constituents
  session (modified: curated.py, query.py, run_all.py, validate.py,
  tests/test_catalog.py, tests/test_pipelines.py, usgs_minerals_pipeline.py,
  SESSION_NOTES*.md; untracked: index_constituents_pipeline.py,
  securities_reference_pipeline.py, fund_holdings_pipeline.py, openfigi_pipeline.py,
  omkar_commodity_pipeline.py, audit_*.py, storage/iceberg/, docs/ARCHITECTURE.md,
  docs/PIPELINE_CATALOG.md, README.md, …). **Left strictly untouched by this
  session.** NOTE: full-suite pass/fail counts may reflect that in-flight work —
  judge eval-framework tasks by `tests/test_evaluation.py` plus no NEW failures.

## This session so far

- Loaded superpowers:writing-plans + signal-eval skills; re-read the spec.
- Gathered exact signatures of the primitives the plan wraps (so plan code is
  real, not guessed): `event_backtest.load_close/load_close_matrix/event_study`
  (EventStudyResult: car/mean_car/horizons/events/baseline), `backtest.backtest`
  (BacktestResult; weights `.shift(1)` = next-day execution already built in),
  `tv_rating_eval.build_signal_cache/build_return_panel/evaluate_signal/
  simulate_trades` (+ BULL_MIN/BEAR_MAX/EXIT_* constants), `sentiment_eval`'s
  confidence-weighted daily aggregation, `analytics.signals.DEFAULT_WEIGHTS`
  (8 factors + composite = the "9 factors").
- RESUMED WRITING THE PLAN: appending Tasks 2–12 to the truncated plan file.
  First append attempt (Tasks 2–3 via bash heredoc) failed on a shell quoting
  error — **file verified unchanged (still 265 lines)**; retrying with a
  different write method.

## Plan design decisions locked while writing (beyond the spec)

- Task order (already in plan header): 1 contracts → 2 data → 3 Tier-1 stats +
  ic → 4 portfolio + events → 5 trades → 6 Tier-2 → 7 Tier-3 → 8 registry →
  9 runner + evaluate CLI (generic parquet input only) → 10 adapters (adds the
  adapter CLI flags to evaluate.py) → 11 report → 12 acceptance run + docs.
- `apply_lag()` advances dates by lag_days BUSINESS days; entry is then the
  first trading close STRICTLY AFTER the (lagged) date (searchsorted
  side="right") in the symbol's own calendar. Runner applies the lag once;
  evaluators receive lag-applied frames (PIT stays in data.py + runner).
- `build_return_panel` returns `(panel, dropped_dict)` so run_meta.json can
  count dropped symbols per spec. Benchmark reindexed+ffilled onto each
  symbol's dates (tv_rating_eval semantics). Entry AND exit closes must be
  finite and > 0 (WTI-2020 degenerate-price guard).
- Generic bucket spread uses per-date cross-sectional top/bottom q=20%
  (arbitrary signal scales), NOT tv's absolute ±0.5 thresholds — acceptance
  comparison in Task 12 therefore keys on IC values/verdicts, not spread.
- `direction=-1` evaluates `-value` so good contrarian signals report positive
  oriented IC; `direction=0` = raw signs (spec).
- trades.py exposes low-level `simulate_symbol(...)` on flag arrays so Tier-2
  permutation can re-simulate permuted entries without hacking TradeRule
  callables.
- Task 11 (Plotly report): load the dataviz skill BEFORE writing that task's
  code (categorical identity colors for signals; status colors only for
  win/loss / bull/bear).

## PLAN COMPLETE (this session, after machine-restart resume + compactions)

All 12 tasks written to `docs/superpowers/plans/2026-07-19-unified-eval-framework.md`
(~3800 lines, full TDD code in every step; appended via scratchpad chunks +
`[System.IO.File]::AppendAllText` — bash heredocs kept mangling quoting).

Self-review done (spec coverage / placeholder scan / type consistency).
Fixes made during review:
- Nested-fence bug in Task 12's EVALUATION.md block (outer fence → 4 backticks).
- `evaluate.py` / `generate_eval_report.py` docstrings made raw (contain `C:\P...`).
- Spec gaps closed: `--adapter tv-rule` CLI path (success criterion 1 = all
  three input types via one CLI invocation each); report default output moved
  to `<run_dir>/report.html` (spec location); CAR-curves figure + registry
  baseline-comparison table added to the report; `registry.summary()` +
  `python -m evaluation.registry` (spec's CLI summary export).
- Documented v1 simplification: trade P&L chart shows the permutation p in a
  tile, not a null-distribution overlay (Tier 2 returns p-values, not draws).

## Next steps (exact resume point)

1. Execution choice offered to Zander: subagent-driven (recommended) vs
   inline (executing-plans). Waiting on the answer.
2. Then execute Tasks 1→12; Task 12 = acceptance run reproducing recorded
   baselines (sentiment pooled IC ≈ 0.01; TV rating IC −0.005..−0.012;
   9 factors' first baselines) within ±0.005 IC and same verdicts.

## Unchanged running TODOs (from 07-18 notes)

- Price-volume signal family (own cycle) • conditional/compound scenario
  testing • config-YAML runner layer • TV-rating experiment writeup •
  custom_index_tool fiscal-quarter derivation fix (EARNINGS_PULL_FAILED.txt).
