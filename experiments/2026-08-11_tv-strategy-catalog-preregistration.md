# Pre-registration: TradingView community strategy catalog

**Status: PRE-REGISTERED. Written 2026-08-11, before any strategy was collected,
translated, or tested.** Nothing in this document may be revised in light of a result.
Amendments are allowed but must be appended below with a date and a reason, and any
result produced under an amended protocol is reported as amended.

## Purpose

Translate a curated sample of open-source TradingView Pine strategies into this repo's
`evaluation.contracts.TradeRule` form, test each one under a fixed protocol against the
cleaned Russell 3000 price panel, and record every result — pass or fail — in the
append-only registry. The output is a catalog of strategies with honest performance
statistics, not a search for a winner.

## Why this document exists

`experiments/2026-08-08_tv-technical-rating-signal-eval.md` documents how this repo's
last TradingView investigation produced a lead (pnl_p = 0.025) that did not survive
broad-market scale on two independent data sources. Its own limitations section names
the cause: "an adaptively-chosen chain of ~10 permutation tests ... with no
multiple-comparisons correction."

This campaign will run 30-50 tests deliberately. At alpha = 0.05 that yields 2-3 false
positives by construction. The cherry-picking step (goal 5 in the original plan) is
therefore the step most likely to produce a confident, wrong answer, and the protocol
below exists to make it survivable.

## Scope of this pre-registration

Covers batch 1: **30-50 strategies**. Combination testing (original goal 6) is
explicitly **out of scope** and requires a separate pre-registration, because combining
survivors introduces a second multiple-testing layer over an already-selected set.

## 1. Sample and selection

### Source
Open-source scripts on tradingview.com, collected manually page-by-page via browser (no
bulk crawling — TradingView ToS prohibits automated collection). Published open-source
scripts are MPL 2.0 by default; author and script URL are recorded for every entry.

### Sampling frame, in priority order
1. `tradingview.com/scripts/editors-picks/` (curated for originality; open-source by definition)
2. `tradingview.com/scripts/opensource/` sorted by Most popular

**Stratification: at most 2 scripts per author.** The current popular open-source listing
is dominated by a single publisher; without this cap one author's style would define the
sample.

### Hard exclusions (applied to source code before any translation)

Screened mechanically by `strategies/screen.py`. A script hitting any of these is
recorded in the catalog with `excluded_reason` and never translated:

| Reason | Detection |
|---|---|
| Repainting via lookahead | `barmerge.lookahead_on` present |
| Repainting via unconfirmed HTF | `request.security(` on a higher timeframe without `barstate.isconfirmed` or `[1]` offset |
| Intrabar recalculation | `calc_on_every_tick=true` or `calc_on_order_fills=true` |
| No exit logic | no `strategy.close`/`strategy.exit`/exit condition; cannot form a `TradeRule` |
| Non-reproducible inputs | reads data this repo cannot supply from OHLCV |
| Visualization only | no entry condition (S/R boxes, session shading, dashboards) |

The lookahead screen is the highest-value filter in this protocol. Repainting scripts
backtest beautifully on TradingView and are pure look-ahead; they would enter the catalog
as the top performers if not excluded.

### Deprioritization (recorded, not excluded)
Title claims of extraordinary performance; parameter count > 8; mechanism duplicating an
already-admitted strategy (deduped on computed mechanism, not name).

## 2. Data

- **Price panel**: `yfinance_universe_prices` (2,285 symbols, split-adjusted at source).
  Chosen over `prices` because Schwab's API returns unadjusted OHLC — see the 2026-08-09
  follow-up in the TV rating writeup.
- **Mandatory hygiene**: `evaluation.universe.clean_symbols()` (single-day |log return| >
  ln(3) flags a symbol wholesale) and a **$5 minimum entry price** gate. Both are
  non-negotiable and pre-declared; the Russell 3000 pass without them produced a
  39,500,000% single trade.
