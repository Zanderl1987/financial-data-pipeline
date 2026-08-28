# Execution Engine Unification (W1) — Design Spec

**Date:** 2026-08-16
**Status:** Awaiting review (brainstorming session, Claude Opus 5)
**Session notes:** `work-notes/financial-data-pipeline/SESSION_NOTES_2026-08-16.md`

## Purpose

Give the repo one place where execution semantics — transaction costs, stops, position
sizing, portfolio capital limits — are defined, and make the rule-based trade engine
model them at all. Today `evaluation/trades.py` models none of them, and it is the engine
the TV strategy catalog campaign's primary endpoint runs on.

This is the work `2026-07-18-unified-eval-framework-design.md` deferred: its "Out of scope
(v1), recorded as TODOs" list opens with *"transaction costs/slippage/borrow;
capital-constrained compounding equity curves."*

W1 is the first of five workstreams (W1 engine → W2 robustness battery → W3 tearsheet →
W4 interactive layer → W5 optimizer). Only W1 is specced here.

## Motivating findings

Three engines, non-overlapping capabilities, measured by reading them 2026-08-16:

| | cost model | stops/targets | sizing | capital constraint |
|---|---|---|---|---|
| `backtest.py` (weight matrix, vectorized) | yes — turnover x rate, borrow fee, sqrt impact | n/a | vol target, max_weight | drawdown breaker |
| `event_backtest.scenario()` (per-event loop) | yes — same rate math | stop/target/"ATR" | no | no |
| `evaluation/trades.py` (per-symbol loop) | **none** | **none** | fixed notional only | **none** |

The duplication is literal: `(cost_bps + spread_bps/2.0)/1e4` appears at `backtest.py:198`
and again at `event_backtest.py:346`, independently written, and is absent from
`trades.py`.

**The campaign's costs come from a runtime monkeypatch.** `strategies/stage3.py:169-190`
swaps `evaluation.trades.simulate_symbol` inside a context manager. Per its own docstring
this works only because both `trades.simulate()` and `stats.permutation_trades()` resolve
that name as a late-bound module global. If either call site is ever refactored to bind
earlier, the patch silently stops applying and the primary endpoint goes gross-of-cost
without erroring. Migrating stage3 off it is therefore mandatory in W1, not optional.

## Constraint: the campaign is live and pre-registered

`experiments/2026-08-11_tv-strategy-catalog-preregistration.md` is in force; 5 strategies
are admitted to Stage 2. Any result produced under an amended protocol must be reported as
amended.

