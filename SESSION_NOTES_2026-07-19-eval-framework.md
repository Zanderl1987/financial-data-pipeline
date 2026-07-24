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

## Session continuation — 2026-07-20 (Claude Sonnet 5, execution)

Zander chose Subagent-Driven Development. Worktree confirmed at
`.worktrees/eval-framework` (branch `eval-framework-impl`, based on `5bd7815`).
Executed Tasks 1–11 of 12 this session (Tasks 1–6 had already landed in an
earlier part of this same session before a resume): fresh implementer
subagent per task + task-reviewer subagent (spec compliance + code quality),
per the skill. All eleven approved; only Minor findings throughout, no
Critical/Important issues survived review. Progress ledger:
`.superpowers/sdd/progress.md` (commit range per task, reviewer verdict).

## Session continuation — 2026-07-22 (Claude Sonnet 5, Task 12 acceptance run)

Plan executed through Task 12 — the acceptance gate. No new framework code;
ran the already-built framework against real curated parquet and reproduced
the legacy baselines.

**Step 1 — test suite:** `pytest tests/ -q` → 440 passed, 1 failed
(`test_catalog.py::TestCatalogPaths::test_storage_dirs_exist`), traced via
`git log -- query.py` to commit `a0b78b0` (before this branch existed) —
CATALOG entries for the separate in-flight constituents-pipeline work whose
storage dirs were never populated. Not a new failure from this task.
`tests/test_evaluation.py` alone: 92/92 passed.

**Step 2 — sentiment acceptance:** `evaluate.py --adapter sentiment
--n-boot 500 --n-perm 100` reproduced `SENTIMENT_EVAL_RESULTS.txt`'s
2026-07-07 `sentiment_eval.py` table to 4 decimal places at every horizon:

| h | pooled_ic | daily_ic | t | legacy pooledIC |
|---|---|---|---|---|
| 1 | 0.0086 | 0.0100 | 0.88 | 0.0086 |
| 3 | -0.0113 | -0.0213 | -1.95 | -0.0113 |
| 5 | -0.0054 | -0.0108 | -0.92 | -0.0054 |
| 10 | 0.0175 | 0.0046 | 0.42 | 0.0175 |
| 21 | 0.0045 | -0.0154 | -1.40 | 0.0045 |

All horizons verdict noise (|daily IC| < 0.02 or |t| < 2 at every h). Well
within the ±0.005 tolerance — effectively an exact reproduction.

**Step 3 — TV rating acceptance:** `evaluate.py --adapter rating
--signal-col rating_all --n-boot 500 --n-perm 100`: daily ICs -0.0049
(h1) to -0.0115 (h21), t down to -5.16 at h21. Matches
`SESSION_NOTES_2026-07-18.md`'s recorded range (-0.0048 to -0.0122, t up
to -5.13) almost exactly. |IC| < 0.02 at every horizon → noise verdict
throughout, consistent with the recorded "mildly contrarian, not a
tradeable edge" read.

**Step 4 — 9 factor first baselines** (`evaluate.py --adapter signal-panel
--factor <f>`, all exit 0, none empty): headline daily_ic (h=1 / h=21):
momentum +0.0190 / +0.0332 (t up to 11.53 — clearly the strongest, real
signal); value -0.0036 / -0.0063; quality +0.0004 / -0.0154; low_vol
-0.0005 / -0.0448 (t -15.27 at h21 — strong and growing, worth a closer
look later); growth +0.0050 / +0.0093; short_pressure -0.0757 (h1 only,
n/a beyond h5 — thin data, only 9 distinct dates); insider_flow -0.0085 /
-0.0582; sentiment(factor) -0.0184 / -0.0798 (this is the fundamentals-
style `analytics.signals` sentiment factor, distinct from the VADER
`news_sentiment` adapter in Step 2 — much stronger/more negative, first
baseline only, no legacy comparison exists); composite +0.0118 / -0.0049.
All recorded as first baselines in the registry, nulls included
(short_pressure has no h10/h21 due to sparse data).

**Step 5 — event + trade-rule smoke:** `rating-changes --start 2024-01-01`
exits 0 (upgrade n=8913, downgrade n=8918, no legacy baseline to compare).
`tv-rule --n-perm 200` exits 0: 21,938 trades (14,230 long / 7,708 short),
36.6% win rate, net pnl $378,073.23 — matches `SESSION_NOTES_2026-07-18.md`'s
recorded trade summary (21,938 trades, 36.6% win rate, net +$378,073)
essentially exactly.