- **Adjustment**: split-only, never dividend-adjusted, via
  `analytics.technical._split_only_adjust`. Dividend adjustment manufactures fake
  long-run uptrends in high-yield names.
- **Benchmark**: SPY.

### Splits (fixed now, before any result)

**Time.** Development sample: start of history through **2017-12-31**. Holdout:
**2018-01-01 onward** (contains the COVID crash, the 2022 bear market, and the recent
regime).

**Symbols.** A deterministic 25% of the cleaned universe is reserved as a symbol holdout,
selected by `sha256(symbol) % 4 == 0` so the split is reproducible and cannot drift.

A strategy is developed and tested only on (development period x development symbols).
The holdout is touched **once per strategy**, only after that strategy has cleared
Stage 4. Every holdout access is logged to the registry with a timestamp; a second
holdout run on the same strategy invalidates it for this campaign.

## 3. Execution and costs

- Entry and exit at the close of t+1, enforced by `evaluation/trades.py`. Rules never
  control their own execution timing.
- Notional $10,000 per trade (framework default).
- **Transaction costs: 10 bps per side (20 bps round trip), applied to every trade.**
  The existing framework models no costs at all; this campaign adds them. Costs are
  applied before the primary endpoint is computed, not as an afterthought.
- Sensitivity, reported but not used for promotion: 5 bps and 20 bps per side. A strategy
  whose verdict flips between 5 and 20 bps is flagged `cost_fragile` in the catalog.

## 4. The test battery (fixed)

Each strategy receives exactly **one primary endpoint**:

> **`pnl_p`** — the one-sided empirical p-value from
> `evaluation.stats.permutation_trades` (n_perm = 200) on **net-of-cost** total P&L,
> computed on the development sample. The null relocates the same number of entries per
> symbol to random days under the same exit rule.

One primary test per strategy means the campaign's test count equals its strategy count,
which keeps the correction tractable and honest.

The following are **descriptive only** and never trigger a promotion: win rate,
`win_rate_p`, profit factor, Sharpe, max drawdown, turnover, median hold, trade count,
per-side splits, regime conditioning. They are recorded for the catalog and for
interpretation.

**No test may be added to this battery mid-campaign.** The prior investigation's failure
came from adaptively adding cuts (basket -> long-only -> fixed-hold -> fade -> fade-long)
until something cleared.

## 5. Promotion rules

- **Stage 1 — Screen.** Source-code exclusions above. Recorded either way.
- **Stage 2 — Translate.** Pine to `TradeRule`. Every entry carries
  `translation_verified` (see section 6).
- **Stage 3 — Development test.** Primary endpoint, net of costs, development split only.
- **Stage 4 — FDR.** Benjamini-Hochberg at **q = 0.10** across the primary endpoints of
  **every strategy in the campaign**, not per batch.
- **Stage 5 — Holdout.** Stage 4 survivors are tested once on the holdout split, using
  the identical rule and cost model. Pre-declared success: `pnl_p < 0.05` on holdout.
- **Stage 6 — Catalog.** Only Stage 5 survivors may be described as promising. Everything
  else is a recorded null result.

### Consequence for incremental batches

Because Stage 4 is computed campaign-wide, **all batch results are provisional until the
campaign closes at 30-50 strategies.** A strategy that looks significant in batch 1 may
fail FDR once batches 2-5 are added. Batch results are reported with an explicit
`provisional` flag, and no strategy may be promoted to Stage 5 before the campaign is
closed and the final strategy count is fixed. This is the specific discipline that makes
"test 10 at a time" compatible with error control — without it, incremental testing is
just sequential cherry-picking.

### Stopping rule

The campaign closes at 50 strategies, or when the sampling frame is exhausted under the
2-per-author cap, whichever comes first. The count is fixed at close and used as the FDR
family size. Additional strategies after close begin a new campaign with its own
pre-registration.

## 6. Translation fidelity

No TradingView Pro+/Premium subscription is available, so per-bar chart-data CSV export —
the only true ground truth for verifying that a Python port matches TradingView's
engine — **cannot be used in this campaign**. This is a real and acknowledged weakness.

