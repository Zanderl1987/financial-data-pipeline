# Unified Evaluation Framework — Design Spec

**Date:** 2026-07-18
**Status:** Approved (brainstorming session, Claude Fable 5)
**Session notes:** `work-notes/financial-data-pipeline/SESSION_NOTES_2026-07-18-eval-framework.md`

## Purpose

One framework that answers, for any signal, trading rule, or event set the repo can
produce: *does it predict forward returns, are the returns statistically significant,
and how does it compare to everything already tried?* Replaces the pattern of writing
a bespoke harness per signal (`sentiment_eval.py`, `tv_rating_eval.py`) with typed
contracts feeding a shared engine and a shared, research-grade significance battery.

Known measured baselines the framework must be able to reproduce (v1 acceptance):

- VADER sentiment: pooled IC ≈ 0.01, no significant horizon (2026-07-07).
- TV rating (`rating_all`/`rating_ma`): mildly contrarian, IC −0.005 to −0.012,
  |IC| < 0.02 at all horizons of 1/3/5/10/21d (2026-07-18).
- The 9 `signal_panel()` factors: never systematically evaluated — v1 produces
  their first recorded baselines.

## Scope

**In scope (v1):** three input types (continuous signals, trading rules, event sets);
three-tier significance battery through research-grade; append-only results registry;
two-stage compute→HTML report CLI pair; adapters for all existing signals; synthetic-
data test suite plus a real acceptance run.

**Out of scope (v1), recorded as TODOs:** transaction costs/slippage/borrow;
capital-constrained compounding equity curves; live/daily refresh wiring;
config-YAML declarative runner (Approach C layer — build after the interface
stabilizes); conditional/compound scenario testing (seed: `event_backtest.scenario()`);
the price-volume signal family (own brainstorm → spec → build cycle).

## Architecture (Approach A — chosen over alternatives)

New `evaluation/` package wrapping existing battle-tested primitives. Rejected:
(B) growing `tv_rating_eval.py` into a monolith — three input types plus a
research-grade battery in one script is untestable; (C) config-driven YAML runner
first — front-loads a config layer before the engine's shape is proven by use.

```
evaluation/
├── contracts.py    — Signal, TradeRule, EventSet input types + validation
├── data.py         — price/return panel builder; the ONE place PIT rules live
├── ic.py           — level-IC battery (generalizes tv_rating_eval.evaluate_signal)
├── portfolio.py    — quantile portfolio evaluation (wraps backtest.backtest)
├── events.py       — event-study evaluation (wraps event_backtest.event_study)
├── trades.py       — trade simulation (generalizes tv_rating_eval.simulate_trades)
├── stats.py        — significance battery, all three tiers
├── registry.py     — append-only parquet results store + baseline lookup
└── runner.py       — orchestrates: input → applicable evaluations → registry
evaluate.py                 (CLI, compute stage → storage/reports/eval/<run_id>/)
generate_eval_report.py     (report stage → self-contained Plotly HTML)
```

Existing scripts (`sentiment_eval.py`, `tv_rating_eval.py`, `backtest.py`,
`event_backtest.py`) are NOT modified in v1; the package imports from them.
Nothing is wired into `run_all.py`/`curated.py`/pipeline-catalog tests — this is
an analysis tool over already-curated data, not an ingestion pipeline.

Evaluation routing by input type:

| Input | Evaluations |
|---|---|
| `Signal` | IC battery + quantile portfolio (+ threshold trades if thresholds given) |
| `EventSet` | event study + CAR significance |
| `TradeRule` | trade simulation + permutation null |
| all | stats battery annotation + registry append |

## Input contracts (`contracts.py`)

Plain dataclasses wrapping DataFrames, strictly validated at construction —
everything downstream trusts them.

**`Signal`** — continuous score per (symbol, day).
- Frame: `symbol`, `date`, `value`; one row per (symbol, date) — the provider does
  any aggregation and must declare it in `source`.
- Metadata: `name` (registry key), `lag_days` (publication lag: days after `date`
  the value became knowable; 0 for price-derived signals; explicit and conservative
  for filed/published data), `direction` (+1 higher-is-better / −1 / 0 unknown),
  `source` (free-text provenance note). `direction` orients the top-vs-bottom
  bucket definition and the expected IC sign in reports; `direction=0` reports
  raw signs with no orientation applied.
- Validation: no duplicate (symbol, date); no NaN `value`; tz-naive dates;
  warn (not fail) below 250 distinct dates — too short for honest daily-IC t-stats.

**`EventSet`** — discrete point-in-time occurrences.
- Frame: `symbol`, `date`, `label`; optional `magnitude`.
- Metadata: `name`, `lag_days` (same PIT meaning), `min_events` (default 5,
  matching the TV transition study) — labels below it are reported as skipped.

**`TradeRule`** — a system producing discrete trades.
- `entries(df) → bool Series`, `exits(df) → bool Series` over an OHLCV+signal
  frame; `side` (long/short/both); `notional` (default $10,000/trade).
- Rules see data up to and including day *t*; the ENGINE executes at close of
  *t+1*. Entry timing is enforced by the engine, never trusted to the rule.

**PIT invariant:** `lag_days` lives on the contract and `data.py` applies it in
exactly one place when aligning inputs to returns. No evaluation module ever
shifts dates itself — look-ahead is structurally excluded, not individually hunted.

