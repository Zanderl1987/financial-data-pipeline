# Session Notes — 2026-08-03

**Branch:** master (merged; `interactive-backtest-explorer` deleted after merge, worktree removed)
**Session model:** Claude Sonnet 5

## What happened

User asked where things stood on "our backtesting tool" and asked for interactive
GUI/visualization work next, calling out Altair as a library to consider. Ran the full
`superpowers:brainstorming` -> `superpowers:writing-plans` -> `superpowers:subagent-driven-development`
pipeline end to end on a new feature: a live Dash app for exploring the existing unified
evaluation framework's (`evaluate.py` / `evaluation/`) results interactively, since the
current `generate_eval_report.py` only produces a static, non-interactive HTML report.

### 0. Context reconstruction

No prior memory of "the backtesting tool," so reconstructed history from git log +
session notes: there are two related systems — the original `tv_rating_eval.py` /
`generate_tv_rating_report.py` one-off (Fable-planned, 2026-07-17/18) and the newer,
general `evaluate.py` / `evaluation/` unified framework (2026-07-19 to 07-23, acceptance-
tested, the "real" engine today). Compute/statistics layer is solid; the gap is
visualization/interactivity, matching what the user actually asked for.

### 1. Brainstorming -> design spec

Worked through architecture (Dash over Streamlit, for native cross-filtering callbacks;
Altair explicitly considered and deferred to implementation time, not ruled out), views
(signal dropdown, live threshold sliders re-running the trade simulation, symbol
price/trade explorer, cumulative P&L), and robustness requirements (user picked all four:
performance, staleness guardrails, graceful edge cases, test coverage) via one-question-
at-a-time dialogue. Wrote and committed
`docs/superpowers/specs/2026-08-03-interactive-backtest-explorer-design.md` (`60e4c52`).

### 2. Implementation plan

Investigated the exact reusable pieces before planning: `evaluation/trades.py::simulate()`
+ `trade_summary()`, `evaluation/contracts.py::TradeRule`, `evaluation/adapters.py::
rating_cache()`/`tv_threshold_rule()`, `evaluation/registry.py`, and
`generate_eval_report.py`'s `find_latest`/`load_run`/chart-builder functions (confirmed
these had **zero test coverage** despite being load-bearing for the new app — added to
plan scope). Wrote a 10-task TDD plan with full code for every step to
`docs/superpowers/plans/2026-08-03-interactive-backtest-explorer.md` (`acedaae`).

### 3. Subagent-driven implementation

Set up an isolated worktree/branch (`.worktrees/interactive-backtest-explorer`, branch
`interactive-backtest-explorer`) after explicit user consent, since work would otherwise
have landed directly on `master`. Baseline: 141 tests passing before starting.

Executed Tasks 1-9 via fresh-subagent-per-task + task review (spec + quality) loop:

- **Task 1 (dash dependency)**: real environment bug caught by independently re-running
  the implementer's own verification command in a fresh process — `dash==4.4.1` fails to
  import outside Jupyter because the environment's pre-existing `comm==0.1.2` (anaconda-
  bundled) raises `NotImplementedError` in dash's `_jupyter.py`. Fixed by upgrading to
  `comm>=0.2.0` and pinning it in `requirements.txt`. Reviewer flagged the fix as "scope
  creep beyond the brief" — adjudicated and parked (the fix was controller-directed and
  necessary for the task's own stated goal).
- **Task 2**: backfilled 9 tests for `generate_eval_report.py`'s previously-uncovered
  `find_latest`/`load_run`/`classify_significance`.
- **Tasks 3-4**: `list_evaluated_signals()`, `load_signal()` — clean on first review.
- **Task 5 (`build_tv_threshold_rule`)**: task review caught a real defect — the
  implementer silently changed strict `<`/`>` crossing-boundary operators to `<=`/`>=`,
  which would have made the live rule NOT bit-for-bit match the existing, already-trusted
  `evaluation.adapters.tv_threshold_rule()` at boundary values. The task's own equivalence
  test didn't probe that boundary, so it passed anyway. One fix round: restored the exact
  spec'd operators and added a boundary-case test that does probe it.
- **Tasks 6-9**: `get_cache`/`simulate_live`, `baseline_vs_live`, the two chart builders,
  and the full Dash layout/callback wiring — all clean on first review after explicitly
  warning each implementer about the Task 5 incident and asking for verbatim transcription
  of the brief's code. Task 9 (the most complex, wiring all prior tasks together) was
  bumped to a standard-tier model given integration risk; review confirmed a byte-for-byte
  match to the brief with correct callback wiring, `mouseup` sliders, and reuse (not
  duplication) of every prior task's functions and `generate_eval_report`'s private chart
  builders. One Minor finding deferred: the trade-summary panel shows "no trade rule
  defined for this signal" for both the true no-trade-rule case and the no-signal-
  selected/artifacts-missing case — the run-banner above already shows the correct
  distinct message in the latter cases, so this is a secondary-panel wording nit.