Mitigation, and the honest limits of it:

- Every catalog entry carries **`translation_verified`**, one of:
  - `unverified` — ported from source, no external check. **The default for this campaign.**
  - `unit_tested` — port has hand-computed test cases for its indicator primitives.
  - `tv_export` — diffed against TradingView-exported per-bar values. Not achievable now.
- Ports use existing primitives in `analytics/technical.py` where one exists, rather than
  reimplementing, so translation error concentrates in already-tested code.
- The author's own description and default parameters are recorded verbatim; defaults are
  used unchanged. **No parameter tuning in this campaign** — tuning would add an untracked
  multiple-testing dimension per strategy.

**A null result from an `unverified` port is weak evidence about the strategy** (the port
may simply be wrong) but remains valid evidence that *this implementation* has no edge.
A positive result from an `unverified` port must be treated as provisional pending a
fidelity check, and this asymmetry is recorded in the catalog.

## 7. Storage

- Per-run artifacts: `storage/reports/eval/<name>_<ts>/` (existing convention).
- Registry rows: `storage/eval_registry/results.parquet` (existing, append-only). Supplies
  the honest `n_trials` for deflated Sharpe, which grows as this campaign adds rows.
- **New catalog table**: `tv_strategy_catalog`, snappy Parquet via
  `storage_utils.write_partitioned()`, mirrored into the DuckDB-readable Iceberg pilot via
  `iceberg_pilot.replace_from_parquet()` (three-slash `file:///C:/...` + `FsspecFileIO`,
  per CLAUDE.md — the two-slash pattern is unreadable by DuckDB on Windows).

### Catalog schema

| column | meaning |
|---|---|
| `strategy_id` | stable slug |
| `tv_url`, `tv_author`, `tv_script_name` | provenance |
| `tv_boosts`, `tv_views`, `tv_comments`, `collected_at` | popularity at collection time |
| `license` | MPL-2.0 default, or author-declared |
| `mechanism_family` | trend / mean-reversion / breakout / volatility / volume / hybrid |
| `param_count`, `params_json` | author defaults, unchanged |
| `screen_status`, `excluded_reason` | Stage 1 |
| `translation_verified` | `unverified` / `unit_tested` / `tv_export` |
| `n_trades`, `win_rate`, `profit_factor`, `sharpe`, `max_dd`, `turnover`, `median_hold` | descriptive |
| `total_pnl_net`, `pnl_p` | primary endpoint, net of 10 bps/side |
| `pnl_p_5bps`, `pnl_p_20bps`, `cost_fragile` | cost sensitivity |
| `bh_q`, `fdr_pass` | Stage 4, recomputed campaign-wide on every close |
| `holdout_pnl_p`, `holdout_run_ts` | Stage 5, one shot |
| `provisional` | true until the campaign closes |
| `stage`, `run_id`, `git_commit` | lineage |

Publication to a public HuggingFace dataset happens **after** the campaign closes.
Per `feedback_hf_publish_hazards`: `create_repo(private=...)` silently no-ops on an
existing repo, and per-table row counts must be diffed individually rather than in total.

## 8. Declared threats to validity

1. **No translation ground truth.** Section 6. The largest weakness in this campaign.
2. **Survivorship in the sampling frame.** Authors who believe they have edge publish
   protected, not open-source. The accessible pool is selected against exactly the
   strategies worth finding.
3. **Popularity is adversarial to quality.** Adds and boosts measure chart aesthetics and
   author following. Ranking by them selects for hindsight-attractive visuals.
4. **Residual data-quality contamination.** `clean_symbols()` is a single-day-jump screen,
   not a corporate-action audit. Some contamination survives it.
5. **Cost model is flat.** 10 bps per side ignores the spread's dependence on market cap
   and liquidity; it understates cost for small caps and overstates for mega-caps.
6. **No parameter sensitivity.** Author defaults only. A strategy may be sound with
   different parameters and fail here, or vice versa.