**v1 adapters** (so the acceptance test is real): `from_signal_panel()` (9 factors),
`from_rating_history()` (TV `rating_all`/`rating_ma`/`rating_osc`),
`from_sentiment()` (daily sentiment scores), `from_rating_changes()` (transition
EventSet), and the TV threshold-cross strategy re-expressed as a `TradeRule`.

## Data flow

```
contract → data.py:
             price panel via event_backtest.load_close (longest-series invariant),
             apply lag_days, align to next-close entry,
             forward returns at 1/3/5/10/21d, excess vs SPY
         → runner.py: dispatch to applicable evaluators (table above)
         → stats.py: annotate every result with the significance battery
         → registry.py: append rows
         → evaluate.py: artifacts to storage/reports/eval/<run_id>/
         → generate_eval_report.py: self-contained HTML (never recomputes)
```

- Universe: parameter; defaults to all `tiingo_prices` symbols (69 as of 2026-07-18,
  deepest history available). Prices read via the query layer (curated), never raw.
- `run_id`: timestamp-based. Every run writes `run_meta.json` (universe, date range,
  git commit, parameters, dropped-symbol counts) — every result reproducible from
  its metadata.
- Horizons fixed at 1/3/5/10/21 trading days (matches existing harnesses so
  baselines are comparable).

## Significance battery (`stats.py`) — three tiers, built in order

**Tier 1 — parametric** (today's methodology, unified):
pooled Spearman IC AND mean daily cross-sectional IC with t-stat; top-vs-bottom
bucket spread with t-test; event-study CAR t-stats; every statistic reported with
n, day count, and universe. Guard: zero cross-day variance → `None` + reason
string (the `sd>0` bug class hit twice in the TV build).

**Tier 2 — resampling:**
- Block bootstrap (block by date, preserving cross-sectional correlation) → CIs on
  bucket spreads and Sharpe.
- Permutation null for trade systems: shuffle entry dates within each symbol,
  re-simulate → empirical p-value on total P&L and win rate.
- Benjamini–Hochberg FDR correction across each run's full
  signals × horizons × statistics grid.

**Tier 3 — research-grade:**
- Walk-forward expanding-window IS/OOS splits; headline stats are OOS-only.
- Regime conditioning: bull/bear via SPY vs its 200-day SMA; high/low volatility
  via realized-vol median split; all stats reported per regime.
- Deflated Sharpe ratio and a White's-reality-check-style comparison against the
  registry's population of previously evaluated inputs — the registry supplies a
  REAL "number of things tried" denominator instead of a guess.

Each tier is independently verifiable on synthetic panels with known answers:
a planted signal must be detected; pure noise must NOT survive FDR correction.
That synthetic falsification test is part of the deliverable, not optional.

## Registry (`registry.py`)

Append-only parquet at `storage/eval_registry/results.parquet`, long format:
`run_id`, `input_name`, `input_type`, `evaluation`, `horizon`, `statistic`,
`value`, `n`, `universe_hash`, `date_range`, `created_at`.

- `baselines()` — latest result per (input_name, evaluation, horizon, statistic).
- `compare(a, b)` — enforces the identical-signal-set rule: refuses to compare
  runs over different symbol-day sets unless explicitly overridden (a coverage
  difference masquerades as a skill difference).
- Not wired into `curated.py`; gitignored data with a CLI summary export.

## Error handling

- Contracts fail loudly at construction; bad frames never enter the engine.
- Evaluators degrade gracefully per symbol: insufficient history → symbol dropped
  AND counted in `run_meta.json`; never silently omitted.
- Statistics whose assumptions fail return `None` plus a reason string.
- `evaluate.py` exits non-zero if zero evaluations completed.
- ASCII-only CLI output (Windows cp1252 — repo rule).

## Testing

`tests/test_evaluation.py`, following repo conventions:

- Unit tests per module on synthetic data with known answers:
  - planted-signal recovery (IC and spread both detect it);
  - noise rejection (no synthetic-noise stat survives BH correction);
  - permutation p-value calibration (a null rule's p ≈ uniform);
  - PIT lag application (a signal with `lag_days=N` loses exactly its first N
    days of alignment);
  - contract validation failures (duplicates, NaN, short history warning).
- One tiny end-to-end integration test (synthetic prices + signal → registry rows
  + artifacts).
- Acceptance run (manual, plan-mandated): all existing signals through the
  framework over the same universe/date ranges as the original runs; IC values
  must land within ±0.005 of the recorded baselines AND reach the same
  significance verdicts (small numeric drift from implementation differences is
  acceptable; a changed verdict is not). CLI run performed for real, not
  inferred from green tests (the missing `__main__` guard lesson, 2026-07-18).

## Report (`generate_eval_report.py`)

Self-contained Plotly HTML per run at `storage/reports/eval/<run_id>/report.html`,
dataviz-skill-compliant (categorical identity colors for signal comparison; status
colors reserved for win/loss, bull/bear; never mixed on one mark). Sections:
headline significance table (FDR-adjusted), IC-by-horizon, quantile spread charts,
event-study CAR curves, trade P&L with permutation-null overlay, regime breakdown,
baseline-comparison table from the registry. Report stage never recomputes.

## Success criteria

1. All three input types evaluate end-to-end through one CLI invocation each.
2. Synthetic falsification suite passes (planted detected, noise rejected).
3. Acceptance run reproduces the three recorded baselines within tolerance and
   writes the 9 factors' first baselines to the registry.
4. Full existing test suite still green; new tests included.
5. A new signal can be evaluated by writing ONLY an adapter (no engine changes) —
   demonstrated in the acceptance run by the number of bespoke lines each adapter
   needs (target: tens, not hundreds).
