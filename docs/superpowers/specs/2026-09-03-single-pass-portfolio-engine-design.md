# Single-Pass Portfolio Engine — Design Spec

**Date:** 2026-09-03
**Status:** Implemented (commit `f42c7c4`), per Zander's answer to Open Question 4
("implement now"). See "Implementation notes" at the end for how the other
three open questions were resolved.
**Session notes:** `work-notes/financial-data-pipeline/SESSION_NOTES_2026-09-03.md`,
`work-notes/financial-data-pipeline/TASKS.md` ("Backtesting Engine Improvements" →
"Inverse-vol sizing / portfolio allocation")

## Purpose

`evaluation/trades.py`'s `_portfolio_pass()` — the code that applies
`PortfolioLimits.capital`/`max_concurrent` and `Sizing.mode in
("fixed_fraction", "inverse_vol")` — is admission FILTERING over a fixed,
pre-computed candidate-trade list, not a true portfolio simulation. Its own
docstring has said so since it was built (W1 Step B, `28e0934`'s
`_portfolio_pass` docstring, quoted in full below): a trade rejected for lack
of capital does not free its symbol to take a different trade it would have
taken in a real single-pass sim. This spec designs the fix that docstring
deferred: *"that would need restructuring `simulate_symbol`/`_portfolio_pass`
into one interleaved pass and is a separate, larger piece of work."*

This is now the last unscoped piece of the inverse-vol/portfolio-allocation
TASKS.md thread. The skfolio/Riskfolio-Lib dependency question is settled
(NO-GO on both, `experiments/2026-09-03_skfolio-vs-riskfolio-vetting.md`) and
the hand-rolled HRP weight calculator is built (`evaluation/hrp.py`,
`588404e`) but deliberately not wired into `Sizing.mode` — sizing a SET of
concurrently-held names from one HRP call needs exactly this rework to have
anywhere correct to plug into. This spec is that plug point.

## Motivating findings

### The exact mechanism of the bug, read from the current code