7. **Event clustering.** `min_gap_days` is still not wired through the adapter path (open
   thread from the prior investigation), so trades bunched within a symbol may overstate
   independent information.

## 9. Pre-declared expected outcome

Recorded now so it cannot be revised afterward: **the modal outcome is that zero
strategies clear Stage 5.** Published community technical strategies with author-default
parameters, tested net of costs on a broad universe against a permutation null, are
unlikely to show entry-timing skill. A campaign yielding no survivors is a successful
campaign that produced a 30-50 row catalog of measured null results, and it will be
written up as such rather than reframed as a search that needs continuing.

## Amendments

### 2026-08-11 (a) — corrected popularity metrics

Section 1 and the catalog schema originally assumed script pages display an
"added to charts" count. **They do not.** Direct inspection of a live script page
shows exactly three public metrics: **boosts** (rocket icon), **comments**, and
**views** (eye icon). "Use on chart" is a button, not a counter. The earlier figure
of 87,322 adds came from a page-summarizer misreading and is withdrawn; the real
values on that script were 873 boosts / 2 comments / 12,386 views.

Schema column `tv_adds` is replaced by `tv_boosts`, `tv_views`, `tv_comments`.
No result depends on this — it was recorded before any strategy was tested.

### 2026-08-11 (b) — sampling frame reprioritized

The pre-registration ranked `editors-picks` as sampling priority 1. A full
enumeration of that listing (23 entries, saved to
`storage/tv_scripts/_roster_editors_picks_2026-08-11.txt`) shows it is a poor frame
for this campaign:

| category | n |
|---|---|
| visualization / profile | 8 |
| indicator-only filter or oscillator, no trade rule | 6 |
| library / rendering engine | 4 |
| screener / backtesting tool | 2 |
| non-trading novelty (a playable chess game) | 1 |
| plausible trade-rule candidate | **2** |

A ~9% yield of testable entry+exit systems. The cause is structural, not a bad
sample: Editors' picks selects for originality and craft, and the most original
Pine work is tooling — libraries, 3D renderers, profile visualizations — not
tradeable systems. Both candidates are TASC magazine reproductions, which are
filters rather than complete systems.

**Amended priority order:**
1. `tradingview.com/scripts/` with the type filter set to **Strategies** and
   **Open-source only**, sorted by Most popular. `strategy()` scripts carry
   entries and exits by definition, which is the binding Stage 1 constraint.
2. `tradingview.com/scripts/opensource/` Most popular (indicators; exit rule
   inferred, flagged in `needs_review`).
3. `editors-picks`, retained only for the trade-rule candidates it does contain.

The 2-per-author cap and every other rule are unchanged. This amendment concerns
where samples are drawn from, not how they are tested, and was made before any
strategy was translated or any endpoint computed.

### 2026-08-12 — collection-size limit on the sampling frame

Pine source can only leave a script page through the two-channel workaround described
in `strategies/collect.py`: the flat text arrives via the page-text channel and the
per-line indentation via a separate integers-only channel, and the two are zipped back
together. Both channels pass through the collecting session's context, so the cost of
collecting a script scales with its length — roughly twice its source size. Encoding
the source to get around the content filter was tested and is blocked (`btoa` output is
rejected as "Base64 encoded data"), and the page is not server-rendered, so an ordinary
HTTP fetch returns no source.

Consequence: scripts beyond roughly 300 lines are excluded at collection time with
status `SKIP-LEN`, logged by slug in the roster file, and not counted against the
campaign's 30-50 target. The first such exclusion is
`mikVFwAu-Alpha-S-R-Channel-Strategy` (801 lines).

This is a mechanical limit of the collection channel, applied before any translation or
testing and independent of any result. It does bias the catalog toward shorter scripts,
which correlates with lower parameter counts — the same direction the protocol's
existing "high parameter count: deprioritize" rule already pushes. Any writeup must
state that the catalog describes short-to-moderate-length community strategies, not the
full population.
