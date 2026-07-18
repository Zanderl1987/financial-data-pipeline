# Session Notes — 2026-07-18

**Branch:** tv-rating-backtest (worktree at `.worktrees/tv-rating-backtest`)
**Session model:** Claude Sonnet 5

## What happened

Executed the full 9-task implementation plan from `docs/superpowers/plans/2026-07-17-tv-rating-backtest-dashboard.md`
via `superpowers:subagent-driven-development` — a fresh implementer subagent per task, a
task-scoped reviewer after each, fix-and-re-review loops on findings, plus a progress
ledger at `.superpowers/sdd/progress.md`. Worked in an isolated git worktree (created
this session) since master had unrelated pre-existing uncommitted work; the worktree's
`storage/` is symlinked to the main checkout's (499MB, gitignored) so both share the
same real curated data without duplication.

**Tasks 1-5 (`tv_rating_eval.py`, the compute stage):** signal cache + forward-return
panel, level-IC evaluation (generalizes `sentiment_eval.py`'s method to 3 signals),
transition event study (reuses `event_backtest.rating_changes()`/`event_study()`
directly), threshold-cross trade simulation (the one genuinely new backtest primitive),
CLI wiring writing 4 output artifacts. All reviewed clean; one round-trip on Task 2
(see below).

**Tasks 6-8 (`generate_tv_rating_report.py`, the report stage):** data-loading/
significance-classification helpers, 6 Plotly chart builders (dataviz-skill-compliant
color roles — categorical identity for signal comparison, status colors reserved for
win/loss and bull/bear state, never mixed on one mark), and final HTML assembly with
correct `include_plotlyjs` sequencing for a self-contained file. Two round-trips (Task
6, Task 7 — both below).

**Task 9 (verification):** ran the full suite (349 passed), ran `tv_rating_eval.py`
against the real 69-symbol/full-history universe, ran `generate_tv_rating_report.py`,
had the user open the resulting HTML in a real browser (Chrome extension wasn't
connected for me to drive it myself this session), walked the PIT checklist.

## Bugs caught during implementation (all fixed at the plan level, not papered over)

Three of these were caught by implementer subagents correctly refusing to force a test
to pass, and one was caught only by Task 9's actual end-to-end run — each is exactly
the kind of thing unit tests alone don't catch:

1. **Task 2**: `test_recovers_positive_signal` asserted `ic_se > 0`, but a zero-noise
   synthetic panel makes every day's Spearman rho exactly 1.0 (rho is scale-invariant),
   so cross-day variance is genuinely zero and `ic_se`/`ic_t_stat` are correctly `None`
   — same guard `sentiment_eval.evaluate()` already uses. Fixed the test assertion,
   added a second test with noise to exercise the `sd>0` branch.
2. **Task 6**: same root cause, different function — `build_symbol_table`'s test used
   `fwd_1d`/`fwd_5d` as clean scalar multiples of `rating_all`, tying both horizons'
   IC at exactly 1.0. The implementer's *first* fix was wrong (added an unrequested
   tiebreaker into the production function to satisfy the broken test) — caught in
   review, reverted, fixed at the fixture instead (added noise to `fwd_1d`).
3. **Task 6 (separate finding)**: `build_symbol_table()` raised `KeyError` on
   `pd.DataFrame([]).sort_values("best_ic")` when no symbol had enough data for any
   horizon. Genuine crash risk, inherited verbatim from the plan's own code — user
   approved adding an empty-columns guard.
4. **Task 9**: `tv_rating_eval.py` was missing its `if __name__ == "__main__(): main()"`
   guard entirely — present in the plan, present in the sibling report script, but the
   Task 5 implementer omitted it and neither self-review nor task review caught it
   (no test invokes the script as `__main__`). Running `python tv_rating_eval.py`
   directly produced silent no-op (exit 0, zero output, zero artifacts). This is
   exactly why the plan mandates an actual CLI run, not just a green test suite.

One plan-mandated coverage gap also surfaced and was closed: Task 7's `build_transition_chart`
had zero test coverage in the original plan (an omission, not an implementer shortcut) —
added a 2-test class covering the groupby path and the empty-input guard.

## Real backtest results (first look, full 69-symbol/1990-2026 universe)

- 476,531 symbol-day rows, 8,400+ trading days.
- Level IC for `rating_all`/`rating_ma` is small and **consistently negative** across
  1/3/5/10/21-day horizons (-0.0048 to -0.0122), growing more negative at longer
  horizons — i.e. the rating is mildly *contrarian*: bearish readings correlate with
  slightly *higher* forward returns than bullish readings, not lower.
- |IC| stays under the 0.02 noise floor at every horizon, but t-stats are large
  (up to -5.13) purely from sample size (8,400+ days) — textbook statistical-vs-
  economic-significance divergence, exactly the trap the signal-eval skill warns
  about. This is a **noise/weak-negative verdict, not a tradeable edge**, per the
  report's own stated thresholds.
- `rating_osc` alone looks noisier and less consistent across horizons than
  `rating_all`/`rating_ma`.
- Transition study: 20 distinct rating-label transition types met the 5-event minimum.
- Trade simulation (threshold-cross long/short, $10k/trade): 21,938 realized trades,
  36.6% win rate, net **+$378,073** — but split by side, longs are net +$975,545 while
  shorts are net **-$597,472**. Consistent with the mildly-contrarian IC: the short
  side (entering on strong_sell) tends to lose since price often doesn't keep falling.
  The low win rate + net-positive total implies a "many small losers, occasional large
  multi-week winners" pattern (spot-checked on AAPL: a losing long held 2-9 days for
  -2% to -8%, a winning long held 22 days for +23.7%).

This is a legitimate, reportable first-pass result — worth writing up formally (the
`experiment-writeup` skill) before drawing conclusions about trading logic, and worth
being skeptical of the long-side profitability specifically given no transaction costs
or slippage are modeled yet (explicitly out of scope per the design spec).

## State

All 9 tasks complete, all reviews clean, ledger at `.superpowers/sdd/progress.md` has
the full task-by-task record including Minor findings deferred to the final
whole-branch review (a few cosmetic dataviz/color items from Task 7, a docstring lag
and missing `<meta charset>` from Task 8). **Next step: dispatch the final
whole-branch code reviewer, then `superpowers:finishing-a-development-branch`.**