Final suite after Task 9: 31 passed (22 `test_backtest_app.py` + 9
`test_generate_eval_report.py`), verified independently after every task rather than
trusting implementer-reported pass/fail claims (which caught the Task 1 environment bug
above — the implementer's first claim of a passing import was false).

### 4. Task 10 (manual verification) — done, via direct Dash callback HTTP requests

Claude-in-Chrome wasn't connected this session; user chose HTTP-level verification over
waiting to reconnect it. The worktree has no curated price data (gitignored,
worktree-local), so first ran a real `evaluate.py --adapter tv-rule` against the main
repo's checkout (real `storage/curated/` data): 21,989 trades, artifacts written.
Starting `backtest_app.py` itself surfaced a second real environment bug worth recording:
`query.py` resolves `storage/` paths relative to its own `__file__`, so running the app
by its worktree path (even with cwd set to the main repo) silently builds an empty
0-symbol cache — the worktree's `storage/` is empty by design. Diagnosed via direct
`ev_adapters.rating_cache()` calls with/without a sys.path hack, confirmed root cause in
`query.py:46-48`'s `os.path.dirname(os.path.abspath(__file__))`. Fixed for testing
purposes by running a temporary copy of `backtest_app.py` from inside the main repo
checkout (deleted after verification, never committed) — this is purely how a worktree +
`__file__`-relative-paths interact, not a code defect; a real single-checkout deployment
never hits it. Also hit and cleaned up a stale-background-process mess (multiple
`python backtest_app.py` processes fighting over port 8050, one still pointed at the
empty worktree storage) — killed all via PowerShell `Get-CimInstance Win32_Process`,
relaunched clean.

With a clean single process running against real data, verified via direct
`POST /_dash-update-component` requests (reading Dash's own `/_dash-layout` and
`/_dash-dependencies` endpoints first to get exact component IDs/callback shapes): signal
dropdown populated with all 12 registry signals including `tv_threshold`; sliders at
correct defaults (0.5/0.1/-0.5/-0.1) with correct range/step/mouseup mode; selecting
`tv_threshold` builds a real 69-symbol cache and the default-threshold live trade summary
exactly matches the recorded baseline (21,989 trades / 36.7% / $374,236); symbol chart
renders 5 traces (price + win/loss-colored entry/exit markers); P&L chart renders real
cumulative data. Edge cases: near-impossible thresholds -> "0 realized trades" + price-only
chart + empty P&L fig; `factor_value` (no TradeRule) -> "no trade rule defined for this
signal", IC panel still renders its 3 charts; Refresh -> banner timestamp updates.

### 5. Final whole-branch review — 7 Important findings, all fixed in one round

Dispatched on the most capable available model per the skill's guidance. Confirmed the
architecture holds (zero new backtest math anywhere, every number traced back to
`evaluation.trades.simulate()`/recorded artifacts) but found 7 real Important-severity
issues, none of them caught by task-level review because each only became visible once
the whole file was read end-to-end:

1. `_CACHE` never invalidated on Refresh — a newer run_id updated the banner but kept
   serving simulation results from the stale in-memory panel.
2. The live cache silently ignored the loaded run's recorded universe (no threading, no
   warning) — latent today, would silently produce apples-to-oranges live-vs-baseline
   numbers after e.g. a universe change.
3. **The IC & Significance panel was permanently blank for `tv_threshold`** — the app's
   only tunable signal, and its own flagship view rendered nothing, because
   `_render_ic_panel` only ever read `results["ic"]`, which doesn't exist for
   `trade_rule`-type runs.
4. Switching the symbol dropdown re-ran the full 69-symbol simulation instead of just
   filtering the already-computed trades.
5. No loading spinner during the ~29s first-load cache build, despite the spec explicitly
   calling for one.
6. A `TypeError` crash path if a baseline summary lacked `total_pnl_dollars` (e.g. a
   zero-trade recorded run) — `baseline_vs_live`'s own test blessed `None` as valid output
   the caller never actually handled.
7. Two subagent fix-round report files (`task-1-report.md`, `task-5-report.md`) got
   committed at the repo root by mistake instead of their intended gitignored path — one
   contained a non-ASCII `✓` glyph against this repo's ASCII-only convention.

Wrote out the complete exact fix code for all 7 (plus folding in Task 9's deferred minor
message-wording issue for free) and dispatched one fix subagent rather than one per
finding. Independently re-verified the diff line-by-line against the exact spec before
the scoped re-review, which came back clean (all 7 ADDRESSED, one new Minor noted and
accepted: `_SIM_CACHE` has no eviction, fine for a single-user local tool). 35/35 tests
passing after the fix.

## Net result

`backtest_app.py` — a new, standalone live Dash app for exploring `evaluate.py`/
`evaluation/`'s results, not wired into `run_all.py`/`curated.py` — is merged to
`master` (fast-forward, commit `69912da`). Full suite: 532/532 passing on the merged
tree (one worktree-only failure during interim testing, `test_storage_dirs_exist`,
confirmed to be a fresh-worktree-has-no-gitignored-data artifact, not a regression —
passes clean on `master`). Worktree removed, feature branch deleted, work not yet pushed
to `origin` (14 local commits ahead as of session end).

## Next up

- Push to `origin` if/when desired (not done this session — merge was local-only).
- Unrelated, still parked from prior sessions: AV DJI earnings-pacing 4-option decision;
  Reddit/Comtrade/Census/USDA/AISStream all blocked purely on user API keys (see
  `TODO.md`).