**Step 6 — registry + report:** registry = 3,893 rows, 13 distinct
input_names (>= 11 required): factor_composite, factor_growth,
factor_insider_flow, factor_low_vol, factor_momentum, factor_quality,
factor_sentiment, factor_short_pressure, factor_value, news_sentiment,
tv_rating_all, tv_rating_changes, tv_threshold. HTML report
(`generate_eval_report.py --latest news_sentiment`) wrote a 3.5 MB file
with correct title/headline section; `Start-Process` skipped per task
instructions (environment can't open a browser) — file-size + content
grep substituted.

**No deviations from the recorded baselines** — both gating checks
(sentiment, TV rating) reproduced within tolerance on the first run; no
re-audit or framework changes were needed.

Files touched this task: `docs/EVALUATION.md` (new), `CLAUDE.md` (Commands
+ Architecture pointer), this file. No `evaluation/*.py`, `evaluate.py`, or
`generate_eval_report.py` changes — acceptance run only.

- Task 7 (Tier-3 battery): clean. 2 Minor (unguarded `n_folds=0`, undocumented
  magic number), both traced to the brief's own reference code.
- Task 8 (registry.py): clean, verified byte-identical transcription.
- Task 9 (runner + evaluate.py CLI): clean, verified byte-identical
  transcription; reviewer independently cross-checked every integration call
  site against the real Task 1–8 signatures, zero mismatches.
- Task 10 (adapters): implementer caught a genuine typo in the PLAN's own
  test literal — `test_tv_threshold_rule_matches_legacy_semantics`'s
  `short_exits` assertion had `False` at index 3, contradicting both its own
  inline comment and the real `tv_rating_eval.py` `EXIT_SHORT_MIN` semantics.
  Independently verified by hand (0.05 > -0.1 is True) before fixing both the
  plan file and the extracted brief, then re-dispatching. Otherwise clean;
  reviewer independently checked every adapter call site against the real
  `analytics.signals`/`sentiment_eval`/`tv_rating_eval`/`event_backtest`
  signatures, zero mismatches.
- Task 11 (generate_eval_report.py): implementer reported DONE_WITH_CONCERNS —
  the `claude-in-chrome` browser tool was unavailable in its environment, so
  the brief's Step 5 visual eyeball couldn't be done; substituted a
  structural check. Controller independently rendered real signal and
  trade-rule reports from the actual module (not test fixtures) and verified
  the style contract by inspecting the raw HTML/Plotly JSON directly: fixed
  categorical colors in order, status colors correctly applied to regime
  bars, zero-line via the axis `zeroline` property (not a `shapes` entry —
  worth knowing if auditing this again), single y-axis throughout, correct
  legend behavior, pure ASCII, no leaked `None`/`NaN`. Browser tool was also
  unavailable to the controller when attempted directly — this remains an
  environment gap, not a code gap.

## Task 12 — real-data access, resolved (2026-07-22)

Task 12 is the acceptance gate: run the framework against REAL curated data
and reproduce the recorded sentiment/TV-rating baselines. The worktree had no
`storage/curated/` (gitignored, never populated there — a non-issue for Tasks
1–11 since those only used synthetic/monkeypatched data, but Task 12
explicitly requires the real store). The real curated data only exists in the
main repo checkout, which currently carries substantial unrelated in-flight
uncommitted work (the constituents/securities pipeline session).

Resolved via option (a) from the choices originally raised: a directory
junction from the worktree's `storage/curated` to the main checkout's real
one (`C:\Users\zande\PycharmProjects\financial-data-pipeline\storage\curated`)
— confirmed present via `Get-Item` (`LinkType: Junction`). No copying; Task
12 only reads curated data through this junction and writes its own new
output solely to `storage/eval_registry/` and `storage/reports/eval/`, both
worktree-local and untouched in the main checkout. See the "Task 12
acceptance run" section above for the resulting numbers, reproduced against
this real data.

Task 12 itself review: clean after 1 fix round (commits `898f724..757637e`).
Important: this file had a stale, self-contradictory "BLOCKED, not yet
resolved" section left over from an earlier draft, replaced with the
resolution text above. Minor: cosmetic only.

## Final whole-branch review + fix + re-review (2026-07-22, same session)

Plan status: **all 12 tasks complete.** Per subagent-driven-development, ran
the final whole-branch review on the most capable model (opus) against the
full range `5bd7815..757637e` (16 commits, all 12 tasks). Verdict: **Ready
to merge = Yes**, no Critical findings. The PIT/lag-applied-once invariant,
no-raw-price-globs rule, local-import convention, forbidden-file (`run_all.py`
/`curated.py`/`validate.py`/legacy eval scripts) untouched-ness, single-choke
registry schema, and `.gitignore` coverage of `storage/eval_registry/` all
held across the whole branch, not just per-task.

Two Important (non-blocking but real) findings surfaced only by the
whole-branch view:
1. `runner.py::_run_signal`'s deflated-Sharpe block appended the current
   run's own sharpe to `registry.population("sharpe", ...)`, which already
   returns latest-per-signal-name — double-counting a signal's own prior
   run on any re-run, inflating the DSR trial count (conservative direction,
   but undercuts the "honest N trials" property that's the point of DSR).
2. `_stat_rows` wrote every numeric leaf of every result dict as a registry
   "statistic" row, including count/flag metadata (`n`, `top_n`, `bottom_n`,
   `oriented`, `n_trades`, etc.) — polluting `registry.compare()`/
   `baselines()`, the framework's own regression-check primitives.

Consistent with how every other Important finding in this plan was handled,
dispatched one fix subagent (sonnet) rather than deferring these. Fix
landed as commit `01dbcb3` ("fix(evaluation): exclude own prior DSR trial +
drop metadata rows from registry"), touching only `evaluation/registry.py`
(new `population(..., exclude_input_name=)` param), `evaluation/runner.py`
(`_run_signal` passes `exclude_input_name=obj.name`; new `_METADATA_KEYS`
blocklist in `_stat_rows`, audited against every stat-dict call site so no
real statistic was dropped), and `tests/test_evaluation.py` (+3 tests).
`pytest tests/test_evaluation.py -q` → 95 passed (was 92).

Re-review (opus, scoped: confirm both fixes correct + no scope creep + no
new issues, light sanity pass on the rest) against `5bd7815..01dbcb3` (17
commits): **Ready to merge = Yes**, both Important findings genuinely
resolved at the root (exclusion happens before the groupby; blocklist
verified key-by-key against every `_stat_rows` call site), exactly the 3
named files touched (+112/-4), no new Critical/Important. 2 Minor notes
left as-is (not blocking): `population()` filters by statistic name across
all evaluation namespaces rather than scoping by evaluation (harmless today,
`"sharpe"` only written by `bootstrap_sharpe`); `sr0_ann` kept as a registry
stat even though it moves with N rather than signal skill.

**Plan complete. Branch `eval-framework-impl` is ready to merge to
`master`** (`.superpowers/sdd/progress.md` has the full per-task ledger).
Final HEAD: `01dbcb3`. Not yet merged — the main checkout has ~31 lines of
unrelated uncommitted work from the separate constituents/securities
pipeline session sitting on `master`, and `master` itself is 28 commits
ahead of `origin/master` (unpushed). Merge/PR/keep/discard decision is
Zander's call, offered via the finishing-a-development-branch skill;
answer not yet given as of this note.

Also worth remembering from this stretch: `rtk`'s git-command rewriting is
not always semantics-preserving — a plain `git restore --staged <file>` got
rewritten into something that also touched the working tree and deleted an
untracked scratch file (no data lost that time, content was already
committed elsewhere). Logged to memory; use `rtk proxy git ...` for
restore/reset/clean/checkout-- inside a worktree with uncommitted work
nearby.

## Merge, push, and cleanup (2026-07-22, same session)

Zander approved Option 1 (merge locally) once told the branch was clean.
Before merging: the main checkout had ~31 lines of uncommitted constituents/
securities-pipeline work on `master`, one file of which
(`SESSION_NOTES_2026-07-19-eval-framework.md` itself) overlapped with what
the branch also changed. Stashed it (`git stash push -u`) to get a clean
tree first — this hit the `rtk` rewrite hazard again (the push didn't fully
reset tracked files even though it did capture everything; verified
byte-identical via file-by-file `diff --strip-trailing-cr` against the
stash contents before finishing the reset with `rtk proxy git checkout --`).

Merge: `git merge eval-framework-impl` was a clean fast-forward,
`5bd7815..01dbcb3`, 18 files, no conflicts. `pytest tests/test_evaluation.py
-q` on merged master: 95/95 passed. Worktree removed (after manually
unlinking the `storage/curated` junction first, non-recursively, so the
removal couldn't cascade into the real curated data) and branch
`eval-framework-impl` deleted (`-d`, safe — fully merged).

Restoring the stash back needed two `stash pop` attempts: the first
correctly aborted on the same overlapping session-notes file (git protects
against overwriting local changes) but did restore the untracked files;
the second (after resetting that one file to HEAD) auto-merged the rest
cleanly. My own uncommitted session-notes addition had been reset away for
the pop, so it had to be reapplied from a backup afterward — no content was
actually lost, just briefly staged in a temp copy. Also found a
`storage/raw/iceberg` junction duplicating `storage/iceberg`'s 29 tracked-
worthy metadata/catalog files under a second path — left it alone at the
time (harmless, untracked).

Pushed `master` to `origin` (`01dbcb3`, 45 commits, clean fast-forward,
0 behind).

Zander then asked to commit the constituents session's own uncommitted
work. Reviewed before staging: no secrets found (grepped new pipeline
files + README for API-key/token/password patterns, checked
`upload_huggingface.py` — reads `HUGGINGFACE_TOKEN` from `.env`, no
hardcoded values), and `storage/raw/iceberg` was excluded from the
commit — committing it would have baked a machine-specific absolute
junction target into the repo and duplicated the same 29 files under a
second path. Committed as `d5dd859` ("feat: index constituents/securities
pipeline + Iceberg migration": index_constituents/securities_reference/
fund_holdings/openfigi/omkar_commodity pipelines, audit tooling, Iceberg
migration for constituents + shipping, shipping FRED expansion 8->18
series, README/ARCHITECTURE/PIPELINE_CATALOG docs, requirements.txt) and
pushed.

This file's own pending edits (the "Final whole-branch review" section
above) and the plan file's test-literal correction were then committed
separately as `33c0cad` ("docs(evaluation): record final whole-branch
review, fix, and re-review") and pushed, since they're eval-framework
content, not constituents-session content.

Finally removed the `storage/raw/iceberg` junction (non-recursive delete,
verified `storage/iceberg`'s real data intact afterward) — it was never
committed, so this was a pure filesystem cleanup, not a git operation.

**End state:** `origin/master` == local `master` == `33c0cad`. Working
tree fully clean. Eval-framework plan (12 tasks + final review + fix)
merged and live. Constituents/securities pipeline session's work
committed and live. No outstanding uncommitted state from either thread.

This section itself (the "Merge, push, and cleanup" epilogue above) was
then committed as `9546d5c` ("docs(evaluation): record merge, push, and
cleanup epilogue") and pushed.

**Final end state:** `origin/master` == local `master` == `9546d5c`.
Working tree clean. Session complete.

## HuggingFace dataset upload (2026-07-23, new session)

Reviewed `upload_huggingface.py` (added uncommitted in `d5dd859`, never
run) before executing it. Found one real bug: the script hardcoded
`ZanderL1337` as the GitHub org in the generated README's Pipeline link,
but the actual GitHub org (per `git remote -v`) is `Zanderl1987` — the
link would have pointed at a nonexistent repo. Confirmed with Zander that
`ZanderL1337` (HF namespace, a separate identity from GitHub) is correct
and left as-is; fixed only the GitHub link.

Confirmed via web search that on a free HF account, public dataset repos
get free/effectively-unlimited storage (~1TB soft cap, raised on request)
while private repos are capped at 100GB total — so public was the right
default for this dataset's size trajectory, not just a visibility
preference. Left `private=False` (the script's existing default)
unchanged.

Ran `upload_huggingface.py`: uploaded 114 tables, 9,993,893 rows, 223.6 MB
to `https://huggingface.co/datasets/ZanderL1337/financial-data-pipeline`
(public). Verified live via WebFetch (browser extension wasn't connected
in this background session) — README rendering correctly, source-category
breakdown and tags all correct, 234 MB reported on the page.

Committed the GitHub-link fix plus the regenerated
`storage/curated/README.md` (tracked in git, refreshed with the live
upload's stats) as `9ec4524` ("fix(upload_huggingface): correct GitHub
org in README template") and pushed.

**End state:** `origin/master` == local `master` == `9ec4524`. Working
tree clean. HuggingFace dataset live and verified.

## EXPERT_BRIEF roadmap items 1-4 + full backfill (2026-07-23, new session)

Zander asked to work through `EXPERT_BRIEF.md` roadmap items 1-4 in order, then
backfill the full pipeline now that public HF storage removed the size constraint
that was previously gating the Schwab full price-history pull. Researched via three
parallel Explore agents (run_all.py structure + per-source quota table, earnings
history blocker, factor-eval interface) before touching anything, since a couple of
these steps burn shared/limited quota or take hours.

**Item 1 — daily automation: done.** Registered `ClaudeAuto-DailyAccumulators`
(daily 9:00 AM, `run_all.py --only tradingview,short_interest,finnhub_events`,
mirrors the existing `ClaudeAuto-PipelineQuality` task pattern). Added
`finnhub_events` to the accumulator set beyond what `AUTOMATION.md` originally
scoped (tv/short_interest only) — confirmed live that `earnings_calendar` is also
a use-it-or-lose-it accumulator (see item 3 finding below), so it needed the same
daily-run protection. Wrote `scripts/daily_accumulators.ps1`, flag file
`DAILY_ACCUMULATOR_FAIL.txt`. Updated `AUTOMATION.md`.

**Item 2 — Schwab OAuth: waiting on Zander.** Interactive, code expires ~30s — I
cannot run this myself. Asked him to run
`schwab_quotes_pipeline.py` in a real terminal to create `tokens.db`.

**Item 3 — historical earnings: real finding, not yet a full fix.** Confirmed
(via a live 2026-07-18 backfill re-check, not assumption) that Finnhub's free
tier hard-caps earnings data at ~1 year regardless of requested date range —
`finnhub_events_pipeline.py`'s `--backfill` already requests 365 days and gets
nothing older than "now." This falsifies the assumption in
`FinancialDataPipeline_Future_Improvements.md` §D that a pipeline-code extension
would fix it. Probed Alpha Vantage's `EARNINGS` endpoint instead (2 requests
spent from the shared 25/day quota, `fetch_earnings()` in
`alpha_vantage_fundamentals_pipeline.py`, already wired but never populated) —
AAPL and MSFT both returned full history back to 1996 (121 quarterly + 31
annual rows each). This is a real fix, but populating
`alpha_vantage_earnings`/`alpha_vantage_earnings_calendar` for the full
watchlist is quota-gated and shares the same per-IP 25/day budget as
`custom_index_tool`'s TranscriptPull/EarningsSurprisePull — not run yet, needs
pacing across multiple days.

**Item 4 — factor evaluation pass: COMPLETE.** Re-ran
`evaluate.py --adapter signal-panel --factor <f>` for all 8 factors + composite
(2026-07-23, background job `biavxk2y2` after `b9sl4qd8b` for momentum). The
2026-07-22 acceptance-gate run (see Step 4 above) computed portfolio metrics
for all factors as a side effect but never recorded them — registry/reports
artifacts are gitignored and lived only in the now-deleted worktree. This run
captured real numbers:

| Factor | Sharpe [95% CI] | Signal quality | Verdict |
|---|---|---|---|
| momentum | 0.55 [0.23, 0.88] | positive IC every horizon, t=6.5->11.5 | only statistically robust positive factor |
| composite | -0.11 [-0.45, 0.21] | h=1 IC t=4.79 (real), decays to t=-1.46 by h=21 | short-horizon signal real, dragged down by low_vol (see below) |
| growth | 0.15 [-0.18, 0.48] | weak positive, t<=1.97 | not significant |
| insider_flow | -0.07 [-0.41, 0.20] | negative, worsens with horizon (t=-5.89 @21d) | not significant, wrong-signed |
| quality | -0.10 [-0.41, 0.20] | negative at longer horizons (t=-3.77 @21d) | not significant |
| sentiment | -0.13 [-0.48, 0.22] | negative, t=-5.42 @21d | not significant |
| value | -0.25 [-0.56, 0.04] | negative, CI hugs zero | borderline negative |
| short_pressure | -0.22 [n/a, n/a] | only 43 registry rows vs 121 for the rest (dates too sparse for CI) | not a real evaluation yet — insufficient coverage |
| low_vol | -0.81 [-1.15, -0.51] | strongly negative, t=-15.27 @21d | **statistically significant negative — signal is inverted**, not just noise |

**Recommendation:** `analytics/signals.py:52-59` `DEFAULT_WEIGHTS` currently
weights all 8 factors equally at 1.0. `low_vol`'s CI sits entirely below zero
(the only factor besides momentum with a CI that excludes zero) — it is
actively pulling the composite down every day it's non-NaN, and is the
likely explanation for why composite shows a real short-horizon IC
(t=4.79 @ h=1, momentum's fingerprint) but a flat/insignificant portfolio
Sharpe overall. Proposed action (not yet applied — needs Zander's OK since
it changes a scored analytics output): zero out `low_vol` in
`DEFAULT_WEIGHTS` (worth first checking whether flipping its sign instead
recovers a positive Sharpe, before assuming it's dead rather than inverted),
and down-weight value/quality/sentiment/insider_flow toward 0 since none
clear statistical significance. `short_pressure` can't be judged yet —
coverage will deepen via `ClaudeAuto-DailyAccumulators` (see Item 1) since
it's a snapshot-only source; re-evaluate in a few weeks once it has more
distinct dates.

**Item 5 — full stage-1 backfill: not started.** Plan (per Zander's choice) is
dry-run `run_all.py --backfill --stage 1` first to preview the full command
list and flag known-broken sources (`nasdaq_data_link`) before spending real
time/quota, then run for real in the background.

Also started `PROJECT_NOTES.md` (new, living/updated-in-place reference doc —
distinct from this file's chronological narrative and from `EXPERT_BRIEF.md`'s
roadmap framing) to hold durable state: active automations, verified hard API
constraints, cross-repo AV quota sharing, and this initiative's in-flight
status. Wired into `CLAUDE.md`'s "Where deeper knowledge lives" section.

**Not yet committed** — working tree has automation script, `AUTOMATION.md`,
`PROJECT_NOTES.md`, `CLAUDE.md`, and this session-notes edit uncommitted as of
this writing; commit once the factor-eval pass and backfill dry-run are further
along, per Zander's usual pattern of committing at natural checkpoints rather
than mid-stream.

## low_vol decision, backfill completion, commits, futures partition fix (2026-07-23, same session continued)

Picked back up after a `/clear`. The stage-1 backfill (item 5 above) was still
running live from the prior session (PID 19220) — armed a persistent `Monitor`
poll on the process rather than blocking, and worked the `low_vol` decision in
parallel while waiting.

**low_vol decision — resolved and applied.** Before deciding zero-vs-flip,
checked the eval registry's regime breakdown (`tier3_regime_bull/bear/high_vol/
low_vol` + `tier3_wf_oos`) rather than trusting the single pooled number:
negative IC in *every* slice (bull t=-5.71, bear t=-2.18, high-vol t=-4.87,
low-vol t=-3.23, walk-forward OOS t=-5.90), full-sample pooled t=-15.27 @ h=21
over 452k obs, 1990-2026. Also checked `vol_21d`'s computation
(`analytics/features.py:132`) for a look-ahead bug given how strong the
inversion was — it's a plain trailing rolling std, same PIT treatment as every
other factor, no bug found. Regime-invariance + no lookahead bug together made
this a real, robust finding rather than a bull-market fluke, so recommended
sign-flip over zero-out. Zander approved both the flip and zeroing the other
four insignificant factors (value/quality/sentiment/insider_flow).

Implemented in `analytics/signals.py`: `low_vol` now `= df["vol_21d"]` (was
`= -df["vol_21d"]`), column name kept for continuity but now longs volatility.
Updated the one test that asserted the old sign (`test_low_vol_is_negated_vol`
-> `test_low_vol_tracks_vol`). `tests/test_signals.py` 16/16 pass. Re-ran
`evaluate.py --adapter signal-panel --factor composite` to confirm the change
actually helps rather than trusting the theory: Sharpe -0.11 [-0.45, 0.21] ->
**1.01 [0.70, 1.32]**, IC t-stat (h=21) -1.46 -> 18.88, deflated Sharpe prob
0.91. Substantial, not marginal.

**Stage-1 backfill completed** (117m12s total): 27 PASS, 2 FAIL (`fed_soma`
timed out at its internal 3600s limit; `google_trends` missing `pytrends`
dependency), 6 SKIP (all expected). Full breakdown and new source breaks in
`PROJECT_NOTES.md` item 5 (kept in sync there rather than duplicated here).

**Committed in 3 commits** once Zander said to commit what we had:
- `c0e4748` — docs: daily-accumulator automation + new `PROJECT_NOTES.md`
- `3b363af` — fix: `low_vol` sign-flip + zeroed insignificant weights
- `40c9df7` — data: Iceberg snapshot refresh from the backfill (149 new
  `fund_holdings` snapshot files flagged as a growth concern, not fixed —
  Zander chose "commit as-is now, investigate later" when asked)

**futures partition mismatch — fixed, but was one of a class of 7.** Zander
asked specifically about `futures`; investigating turned up 6 more raw-store
directories with the same shape of bug (legacy unpartitioned file colliding
with a newly-written `year=/month=` sibling, breaking `curated.py`'s
`read_parquet(..., hive_partitioning=true)` for the whole run, not just the
one table): `sec_edgar/submissions`, `sec_edgar/xbrl_fundamentals`, `cot`,
`synthetic_options`, `options_history` (4 symbol files). Found all of them at
once via `glob.glob('storage/raw/**/*.parquet', recursive=True)` filtered to
paths missing `year=`, rather than fixing one-by-one by rerunning `curated.py`
each time. Moved each into its correct `year=/month=` dir (partition inferred
from the file's own `fetched_at`/`fetch_date` column, not file mtime). Also
hit and removed a second, unrelated blocker: the timed-out `fed_soma` run left
a truncated parquet file (`No magic bytes found` — confirmed corrupt via
`pyarrow.parquet.ParquetFile`) that was failing `curated.py` for a different
reason; deleted it (gitignored raw data, unreadable, `fed_soma` needs a
proper re-run anyway).

Verified: `curated.py` compacts all 133 tables clean, no errors. `futures`
loads via `query.load('futures')` — 183,281 rows, 30 symbols, 1997-10-29 to
2026-07-23 (June + July raw files both present and merged). Full test suite:
453 passed, 1 failed — but the failure (`test_storage_dirs_exist`) is
unrelated to this fix (confirmed no directories were removed by the file
moves). That test caught a real, separate, pre-existing bug: `query.py`'s
`CATALOG` entries for 6 Iceberg-backed tables resolve through `_glob()`,
which always roots at `storage/raw/`, but the actual Iceberg data lives at
`storage/iceberg/...` — those 6 paths can never exist. Looks like a leftover
from yesterday's `d5dd859` Iceberg migration. **Not fixed** — flagged to
Zander, awaiting a decision on whether to tackle it now or later. Also not
yet committed as of this writing (the file moves + this note).

## Iceberg CATALOG fix + pytrends dependency (2026-07-23, same session continued)

Zander asked to fix the Iceberg CATALOG path bug next. Before touching
`query.py`, checked whether `_register_views()` errors or silently no-ops
when a glob matches zero files — it silently `continue`s. That explained why
`curated.py` never complained about these 6 tables: their views (and
`index_holdings`, the composite view joining 3 of them) simply never existed,
with no error surfaced anywhere. Added `_iceberg_glob()` (rooted at
`storage/iceberg/`, next to the existing `_glob()` rooted at `storage/raw/`)
and repointed the 6 CATALOG entries.

Before calling that "fixed," checked whether these tables would return clean
data once their views existed — naively globbing an Iceberg table's `data/`
directory reads every data file ever written across every commit, not just
the current snapshot (query.py doesn't use a real Iceberg snapshot-aware
scan). Checked for duplicates on each table's natural key: `index_members`
0.05% dupes, `fund_holdings` ~1%, but `securities` 49.7% and both shipping
tables ~50% — periodic full re-fetches, not corruption. Confirmed these 6
were never wired into `curated.py`'s `KEYS` either (a second gap from the
`d5dd859` migration — there was even a stale comment claiming shipping was
"Iceberg-managed, no curated needed," which is false given how query.py
actually reads it). Added natural keys for all 6. `securities` uses
`last_refreshed` instead of `fetched_at` for its timestamp, which
`_sort_recency` didn't recognize — added it, spot-checked NVDA's dedup keeps
the 07-23 refresh over 07-17 (i.e. actually freshest, not just last-in-glob-order).

Verified: `curated.py` compacts 139 tables (was 133), `index_holdings` (previously
silently empty) returns 15,199 rows, full suite still 453 passed with only the
same pre-existing unrelated failure (never-run pipelines) remaining. Committed
`8e7b05f`.

Then fixed the last item on the list: `google_trends`'s missing `pytrends`
dependency. Already listed in `requirements.txt` (unpinned) — just never
installed. `pip install pytrends` (4.9.2), ran the pipeline live (3 keyword
groups, 1,365 rows each), reran `curated.py` (142 tables, clean). No repo file
changed, so nothing to commit for this one — env-only fix.

All three follow-up issues found while fixing the original `futures` ask are
now resolved: Hive partition stray files (prior section), Iceberg CATALOG
path bug, and `google_trends`. `PROJECT_NOTES.md` updated in place to reflect
all of this; this file gets the narrative. Of the original 5-item roadmap
initiative, only items 2 (Schwab OAuth) and 3 (AV earnings backfill) remain,
both blocked on external factors (Zander's interactive action; AV quota
pacing) rather than anything left to build.

## PatentsView / FDIC re-check, session continued (2026-07-23, later same day)

With items 2 and 3 both externally blocked (Schwab needs Zander at an
interactive terminal; AV's shared daily quota window is ~10:05-10:30am and
it was already 16:45 by this point), picked up the one open, unblocked
thread from the stage-1 backfill notes: the `patents`/`fdic` DNS failures
flagged as "likely transient, re-run before concluding broken."

Re-ran both live. `fdic_pipeline.py` pulled clean — 4,255 institutions,
98,669 financials rows, 4,115 failures — confirming that one really was a
transient blip. `patents_pipeline.py` failed again with the identical
`getaddrinfo failed` error for `search.patentsview.org` across all 6 CPC
sectors. Before writing that off as "still transient, try again later,"
checked with `nslookup`: `search.patentsview.org` is a genuine NXDOMAIN
(non-existent domain), while `patentsview.org` itself and an unrelated
control host (`www.google.com`) both resolve fine — ruling out a general
local DNS/network problem.

Used WebSearch + WebFetch to confirm why: PatentsView migrated to the USPTO
Open Data Portal (`data.uspto.gov`) around March 2026 — `patentsview.org`
now 301-redirects there, and reporting suggests the original API's
endpoints return 410 Gone. `patents_pipeline.py`'s hardcoded
`BASE_URL = "https://search.patentsview.org/api/v1/patent/"` is pointed at
a hostname that no longer exists in DNS at all, not one that's merely
rate-limiting or flaking. This is a real breaking change needing a pipeline
rewrite against the new ODP API (different auth, `size`/`after` paging
instead of `per_page`/`page`) — not attempted here, since it's a scoped
rewrite job of its own, not a quick re-run check. Same class of problem as
the `oecd` dead-endpoint finding from the backfill.

Reran `curated.py` after the FDIC refresh: 142 tables, clean, no partition
errors. `PROJECT_NOTES.md`'s constraint table entry updated in place — split
the old combined "PatentsView / FDIC failures (probably transient)" row into
two: FDIC confirmed fixed, PatentsView confirmed a real dead endpoint
pending rewrite. Committed `bae8457`.

Checked sync against GitHub (`origin/master`): local was 7 commits ahead,
0 behind — this whole roadmap initiative's worth of commits, unpushed.
Zander confirmed; pushed all 7 (`ebc3bfb..bae8457`). Repo is now in sync.

Of the original 5-item roadmap, only items 2 (Schwab OAuth) and 3 (AV
earnings backfill) remain, both externally blocked as described above.
PatentsView's rewrite (found this session, not part of the original 5) is
a new, not-yet-started open item.

## Schwab OAuth completed (2026-07-24, same session continued)

Zander asked to start the Schwab OAuth flow. The Chrome browser extension
wasn't connected on this machine, so couldn't drive the login tab directly;
instead constructed the auth URL from `.env` (`SCHWAB_API_KEY` +
`SCHWAB_CALLBACK_URL`, formula copied from `schwabdev/tokens.py`) and had
Zander open it, log in, and paste back the resulting (failed-to-load)
`https://127.0.0.1:8182/?code=...` redirect URL.

Rather than running `schwab_quotes_pipeline.py` interactively (which blocks
on a terminal `input()` call this tool session can't supply), completed the
token exchange programmatically: `schwabdev.Client(..., call_on_auth=lambda
auth_url: redirect_url, open_browser_for_auth=False)`. First attempt used
the wrong kwarg name (guessed `call_for_auth`; the real one, found by
reading `schwabdev/client.py`, is `call_on_auth`) — by the time that was
sorted out the ~30-second auth-code expiry had passed, so Zander had to
redo the browser step once. Second attempt succeeded immediately.

Verified `tokens.db`'s refresh token timestamp jumped from 2026-07-04
(expired, Schwab refresh tokens last 7 days) to 2026-07-24, then ran
`schwab_quotes_pipeline.py` live end-to-end: 45 symbols (DJI + sector ETFs)
fetched successfully with real quote/PE/dividend data. The script then
crashed — but *after* the parquet write, on a bare Unicode arrow (`→`) in
the trailing print statement, which Windows' cp1252 console can't encode.
This is the exact "ASCII-only CLI output" gotcha documented in `CLAUDE.md`;
this script (and, checked via grep, all 4 other Schwab pipelines) predates
that convention because none of them were ever runnable before without
valid tokens, so the bug never surfaced. Fixed all 5 (`→` -> `->`).

Reran `curated.py`: 143 tables (up from 142) — `schwab_quotes` now populated
and verified queryable through `query.py` (45 rows, real AAPL/AMZN/etc.
prices and PE ratios). Committed the 5 pipeline fixes. `PROJECT_NOTES.md`
updated in place: item 2 of the original roadmap marked done, and the
Storage section's note about the Schwab price-history backfill being
"blocked on OAuth" corrected — it's now unblocked (just not started).

Of the original 5-item roadmap, only item 3 (AV earnings backfill, quota-
gated) remains. Two not-yet-started follow-on items are now open: the
PatentsView rewrite (found earlier this session) and the Schwab full
price-history backfill (newly unblocked by this OAuth fix).

## AV earnings backfill pacing started (2026-07-24, same session continued)

Zander asked to start pacing item 3. It was ~22:00 ET, well outside the
"~10:05-10:30am safe window" noted in the constraints table for spending
this repo's share of the AV quota before `custom_index_tool`'s TranscriptPull
grabs it. Rather than assume the window had passed and the day's quota was
gone, tested with a single live probe request (`EARNINGS` for AAPL) — it
returned real data, no quota-exhaustion `Note`/`Information` field. So the
10:05-10:30 guidance is apparently about avoiding *contention* with
TranscriptPull, not a hard proof that quota resets right before then; quota
was still available hours later.

Ran `alpha_vantage_fundamentals_pipeline.py` (default incremental mode, no
flags) rather than writing a one-off earnings-only script — the pipeline
already has a rotating-subset pacing mechanism built in
(`INCREMENTAL_EARNINGS_N=7`/day via day-of-year rotation) designed for
exactly this. Used the existing mechanism instead of duplicating it.
Consumed the full 20-request default budget: 7 company overviews, earnings
history for GOOGL/AXP/AMGN/AMZN/AAPL/BA/CAT (1,017 records total), a full
3-month earnings calendar (4,885 upcoming dates across the whole market,
not just DJI), dividends for 2 symbols, insider transactions for 2 symbols.
`news_sentiment` failed with an "Invalid inputs" error from the AV API,
burning 1 request for nothing — not investigated, since the active
`sentiment` factor already runs on local VADER, not this endpoint (see
`CLAUDE.md`'s Open Work section from 2026-07-06).

Confirmed neither this repo's run nor `custom_index_tool`'s exhausted the
other's share today; not clear if there's real per-key headroom beyond 25
or if `custom_index_tool` simply hadn't run yet tonight — didn't dig further
since it wasn't necessary to complete the task.

Reran `curated.py`: 148 tables (up from 143). Verified both target tables
directly: `alpha_vantage_earnings` — 1,017 rows across the 7 fetched
tickers (152 each for AAPL/AMGN/AXP/BA/CAT, 146 AMZN, 111 GOOGL — the
`ticker` column, not `symbol`, caught that on the first check). 
`alpha_vantage_earnings_calendar` — 4,885 rows, real upcoming report dates/
estimates. `PROJECT_NOTES.md` updated: item 3 now "in progress" instead of
"not yet run," with the specifics of this batch and a note that ~5 more
daily runs will complete the 30-symbol DJI universe for earnings (other
sections rotate on their own independent schedules).

All 5 items of the original roadmap have now been started; item 3 needs a
few more days of unattended manual runs to finish. Follow-on items still
open: PatentsView rewrite, Schwab price-history backfill.
