# Full-Universe Factor Validation (momentum / low_vol)

**Date:** 2026-07-29
**Status:** Draft for review (design finalized interactively across this session's
AskUserQuestion decisions; written up here per the brainstorming skill's process)

## Background

The unified evaluation framework (`evaluation/` + `evaluate.py`, `docs/superpowers/specs/
2026-07-18-unified-eval-framework-design.md`) has only ever been run against
`analytics.signals.signal_panel()`'s default universe — in practice the 69-symbol
`tiingo_prices` watchlist, because until this session's clone-sync the `prices` table had
no data in the clone doing the eval work. This clone now holds the real Schwab-sourced
`prices` table: **27,759 symbols**, 46.9M rows, 1970–2026.

Goal: validate whether `momentum` and `low_vol` — the only two factors with real
full-universe breadth (`value`/`quality`/`growth`/`sentiment`/`insider_flow` all need
fundamentals/short-interest/insider/sentiment data that only covers the 69-symbol
watchlist) — hold up outside the watchlist, where survivorship and selection effects are
largest.

**Survivorship bias, framed explicitly** (this was the user's core requirement driving
the whole design): a naive "run it on all 27,759 symbols" approach has two distinct bias
sources, and they are not the same problem:

1. **Look-ahead from using *today's* liquidity to judge history.** If eligibility is
   "is this symbol liquid *right now*," a stock that only became liquid last year gets
   included in its illiquid 2015 history too — a form of survivorship. **This is fixable**
   with a point-in-time trailing liquidity filter (below), and is the main thing this
   design builds.
2. **Delisting survivorship inherent to the data source.** `prices` / `symbol_universe.csv`
   are a 2026-07-24 snapshot of currently-tradable Schwab instruments. Any company that
   delisted, was acquired, or went bankrupt before that snapshot date is **entirely
   absent** from the data for its whole history — not just filtered out, never fetched.
   **This is NOT fixable** without a different data source (e.g. a CRSP-style point-in-time
   constituent history). Out of scope here. Must be stated prominently in every result,
   not glossed over as a caveat.

## Two bugs found and fixed while scoping this (prerequisites, not part of the design proper)

Investigating feasibility surfaced two real, previously-undiscovered issues — both fixed
as part of this work since the design depends on them:

1. **`feature_matrix(start=...)` correctness bug** (`analytics/features.py`): passing
   `start` queried the price table from that date directly, *before* computing rolling
   features. `mom_12_1`/`ret_252d`/`vol_21d` need 252+ trading days of trailing history,
   so any windowed call came back silently all-NaN regardless of how much real history
   existed before `start` — reproduced live with AAPL (50+ years on file, still all-NaN
   `mom_12_1` with `start='2026-06-01'`). At full-universe scale the `momentum` column
   was **entirely absent** from `signal_panel()`'s output. Fixed: pad the internal price
   query back 450 calendar days when `start` is set, compute rolling features on the
   padded panel, trim to the true `start` before the fundamentals/macro/etc. joins run.
   Verified exact match against the unwindowed baseline. Regression test added
   (`tests/test_features.py::test_start_param_does_not_truncate_rolling_window`).

2. **`_asof_fundamentals` OOMs at full-universe scale.** A fully unbounded
   `feature_matrix()` call (`symbols=None`, all blocks on) against the real 46.9M-row
   `prices` table crashes with `numpy.core._exceptions._ArrayMemoryError` — a plain
   pandas `.merge()` run once per fundamentals metric against the full panel. Not fixed
   at the source (a real refactor of a widely-used core function); worked around here
   since full-universe momentum/low_vol never needed the fundamentals/short-interest/
   insider/sentiment blocks anyway (see Architecture below). Tracked as backlog item S
   for a future proper fix (DuckDB-side join instead of pandas merge).

## Architecture

### New module: `evaluation/universe.py`

Two pure functions, both point-in-time safe, no engine changes:

```python
def exchange_listed_symbols(exclude_otc: bool = True) -> list[str]:
    """Symbols from symbol_universe.csv, optionally excluding OTC Markets /
    Nasdaq OTCBB. Static (not date-varying) -- this is a market-structure
    filter, not a liquidity filter, so it carries no look-ahead risk beyond
    the delisting-survivorship limitation already inherent to the source file."""

def point_in_time_eligible(symbols, min_dollar_volume, start=None, end=None) -> pd.DataFrame:
    """Returns (symbol, date, eligible: bool) for every trading day in range.
    eligible=True iff trailing 21-day average dollar volume (computed ONLY from
    data available as of that date -- no look-ahead) >= min_dollar_volume.
    One DuckDB query using a rolling window function, not a pandas merge --
    deliberately avoids the item-S OOM path since this can run over the full
    27,759-symbol table."""
```

`point_in_time_eligible` is the piece that actually solves bias source #1: eligibility
is recomputed per date from trailing data, so a stock's thin-liquidity years are
correctly excluded from the sample even though the same stock qualifies today.

### Additive `eligible=` parameter on `evaluation/adapters.py::from_signal_panel()`

```python
def from_signal_panel(factor="composite", symbols=None, start=None, end=None,
                      eligible: "pd.DataFrame | None" = None) -> Signal:
```

- Default `None` → byte-identical to current behavior. All 95 existing
  `test_evaluation.py` tests untouched.
- When given, `eligible` is a `(symbol, date, eligible)` frame (the direct output of
  `point_in_time_eligible`). After building the factor panel, an inner join drops
  `(symbol, date)` rows where `eligible=False` **before** the Signal is constructed —
  the engine's per-date `.groupby("date")` machinery then naturally sees a varying
  universe size day to day, which is already a supported (tested) shape.
- For full-universe calls, this function builds its own light `feature_matrix`
  (`fundamentals=False, short_interest=False, insider=False, sentiment=False`) and
  passes it to `signal_panel(fm=...)` instead of letting `signal_panel` build the
  (memory-unsafe at this scale) default heavy panel — this is the item-S workaround,
  scoped to exactly the call path that needs full breadth.

### Two new opt-in CLI flags on `evaluate.py`

```
--exclude-otc                  # apply exchange_listed_symbols() before evaluating
--min-dollar-volume N          # apply point_in_time_eligible() with this floor
```

Both default off (unchanged behavior without them). When either is passed with
`--adapter signal-panel`, `evaluate.py` builds the `eligible` frame and passes it
through to `from_signal_panel`.

### Zero changes to the tested core

`evaluation/data.py`, `ic.py`, `stats.py`, `portfolio.py`, `events.py`, `trades.py`,
`runner.py` are untouched — confirmed during initial scoping that `evaluate_ic()`,
`daily_ic()`, `quantile_spread()` all operate via `.groupby("date")` purely on whichever
`(symbol, date)` rows exist in the input panel. Point-in-time universe membership is
entirely a Signal-construction concern, upstream of the engine.

## Acceptance plan

1. Run `momentum` and `low_vol` through `evaluate.py --adapter signal-panel --exclude-otc
   --min-dollar-volume <floor>` over the full history available.
2. Compare against the existing watchlist baselines via `evaluation/registry.py::
   compare(rows, allow_universe_mismatch=True)` — this guard already exists specifically
   for comparing results across different symbol universes; no new registry code needed.
3. Report both factors' full-universe IC/spread/Sharpe alongside the watchlist numbers,
   with the delisting-survivorship limitation stated prominently in the summary, not
   buried in a footnote.
4. `--min-dollar-volume` floor: start at a conservative $1M ADV (trailing 21d) — low
   enough to include real small/mid-caps, high enough to exclude bulletin-board-adjacent
   noise. Not tuned further in this pass; revisit if results look liquidity-driven rather
   than factor-driven.

## Testing

- `tests/test_evaluation.py`: existing 95 tests must stay green (adapter default
  unchanged).
- New: `tests/test_universe.py` — `exchange_listed_symbols()` returns the expected
  exchange breakdown on `symbol_universe.csv`; `point_in_time_eligible()` correctly
  excludes a symbol during a synthetic low-volume period and includes it once volume
  rises, with no look-ahead (a stock's eligibility on date *t* must not change if data
  after *t* changes).
- Adapter test: `from_signal_panel(eligible=...)` correctly drops ineligible rows and
  is a no-op when `eligible=None`.

## Out of scope

- Delisting survivorship (stated above — needs a different data source).
- Tuning `--min-dollar-volume` beyond the initial $1M floor.
- Applying this to any factor besides momentum/low_vol (others aren't full-universe-viable
  with current data coverage).
- Fixing `_asof_fundamentals`'s OOM at the source (item S) — worked around, not fixed.