**Resolution (Zander's call, 2026-08-16):** one code path, behavior versioned by an
explicit config object. The campaign pins a named config reproducing today's numbers
bit-for-bit, so no amendment is needed now and it can migrate later on its own schedule.

## Approach

**Chosen: B via A.** Step A extracts a shared cost/risk/sizing core behind a frozen
`ExecutionConfig`, with zero behavior change. Step B collapses the rule-based trade path
onto one simulator with costs, stops, sizing, and portfolio limits.

Rejected:

- **A alone** — deduplicates the math but leaves three orchestrating loops free to drift
  apart again.
- **C, full event-driven rewrite** (LEAN/Nautilus style: order objects, portfolio object,
  fill and latency models). Best long-term ceiling and the natural bridge to live Schwab
  execution, but months of work and a near-total rewrite of the 62KB `test_evaluation.py`,
  for daily-bar US equities with no L2 data to feed it.

### Scope correction made during design

`scenario()` does **not** become an adapter onto the trade simulator. Section 2 of the
brainstorm proposed that before its body had been read; it does not hold. `scenario()`
computes P&L from `res.car` — the benchmark-relative cumulative abnormal return path from
`event_study()` — exits on a relative day offset inside a fixed `holding_days` window,
applies cost to the return (`net = gross - effective_cost * 2`), and lets every event
become a trade with no one-position-per-symbol rule. `trades.py` is raw close-to-close,
unbounded horizon, rule-driven exit, one position per symbol, and drops unclosed
positions. Folding them together would silently change every existing event-study result.

`scenario()` therefore shares the primitives (cost rate, stop evaluation) and keeps its
own semantics. **W1 delivers one shared execution core and one unified rule-based trade
engine — not one engine overall.**

## Architecture

New module `evaluation/execution.py`:

```python
@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 0.0      # per side
    spread_bps: float = 0.0          # full spread; half charged per side
    borrow_fee_bps: float = 0.0      # annualized, on short exposure
    impact_model: str | None = None  # None | "sqrt" | "flat" (see note below)
    impact_coeff: float = 0.0        # sqrt: backtest.py's adv_impact_coeff (default 0.1)
                                     # flat: bps added per round trip (scenario() uses 10.0)

@dataclass(frozen=True)
class RiskControls:
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    vol_stop_mult: float | None = None   # see naming note below
    trailing: bool = False
    max_holding_days: int | None = None

@dataclass(frozen=True)
class Sizing:
    mode: str = "fixed_notional"     # fixed_notional | fixed_fraction
    notional: float = 10_000.0
    max_weight: float | None = None

@dataclass(frozen=True)
class PortfolioLimits:
    capital: float | None = None            # None = unlimited (today's behavior)
    max_concurrent: int | None = None
    max_drawdown_stop: float | None = None

@dataclass(frozen=True)
class ExecutionConfig:
    name: str = "default"
    costs: CostModel = field(default_factory=CostModel)
    risk: RiskControls = field(default_factory=RiskControls)
    sizing: Sizing = field(default_factory=Sizing)
    limits: PortfolioLimits = field(default_factory=PortfolioLimits)
```

**Which fields apply to which engine.** The config spans both engines, so not every field
is meaningful everywhere. Applying a field to an engine that ignores it must raise at
construction rather than silently no-op:

| Field | `trades.py` (discrete) | `backtest.py` (weights) |
|---|---|---|
| `costs.*` | yes (per-trade round trip) | yes (per-day turnover) |
| `risk.stop_loss_pct` / `take_profit_pct` / `vol_stop_mult` / `max_holding_days` | yes | no |
| `sizing.mode` / `notional` | yes | no |
| `sizing.max_weight` | no | yes |
| `limits.capital` / `max_concurrent` | yes | no |
| `limits.max_drawdown_stop` | no | yes |

Grouped rather than ~14 flat fields because W4's sliders map onto exactly these groups.
Frozen so a config can be hashed into the registry beside `universe_hash` — every result
then carries the execution semantics that produced it, which is implicit today.

**Presets:**

- `LEGACY` — all defaults. Reproduces today's `trades.py` bit-for-bit.
- `TV_CAMPAIGN` — `commission_bps=10.0`, else legacy. Reproduces `stage3.PRIMARY_COST_BPS`.

**Shared functions (only these two are genuinely shared):**

```python
def round_trip_rate(costs: CostModel) -> float
def daily_cost(costs, turnover, short_exposure, ann) -> pd.Series
```

The campaign charges a fixed round-trip deduction per trade; `backtest.py` charges
turnover x rate per day. Those are different *application points* over the same rate. The
core unifies the rate computation, not the application. Claiming more would oversell it.

**`"sqrt_impact"` means two different things today — found during spec self-review.**
Both engines accept `slippage_model="sqrt_impact"`, but:

- `backtest.py:207-209` applies `turnover.pow(0.5) * (adv_impact_coeff / 1e4)` — an actual
  square-root impact function of turnover, coefficient-scaled.
- `event_backtest.py:347-348` applies `effective_cost += 0.0010` — **a flat 10 bps
  penalty, with no square root and no coefficient.**

One name, two unrelated models. `round_trip_rate()` therefore cannot absorb `scenario()`'s
version by computing the `backtest.py` formula; doing so would change every event-scenario
result that passes `slippage_model`. W1 preserves both behaviors exactly and names them
apart in the config (`impact_model="sqrt"` for the real one, `impact_model="flat"` with
`impact_coeff=10.0` reproducing `scenario()`'s constant). Deciding whether the flat model
should exist at all is a research question, not a refactor, and is left to W2.

Also preserve: `backtest.py`'s `adv_impact_coeff` defaults to `0.1`, not `0.0`. The shim
must carry that default through, or every existing `sqrt_impact` backtest changes.

**Naming note.** `scenario()`'s `atr_stop_mult` is not ATR: `event_backtest.py:364`
computes `window_px.diff().abs().mean()`, the mean absolute close-to-close change over 14
days. True range requires highs, lows, and the prior close. The measure is a reasonable
close-only volatility proxy; only the name is wrong. The shared field is therefore
`vol_stop_mult`. W1 does **not** change the math (that would move existing results); it
corrects `scenario()`'s docstring and keeps `atr_stop_mult` as a deprecated alias.

## Step A — extraction, zero behavior change

1. Add `evaluation/execution.py`.
2. `backtest.py` — replace the inline cost block (`:196-209`) with `daily_cost()`. Existing
   flat kwargs stay as a shim building a `CostModel`; no caller changes.
3. `event_backtest.scenario()` — replace `:346` with `round_trip_rate()`. Same shim.
4. `strategies/stage3.py` — `cost_adjusted()` keeps its signature and behavior but computes
   its rate from `round_trip_rate(CostModel(commission_bps=bps))`. **The monkeypatch
   survives Step A**; it is removed in Step B once a config parameter can replace it.

Load-bearing seam: `stats.permutation_trades` re-enters the engine at `simulate_symbol`.
Step A must not change that signature.

## Step B — the discrete-trade simulator

### Signature compatibility

Two callers constrain the signature:

- `stats.permutation_trades:210` calls `simulate_symbol` with **8 positional args**.
- `stage3.cost_adjusted:176-178` does `sig.bind(*args, **kwargs)` and reads
  `bound.arguments["notional"]`, so a parameter *named* `notional` must still exist.

Both survive if the config is appended as an optional keyword-only parameter:

```python
def simulate_symbol(index, close, long_entry, long_exit, short_entry, short_exit,
                    symbol, notional, *, config=None):   # None => LEGACY
```

Extended, never changed.

### Inside `simulate_symbol` (all no-ops under LEGACY)

1. **Costs** — subtract `round_trip_rate(config.costs)` from realized `pct`.
2. **Stops / targets** — scan closes between `entry_i` and the rule's exit; exit early if
   triggered.
3. **`max_holding_days`** — cap the search window.
4. **`exit_reason` column** — `"rule" | "stop" | "target" | "time"`.

Item 4 is **the one deliberate observable change in Step B**: `TRADE_COLS` grows by one.
Values are unchanged under `LEGACY` (always `"rule"`), but tests asserting exact column
lists need editing. Step A's "no assertion edits" contract does not extend to Step B.

### Inside `simulate()` — portfolio pass

`PortfolioLimits` and `Sizing.fixed_fraction` cannot be decided per-symbol. `simulate()`
generates candidate trades per symbol as today, then walks them in chronological entry
order maintaining committed capital and open-position count, admitting a trade only if
`capital` and `max_concurrent` allow and releasing on exit.

Under `LEGACY` the pass is **skipped entirely** — not run-with-no-effect — so no float
drift is possible.

**Documented limitation:** this is admission-filtering layered on per-symbol candidates,
not a true single-pass portfolio simulation. The per-symbol "one position at a time, an
unclosed position blocks later entries" rule resolves first. A trade rejected for capital
does not free that symbol to take a different trade it would have taken in a true
portfolio sim. This belongs in the docstring, not in a later bug report.

### The rounding trap in the stage3 migration

Today's monkeypatch computes:

```python
r["pnl_dollars"] = round(round(notional * pct, 2) - cost_dollars, 2)
```

Cost is subtracted **after** the engine already rounded to cents. The natural in-engine
implementation, `round(notional * (pct - rate), 2)`, rounds once and can differ by a cent
per trade. Across thousands of trades that moves `total_pnl_net`, and `pnl_p` compares
that total against permuted totals — so it can move the campaign's primary endpoint.

**The migration must reproduce the existing order of operations exactly** (round, deduct,
round), even though rounding once is cleaner. Reproducibility beats cleanliness here.

## Test strategy

### Step A gate (all must pass before Step B begins)

| Check | Assertion |
|---|---|
| Golden masters | `BacktestResult.returns`/`.metrics`, `scenario()` trade frames, `stage3` per-strategy `pnl_p` on fixed seeds — **exact equality**, not `approx` |
| Existing suite | 762 pass with **zero assertion edits**. A test needing a new expected value means something broke |
| Campaign re-run | 5 admitted Stage-2 strategies: `total_pnl_net`, `pnl_p`, `cost_fragile` identical |
| Impact-model coverage | Golden masters must include a `slippage_model="sqrt_impact"` run through **both** engines, since the two implement different formulas under that one name (see Architecture). Also a `borrow_fee_bps > 0` short-exposure run, the only path exercising `daily_cost`'s third argument |

### Step B tests

**Equivalence:**

- `simulate(rule, cache)` == `simulate(rule, cache, config=LEGACY)` == Step A golden
  master, across synthetic rules covering long/short/both, no-exit-before-end, empty cache.
- `permutation_trades` with a fixed seed yields identical `pnl_p`/`win_rate_p` before and
  after — its RNG draw order must not shift.
- **stage3 monkeypatch vs config, bit-identical on all 5 admitted strategies.** This gates
  deletion of `cost_adjusted()`; both paths run in the same test until it passes.

**Look-ahead regression (the most dangerous new surface):** a stop observed on day *t*'s
close must execute at *t+1*'s close, exactly like a rule exit. Exiting at the stop day's
own close is look-ahead — the bug class that has already bitten twice here (`oil_shock`'s
`entry_lag`; the vol-target and circuit-breaker same-day bugs fixed in `35c60e3`).
Hand-built price path where the two differ, asserting the later exit price.

**New capability:** stop fires before rule exit; target fires before stop;
`max_holding_days` caps a longer trade; a capital-constrained trade is rejected while an
earlier one is open and admitted after it closes; `max_concurrent` caps simultaneous
positions; `fixed_fraction` compounds off realized equity.

## Migration order

1. Land Step A. Gate above green.
2. Add `config` params (`simulate_symbol`, `simulate`, `permutation_trades`), defaults
   `None`, no call-site changes. Suite stays green.
3. Implement the config branches. Equivalence tests green.
4. Switch stage3 to `TV_CAMPAIGN` **with the monkeypatch still present**; run both; assert
   identical.
5. Delete `cost_adjusted()` and its now-unused `inspect` / `contextmanager` imports.
6. Record `ExecutionConfig` in the registry alongside `universe_hash`.

Steps 1-3 are additive and revert cleanly. Step 5 is the only irreversible one and is
gated on step 4.

## Out of scope

Order types, intrabar fills, multi-timeframe, live execution, vol targeting for discrete
trades (well-defined for a continuously-held weight vector, ambiguous for discrete trades;
`backtest.py` keeps its own). `scenario()` keeps its CAR-path semantics. `backtest.py`
keeps its weight-matrix path. W2-W5 are separate specs.

## Exit criteria

Full suite green; the three Step-A golden-master families exact; stage3 bit-identical and
the monkeypatch deleted; the look-ahead test present and passing; `ExecutionConfig` hashed
into registry rows.
