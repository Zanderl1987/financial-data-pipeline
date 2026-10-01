# Option Strike Introduction Study — Design

Date: 2026-10-01 · Status: approved in conversation, pending written-spec review

## Purpose

Two questions, from Zander's observation that exchanges add option contracts around
events (earnings, large moves):

1. **Continuation.** After a stock has *already started moving*, does the listing of new
   strikes beyond the existing strike range, on the side of the move, predict that the
   move continues?
2. **Mechanics vs information.** Is any such effect just the mechanical/volatility
   response (exchanges must keep strikes around the price) — or does listing *in excess
   of* what mechanics require carry the information? And do new strikes predict the
   *size* of future moves rather than their direction?

Success = a defensible answer to both, including a well-powered null. Not success = a
tuned backtest.

## Background

- Strikes are listed by the **exchanges** (Cboe/Nasdaq/NYSE rule books, OLPP): after the
  underlying moves, on a calendar cycle, and on demonstrated customer demand (>30% OTM
  requires customer interest; market makers' own-account interest is excluded).
- Hojoon Lee (2025), *The Information in Option Strike Price Introductions* (FMA
  Derivatives 2025; OptionMetrics 1996–2023): stocks with more strikes introduced above
  the prior month's max strike than below the min outperform the reverse by ~4% over 12
  months (t 4.28), not explained by 1-month return or momentum; mostly the downside
  leg; month-1 spread insignificant (slow drift); stronger in earnings months and high
  option/stock-volume names; delistings and index options carry no signal.
- This study differs: daily resolution, conditioned on an already-started move.

## Data

### Source and gate

Massive (formerly Polygon) `GET /v3/reference/options/contracts` with `as_of=<date>`,
free "Options Basic" tier: 2 years of history, 5 requests/minute. Key in `.env` as
`MASSIVE_API_KEY`.

**Step 0 gate (must pass before any further build).** On AAPL:
1. `as_of` ≈ 18 months ago returns contracts that have since expired (with
   `expired=true` where needed). If history only contains still-live contracts, STOP —
   source is unusable; revisit paid options.
2. Today's contract count is within a few % of our own full-chain `schwab_options`
   snapshot (first full snapshot: 2026-10-02).
3. A strike addition appears on a date consistent with AAPL's price path.

### Universe

~100 S&P 500 single stocks with the highest total option volume in `schwab_options`
snapshots 2026-08-02..2026-09-30; ETFs excluded. Frozen to
`experiments/strike_intro_universe.csv`. Selection on recent activity is a survivorship
tilt — stated in the writeup.

### Pipeline: `massive_option_listings_pipeline.py`

- For each symbol and each trading day in the last 2 years: fetch the full `as_of`
  contract list (paginated, `limit=1000`), diff against the previous trading day.
- Request pacing 12 s (5/min); 429 → backoff; per-symbol checkpoint so a restart resumes.
- Launched detached (`Start-Process`, `python -u`, log file) — session churn must not
  kill a ~3-week job.
- Outputs (lossless: first day stored as the full list with `change='initial'`):
  - `option_listing_changes`: `symbol, date, contract_ticker, strike, expiration_date,
    put_call, shares_per_contract, change ∈ {initial, added, removed}, fetched_at`.
    Curated key: `(symbol, date, contract_ticker, change)`.
  - `option_chain_summary`: `symbol, date, n_contracts, n_expirations, min_strike,
    max_strike, fetched_at` (standard contracts only). Key: `(symbol, date)`.
- Full repo wiring checklist (CATALOG, validate SCHEMAS, curated KEYS, run_all
  PipelineSpec with `requires_env=["MASSIVE_API_KEY"]`, tests). Excluded from the daily
  schedule (one-time backfill; forward days come from `schwab_options` full chains).

### Other inputs

- Prices: curated `prices` via `event_backtest.load_close_matrix`; benchmark SPY.
- Earnings dates: `event_backtest.earnings_events()` (AV + Finnhub) plus yfinance
  earnings dates for the 100 names. If coverage over the window is < 90%, the earnings
  subgroup is labelled incomplete.

## Signal

Standard contracts only (`shares_per_contract == 100`, no adjusted roots such as FDX1).

- Range on day t−1: min/max strike over all live standard contracts.
- `ABOVE_t` / `BELOW_t`: count of contracts added on day t with strike > prior max /
  < prior min.

### Events (two separate sets)

- **Day trigger:** |1-day return − SPY return| > 2.5 × trailing 60-day σ of daily excess
  returns. Direction d = sign of the excess return.
- **Run trigger:** |cumulative 5-day excess return| > 2 × σ scaled to 5 days (σ·√5).
  Event day = the day the threshold is first crossed; no new run event for that symbol
  within 5 days.
- Earnings flag: event within [−1, +1] trading days of an earnings date.

### Event measures (pre-registered)

- `DIR_INTRO` = Σ over days e+1..e+2 of `ABOVE` (d=+1) or `BELOW` (d=−1).
  `OPP_INTRO` = same for the opposite side (diagnostic).
- **HIT** = `DIR_INTRO > 0`, **MISS** = `DIR_INTRO == 0`.
- **Entry: close of e+3 for every event** (identical timing for both groups; listings are
  public pre-open, so this is ≥1 day after observability).
- One labelled sensitivity: window e+1 only, entry close e+2.

### Excess measure (mechanics vs demand)

Poisson regression of directional introductions per stock-day t (all stock-days, both
sides as separate rows) on covariates known at the close of t−1 (listings on t are
made pre-open, in response to t−1): headroom = distance from the t−1 close to the t−1
extreme strike on that side (%), the t−1 move in σ units signed toward that side, 60-day
σ, earnings flag, trading days since monthly expiration, log price.
Predictions are leave-one-symbol-out (each symbol's fit excludes it).
`EXCESS = DIR_INTRO − expected DIR_INTRO over the same window`. Events split by EXCESS
tercile (and > 0 vs ≤ 0).

## Evaluation

Returns: excess vs SPY, signed by d (positive = continuation). Horizons 1, 3, 5, 10, 21,
63, 126 trading days from entry. Robustness: market-model (beta-adjusted) abnormal
returns, beta from the 250 days ending at e−5.

**Primary test (pre-registered):** day trigger, HIT − MISS signed excess return at 21
days. All other tests are secondary and BH-adjusted together.

Per group × horizon × trigger:
- n, mean, median, continuation rate, 95% CI.
- HIT − MISS: difference, Welch t-test p, permutation p (labels shuffled within
  symbol-month strata, ≥ 10,000 draws), week-block bootstrap CI and p, Mann-Whitney p,
  Cohen's d.
- Benjamini-Hochberg adjusted p across all secondary tests.
- Regression: signed return_h ~ HIT + EXCESS + move σ + earnings + 60d σ + headroom +
  prior 21d return; standard errors two-way clustered (date, symbol).
- Non-overlap robustness: first event per symbol per 21 trading days.
- Minimum detectable effect (80% power, α = 0.05) for the primary test at realized n.
- Subgroups: up vs down; earnings vs non-earnings; EXCESS terciles.
- **Volatility check:** same battery on |21-day excess return| and on realized-vol ratio
  (σ over e+3..e+24 / σ over e−60..e−1), HIT vs MISS and by EXCESS tercile.
- Trading view (illustrative): long up-move HITs / short down-move HITs, 21-day hold,
  equal weight, 10 bp per side; equity curve, Sharpe, win rate, profit factor, max DD.

## Outputs

- Interactive Plotly report, published as a private artifact:
  1. CAR paths (e−5..+126) HIT vs MISS with 95% bands; filters: trigger, direction,
     earnings.
  2. HIT − MISS by horizon with CIs; hover shows raw and BH p.
  3. Forward return by EXCESS tercile.
  4. Volatility check distributions.
  5. Event scatter EXCESS vs 21-day signed return (hover: symbol, date, earnings).
  6. Strike-range explorer: per symbol, price inside min/max strike band with listing
     markers (symbol dropdown).
  7. Sortable table of every test.
  8. Trading-view equity curve.
- `experiments/2026-10-xx_strike-intro.{py,md}` writeup (null results included), result
  rows registered in `storage/eval_registry`.

## Correctness checks

- Pipeline: fake-client tests (diffing, resume, adjusted-contract filtering); live trial
  3 names × 1 month; lossless check — rebuild a random day's list from changes and match
  a direct `as_of` call exactly.
- Analysis on synthetic data: planted effect is detected; no-effect data gives roughly
  uniform p-values; leakage probe — shifting entry earlier must look better.
- Cross-source: listing dates agree with `schwab_options` full chains over the overlap
  (~2026-10-02 onward).

## Build order

1. Step 0 gate (needs Zander's `MASSIVE_API_KEY`).
2. Pipeline + tests + live trial + lossless check.
3. Detached ~3-week backfill.
4. Analysis module + synthetic tests, developed on partial data.
5. Final run → report artifact → writeup.

## Risks

- Gate fails (no expired contracts in `as_of`) → paid source needed.
- Few HIT events on 100 names → reported MDE tells whether a null is informative.
- 2 years is one market regime; results are not a trading recommendation.