`simulate_symbol()` (`evaluation/trades.py:122-222`) generates a symbol's
ENTIRE candidate-trade list in one forward walk, using a `next_free` cursor:
starting from the first entry signal, it finds the paired exit (via
`_find_exit`'s rule/stop/target/time precedence), records the trade, and
resumes searching for the NEXT entry signal starting at `exit_i + 1`. This
walk has no knowledge of portfolio capital or concurrency — it assumes every
candidate it finds will actually be taken.

`_portfolio_pass()` (`evaluation/trades.py:225-295`) then sorts ALL symbols'
candidates by `(entry_date, symbol)` and walks them once, admitting a
candidate only if capital and `max_concurrent` allow, releasing capital when
open positions' `exit_date` is reached. A REJECTED candidate is simply
dropped from the output — but critically, `simulate_symbol` had already
decided, at generation time, that this symbol's NEXT entry search would not
start until the rejected trade's (never-actually-taken) exit. If a different
entry signal fired for that symbol before that phantom exit date, it was
never generated as a candidate at all — not rejected, never considered.

**Concrete case the current code cannot handle**: symbol A signals entries at
day 1 and day 5, with day 1's trade's natural exit at day 20. `simulate_symbol`
generates exactly ONE candidate for A (day 1 → day 20) and, having consumed
the day-5 signal as "already in a position," never generates a second
candidate starting at day 5. If the portfolio pass then rejects the day-1
candidate for lack of capital, symbol A contributes ZERO trades — even though
a true single-pass sim, having rejected day 1, would immediately be free to
consider day 5 as a fresh entry.

### Who is actually affected today (checked, not assumed)

- **The live, pre-registered TV catalog campaign (`strategies/stage3.py`) does
  NOT set `PortfolioLimits` or a non-default `Sizing` at all** — grepped, zero
  matches. `cost_config()` only builds a `CostModel`. `needs_portfolio` (the
  gate in `trades.simulate()`, line 317-319) is therefore always `False` for
  every campaign run to date, and `_portfolio_pass` has never executed as part
  of a pre-registered result. **This is lower-risk than it might look —
  nothing pre-registered needs a protocol amendment for this fix.**
- **`backtest_app.py`'s interactive execution-config panel** is the one live
  surface that exercises `PortfolioLimits`/non-default `Sizing` today (its own
  module docstring: "DIAGNOSIS ONLY — nothing here writes to the registry").
  A user dragging the capital/concurrency/sizing sliders there will see
  different (more correct) numbers once this ships.
- **`stats.permutation_trades`** already refuses to use its
  multi-`workers` parallel path "when the config requires the capital/
  concurrency portfolio pass (that pass couples symbols, so the classic loop
  always handles it)" (`evaluation/stats.py:259-261`). This constraint was
  already necessary before this spec and remains necessary after — a
  single-pass engine couples symbols even more explicitly than a filter does.
  No new constraint, just confirms the existing one stays load-bearing.
- **`tests/test_execution_step_b.py::TestPortfolioLimits`** exercises
  rejection (`test_max_concurrent_rejects_the_second_overlapping_trade`,
  `test_capital_budget_rejects_unaffordable_trade`) and release-timing
  (`test_capital_frees_up_after_exit`), but none of its fixtures contain a
  SECOND entry signal on the same symbol after a rejected trade's real entry
  — i.e., none of them currently probe the exact gap this spec closes. **The
  existing suite is not expected to need any assertion changes**; new tests
  are needed to demonstrate the fix, not to re-certify the old behavior.

## Approach

**Chosen: lazy per-symbol candidate generation, merged across symbols by a
chronological min-heap, admission decided at generation time.**

The key reframing: today's bug isn't really about the ADMISSION step, it's
that TRADE GENERATION happens too early, before admission is known. The fix
is not a full rewrite of exit-timing logic (which is correct and stays
untouched — `_find_exit`'s look-ahead-safe rule/stop/target/time precedence
is unaffected by portfolio state and does not need to change) — it's making
candidate generation resumable, so that a rejection can restart the search
immediately after the rejected candidate's own entry signal instead of after
its phantom exit.

Rejected alternatives:

- **Full day-stepped simulation** — iterate the union of every symbol's
  trading calendar one day at a time, updating every symbol's flat/in-position
  state each day. Conceptually the simplest "true" single-pass design, but
  O(total_days × total_symbols). Wrong tool for this repo specifically:
  CLAUDE.md already documents `event_backtest`'s wide-universe scaling problem
  (~22,000 queries for a 2,935-symbol study) as a real, previously-hit cost;
  a day-stepped rewrite of the discrete-trade engine would reintroduce the
  same class of problem for the TV catalog's 2,100+-symbol universe. Rejected
  on complexity grounds alone, before even reaching correctness.
- **Iterative two-pass "retry/backfill"** — keep `_portfolio_pass` as a filter,
  but after it runs, re-simulate any symbol that had a rejection starting from
  the rejection point, repeat until nothing changes. Converges to the same
  answer as the chosen approach but via a fixed-point loop with a fuzzier
  termination story and two separate code paths to keep in sync (candidate
  generation logic would still need to exist in two forms: the original
  whole-list form and a resumable retry form). Rejected for clarity: one
  event loop with an obvious termination (heap empty) beats an iterate-until-
  stable loop with the same asymptotic cost and more surface for a subtle
  infinite-loop or double-counting bug.

## Architecture

### Data structures

- A per-symbol **cursor**: the earliest index from which to search for that
  symbol's NEXT entry signal. Starts at 0 for every symbol.
- A **min-heap** of pending candidates, keyed `(entry_date, symbol)` — same
  tie-break convention `_portfolio_pass` already uses (chronological, then
  alphabetical), preserved rather than reconsidered so this change is legible
  as "the same ordering, applied earlier" and not a second unrelated decision
  bundled into one spec.
- **Portfolio state**: `equity`, `open_positions` (list of `(exit_date,
  committed, pnl_dollars)`) — identical shape to today's `_portfolio_pass`
  locals, reused verbatim.

### The loop

```
for each symbol with data:
    push_next_candidate(symbol, cursor=0)     # may push nothing (no more signals)

while heap is not empty:
    candidate = heap.pop()                     # earliest (entry_date, symbol)

    release all open_positions with exit_date <= candidate.entry_date,
        crediting pnl_dollars to equity          # unchanged from _portfolio_pass

    if max_concurrent set and len(open_positions) >= max_concurrent:
        REJECTED
    else:
        size = compute_size(candidate, sizing, equity)   # unchanged from
                                                           # _portfolio_pass's
                                                           # per-mode branches
        if size is None (e.g. no reliable entry_vol_pct) or size <= 0:
            REJECTED
        elif capital is not None and committed_now + size > capital:
            REJECTED
        else:
            ADMIT: record trade (re-denominated pnl_dollars if sizing mode
                   requires it, same as today), open_positions.append(...)

    if ADMITTED:
        push_next_candidate(symbol, cursor=candidate.exit_index + 1)
    if REJECTED:
        push_next_candidate(symbol, cursor=candidate.entry_signal_index + 1)
```

`push_next_candidate(symbol, cursor)` is `simulate_symbol`'s existing entry-
signal-then-`_find_exit` logic, UNCHANGED, just called to produce ONE trade
starting the search at `cursor` instead of walking the whole symbol. If it
finds nothing (no more entry signals, or the found entry/exit falls off the
end of the data), it pushes nothing and that symbol is done contributing to
the heap.

**This is the entire fix.** The REJECTED branch's `cursor=candidate.
entry_signal_index + 1` (not `+exit_index+1`) is the one-line semantic change
that closes the gap described in Motivating Findings — everything else is
the existing logic, relocated to run lazily instead of eagerly.

### Complexity

Same asymptotic order as today: each symbol's trading history is walked once
per REALIZED-OR-REJECTED candidate it produces, same as `simulate_symbol`
walks it once per realized trade today (rejections are the only new "extra"
work, and only occur for symbols that actually breach a capital/concurrency
limit — for an unconstrained config nothing is ever rejected and the walk
count is identical to today). No new O(days × symbols) term.

### `needs_portfolio=False` stays untouched

The existing gate in `simulate()` — `capital is None AND max_concurrent is
None AND sizing.mode == "fixed_notional"` — continues to bypass the portfolio
machinery ENTIRELY, calling `simulate_symbol`'s whole-list form directly with
zero change. This is not merely an optimization: it is what keeps `LEGACY`
and every unconstrained caller (**including the entire live campaign, per
the Motivating Findings above**) bit-for-bit unchanged. The new heap-based
loop is reachable ONLY when a caller has opted into `PortfolioLimits`/
non-`fixed_notional` `Sizing` — an already-deliberate, already-opt-in choice.

## Interaction with other in-flight pieces

- **`evaluation/hrp.py`'s eventual `Sizing.mode="hrp"`** (scoped, not yet
  built — see TASKS.md) sizes a SET of concurrently-held names from one HRP
  call, which only makes sense once there is a real, ordered admission
  sequence with a live open-positions set at every decision point — exactly
  what this spec produces and today's filter-only pass does not. This spec is
  the prerequisite, not a parallel piece of work.
- **`stats.permutation_trades`'s `workers>0` parallel path** stays disabled
  whenever `needs_portfolio` is true, as it already is today (see Motivating
  Findings) — no change needed there, just confirmed still correct.

## Test strategy

**Existing suite**: expected to pass with zero assertion edits (see Motivating
Findings — no existing fixture probes the specific gap this closes).

**New tests, `tests/test_execution_step_b.py::TestPortfolioLimits` or a new
`TestSinglePassPortfolio` class**:

- **The exact motivating case**: symbol A signals at day 1 (exit at day 20)
  and day 5 (exit at day 10); symbol B alone consumes all capital via a
  concurrent candidate. Assert that under the OLD filter-only behavior A
  contributes 0 trades (documents the bug being fixed — this assertion
  should be written against `_portfolio_pass` directly, kept as a regression
  marker of the historical behavior, not deleted) and under the NEW engine A
  contributes the day-5 trade once day-1 is rejected.
- **Rejection does not reorder admitted trades**: a config where nothing gets
  rejected must produce IDENTICAL output (same rows, same order) between the
  old filter-only pass and the new lazy engine — this is the equivalence
  bound for the unconstrained-within-limits case, and should be checked as
  an exact `assert_frame_equal`, not `approx`.
- **`max_concurrent` and `capital` regression** (existing behavior, re-asserted
  against the new engine): the current `TestPortfolioLimits` tests, re-run
  against the new code path, must produce the same admit/reject counts they
  do today — these tests don't probe the gap, so they should be usable as-is
  against the new engine as an equivalence check, not just left alone.
- **`fixed_fraction`/`inverse_vol` sizing correctness carries over unchanged**:
  `entry_vol_pct` and the size formulas are untouched by this spec; a test
  confirming a resumed (post-rejection) candidate still sizes correctly off
  current `equity` closes the loop on the one place state threads through
  the rewrite.
- **`permutation_trades` with `PortfolioLimits` set**: fixed-seed run before/
  after this change should differ (the whole point) but stay internally
  consistent (same seed → same result on repeat runs of the NEW code).

**Real-data check** (this repo's standing discipline this session — every new
feature verified against production data, not just synthetic fixtures): run
`ev_trades.simulate()` with `PortfolioLimits(capital=..., max_concurrent=...)`
against a real multi-symbol basket (e.g. the 8-10 symbol universes used
throughout this session's other verifications) before/after, and manually
inspect at least one case where a rejected trade's symbol produces a
different downstream trade — confirm it's a real, sensible signal-driven
entry, not an artifact.

## Migration order

1. Land the new engine as a SEPARATE function (e.g. `_portfolio_pass_v2` or
   inline in a new `_simulate_portfolio_constrained`), not a rewrite-in-place
   of `_portfolio_pass`, so the old function stays available for the
   regression-marker test above and for an easy revert if something is wrong.
2. Wire `simulate()`'s `needs_portfolio` branch to call the new engine.
3. Run the full equivalence + regression test set (above). Full suite must
   stay green with zero unrelated assertion edits.
4. Real-data check (above).
5. Once confirmed, either delete `_portfolio_pass` (if the regression-marker
   test is judged not worth keeping the old code alive for) or leave it
   as dead code behind the marker test, whichever Zander prefers — see Open
   Questions.
6. Update `_portfolio_pass`'s (or its replacement's) docstring to remove the
   "APPROXIMATION" caveat and describe the new, corrected semantics; update
   TASKS.md.

## Out of scope

- `Sizing.mode="hrp"` itself (separate spec/task, this is its prerequisite).
- Changing the `(entry_date, symbol)` tie-break convention for same-day
  candidates — preserved as-is.
- `backtest.py`'s weight-matrix engine — unaffected, it has no discrete-trade
  admission concept.
- Any change to `_find_exit`'s look-ahead-safe exit-timing logic — untouched
  by this spec, reused as-is.
- Re-registering or amending the TV catalog campaign's pre-registration —
  not needed, since the campaign never exercises this code path today
  (Motivating Findings).

## Exit criteria

Full suite green with zero unrelated assertion edits; the motivating-case
test present and passing; the old-behavior regression marker present (so a
future reader can see what was fixed and why, matching this repo's own
"belongs in the docstring, not a later bug report" precedent); real-data
check done and documented in work-notes; TASKS.md updated.

## Open questions for Zander

1. **Keep `_portfolio_pass` (old filter-only behavior) alive as dead code
   behind a regression-marker test, or delete it once the new engine is
   verified equivalent-where-it-should-be?** Recommendation: keep it, cheap
   insurance and documents the bug's shape for anyone who finds this spec
   later — but it's a real ongoing-maintenance-surface tradeoff worth a call.
2. **Does this need a `docs/PIPELINE_CATALOG.md`/`CLAUDE.md` mention**, given
   it changes observable behavior for anyone using `backtest_app.py`'s
   capital/concurrency sliders (diagnosis-only, but still a real behavior
   change a returning user could notice)? Recommendation: yes, one line in
   `backtest_app.py`'s module docstring or the W4 spec's changelog, not a
   big deal, just don't want it discovered as a silent surprise.
3. **Should the campaign's Stage 3 batch actually start USING
   `PortfolioLimits`/`Sizing` now that they'd be simulated correctly** — i.e.,
   is this spec also an invitation to reconsider whether `stage3.py` should
   model capital constraints at all, or is that a separate research/strategy
   decision entirely outside this spec's scope? Recommendation: separate
   decision, flagged here only so it doesn't get conflated with "the engine
   is now correct, therefore we should turn this on" — correctness and
   adoption are different questions.
4. **Timing**: this is scoped as a single, mechanically well-defined change
   (one new function, one cursor-advancement rule), but it touches the exact
   engine backing every discrete-trade evaluation in the repo. Implement now,
   in a dedicated session with nothing else changing at the same time, or
   defer until there's an actual near-term consumer (the HRP sizing mode)
   that needs it?

## Implementation notes (2026-09-03, commit `f42c7c4`)

Zander answered Open Question 4 directly: implement now. The other three
were resolved as part of implementation, each documented in the commit:

1. **Keep vs. delete `_portfolio_pass`**: kept, per the doc's own
   recommendation. It is no longer called by `simulate()` or
   `permutation_trades()` (both now go through `_simulate_single_pass`), but
   stays in `evaluation/trades.py` as a regression marker --
   `tests/test_execution_step_b.py::TestSinglePassPortfolio::
   test_old_engine_shows_the_bug` runs it directly against the motivating
   fixture and asserts it STILL shows the old, buggy behavior, so the fix is
   legible as a fix (matching this repo's "belongs in the docstring, not a
   later bug report" precedent).
2. **User-facing note**: added to `backtest_app.py`'s module docstring,
   dated, naming exactly which two config fields are affected.
3. **Should `stage3.py` start using these limits**: not touched. Explicitly
   out of scope, same as the spec said -- correctness and adoption are
   different decisions.

**One thing the design phase did not anticipate**: `evaluation/stats.py`'s
`permutation_trades()` calls `tr._portfolio_pass` directly (a private
cross-module call, found only once implementation started grepping real
call sites) for its portfolio-constrained null. Left unfixed, the observed
run would have used the corrected engine while the null still used the
buggy one -- an invalid comparison, not a smaller version of the same fix.
Updated in the same commit; see `stats.py`'s `_perm_needs_portfolio` branch.

**Verification**: full suite 3082 passed / 1 skipped, zero unrelated
assertion edits. Real-data check on a real 10-symbol SMA(10/50) crossover
basket (2018-2025, capital=$50k/max_concurrent=2): MSFT gains one genuine
extra trade (2020-12-17 → 2021-01-19) the old engine silently dropped;
every other trade (53 of 54) is byte-identical between the two engines.
