# W3 — Reporting / tearsheet (design)

Status: design, 2026-08-17. Independent of W1/W2 (no dependency either way).
Target modules: `evaluation/tearsheet.py` (computation) + `generate_tearsheet.py` (render).

## 1. Purpose

The repo can tell you whether a result is *significant*. It cannot currently tell you what
holding it would have felt like. `backtest.py` reports five headline numbers
(`total_return_pct`, `cagr_pct`, `ann_vol_pct`, `sharpe`, `max_drawdown_pct`) and
`generate_eval_report.py` renders IC, spread, regimes and a trade scatter. Missing is the
performance-analytics layer every commercial platform ships and every practitioner reads
first: **when** the returns happened, **how long** the bad stretches lasted, and **what
the strategy adds over its benchmark**.

Five additions, matching the QuantStats/pyfolio vocabulary a reader already knows:

| Deliverable | What it answers |
|---|---|
| Monthly-returns heatmap | Is the return concentrated in a handful of months? |
| Rolling Sharpe / Sortino / vol | Was the edge present throughout, or only early? |
| Underwater plot | How deep, and how *often*, does this go under? |
| Drawdown-periods table | How long until it recovered — the number that decides whether a strategy is holdable. |
| Benchmark alpha / beta / capture | Is this a strategy, or leveraged beta with extra steps? |

## 2. Separation of concerns

`evaluation/tearsheet.py` is **pure computation** — pandas/numpy/scipy in, dicts and
DataFrames out, no plotting import. `generate_tearsheet.py` renders. This matches the
existing convention (`stats.py` and `robustness.py` compute; `generate_eval_report.py`
only ever reads artifacts and never recomputes) and it is what makes W4's interactive
layer possible without a rewrite: Dash callbacks call the same functions the static HTML
does.

House rule from `stats.py` carries over: a statistic whose assumptions fail returns `None`
plus a `*_reason` string.

## 3. The input-series problem, stated before it becomes a bug

Tearsheet analytics need a **daily return series**. Two of the three engines have one
naturally (`backtest.BacktestResult.returns`, and `event_backtest`'s CAR path). The
discrete-trade engine does not — it emits realized trades with entry/exit dates and P&L.

`daily_returns_from_trades()` bridges the gap on a **realized basis**: each trade's P&L
lands on its exit date, equity is `starting_equity + cumsum(realized P&L)`, and the series
is reindexed to a business-day calendar with 0.0 on days nothing closed.

**This is not mark-to-market, and the difference is not cosmetic.** An open position that
is 40% underwater contributes nothing to the curve until the day it closes. Realized-basis
drawdown is therefore a *lower bound* on the drawdown actually experienced, and the daily
series is spiky in a way that makes its Sharpe non-comparable to a mark-to-market Sharpe
from `backtest.py`. Both facts go in the docstring and the returned dict carries
`basis: "realized"` so a downstream consumer cannot silently compare the two. Producing a
true mark-to-market curve would require per-day position valuation the trade engine does
not currently retain — a real W4/W5 item, not something to fake here.

## 4. Method specifications

### 4.1 `monthly_returns_table(returns)`
Compounded monthly returns pivoted year × month, plus a `YTD` column compounding each
year's months. Percent units. Partial first/last months are included as-is and the
`n_months` count reports how many are real, so a 3-month backtest cannot masquerade as a
full year of evidence.

### 4.2 `rolling_metrics(returns, window=63)`
Annualized rolling Sharpe, Sortino, and volatility on a trailing `window` (63 ≈ one
quarter). Windows with zero standard deviation yield `NaN` rather than `inf` — the same
zero-sd rule the rest of the repo follows. Requires `len(returns) >= 2 * window`.

### 4.3 `drawdown_series(equity_or_returns)`
Underwater series `equity / cummax(equity) - 1`, in percent.

### 4.4 `drawdown_periods(returns, top_n=5)`
One row per drawdown episode: peak date, valley date, recovery date, depth %, days from
peak to valley, days from valley to recovery, total length. An episode still underwater at
the end of the sample gets `recovery_date = None` and `recovered = False` — **not** a
silent recovery at the last bar, which would flatter the worst drawdown in exactly the
sample where it matters most.

### 4.5 `benchmark_stats(returns, bench_returns, rf=0.0)`
OLS of strategy on benchmark over the inner-joined dates: annualized alpha, beta, R²,
correlation, tracking error, information ratio, and up/down capture. Alignment is an
inner join on date, and `n_overlap` is reported — a benchmark covering half the period
should be visible as such, not silently forward-filled.

### 4.6 `tearsheet(returns, bench_returns=None)`
Assembles headline metrics plus all of the above into one dict. Benchmark sections are
omitted (with a reason) when no benchmark is supplied.

## 5. Renderer

`generate_tearsheet.py`: self-contained HTML, embedded Plotly.js, no external requests —
the same constraints and the same palette constants as `generate_eval_report.py`
(fixed-slot categorical colors; status colors reserved for state, never series identity).
Reads a run directory's artifacts or takes a returns series programmatically.

## 6. Out of scope

- Wiring into `evaluate.py`'s automatic report stage. Additive later; not needed to land.
- Mark-to-market trade curves (see §3).
- Interactive controls — that is W4, which consumes this module.

## 6b. Two bugs found during implementation

Recorded here rather than silently patched, because both are the kind that pass every
plausible test and then misreport in production.

**`sd > 0` is not a sufficient zero-variance guard.** The repo's house rule is "never
divide by a zero/NaN sd," and every existing implementation checks `sd > 0`. That check
does not catch a *constant* float series: a series of 100 identical `0.001` values has an
arithmetically-zero sd, but in float64 it evaluates to roughly `6e-19` — positive, finite,
and enough to produce a Sharpe of **2.4e16**, which renders as a plausible-looking huge
number rather than as the degenerate input it is. `tearsheet.SD_FLOOR` (1e-12, scaled by
the series mean) is the fix, applied in `headline_metrics`, `rolling_metrics`, and
`benchmark_stats`. Worth a look at whether `stats.py` and `backtest.py` want the same
treatment — they currently use bare `> 0`, and would report the same artifact.

**Pandas column-type inference made `recovery_date` two different things.** An
all-unrecovered drawdown table keeps Python `None` in an object column; as soon as one
episode *does* recover, pandas coerces the column to `datetime64` and the `None` becomes
`NaT`. A downstream `v is None` check therefore passes every synthetic test — which tends
to have one drawdown — and renders the literal string `"NaT"` on real data, which has a
mix. Found by the real-data smoke run, not by the unit tests. Fixed on both sides: the
table now pins `datetime64[ns]` / `Int64` / `bool` dtypes so `pd.isna` works uniformly,
and the renderer uses `pd.isna`. `recovered` (a plain bool) is the column to branch on.

## 7. Exit criteria

- `evaluation/tearsheet.py` with the six functions, house-rule dicts throughout.
- Tests covering: a known-answer monthly table, the zero-sd rolling guard, an unrecovered
  final drawdown reporting `recovered=False`, beta ≈ 1 / alpha ≈ 0 when strategy *is* the
  benchmark, beta ≈ 2 on a doubled series, the realized-basis caveat holding
  (`basis == "realized"`), and every `*_reason` path.
- `generate_tearsheet.py` produces a valid self-contained HTML file with no network refs.
- Full suite green with zero edits to existing assertions.
