# QMJ quality factor (Q approximation) — measured null / inverted

- `experiments/2026-09-13_quality-factor.py`
- Date: 2026-09-13
- Version: first pass

## Question

Does a quality (QMJ-style) long/short earn a spread in the in-house free-data
universe? The payout leg of QMJ (dividends + repurchases / net income) is not
available at universe scale in free sources, so the scope agreed with Zander was:

1. Build the Q approximation: profitability + growth + safety (A-F-P minus the
   payout leg) from first-report PIT fundamentals.
2. Validate the payout dimension directionally on the ~42-symbol simfin
   cashflow slice.

## Data and construction

- Universe: `yfinance_universe_prices` (2,570 symbols), month-end closes
  2010-01 -> 2026-09, eligibility `close >= $5` and trailing 21-day dollar
  volume `>= $10M`.
- Fundamentals: SEC EDGAR XBRL via `storage/raw/fundamentals/annual/`
  (10-K/20-F/40-F, 6,424 symbols), **first-report PIT**: for each
  (symbol, metric, period_end) take the earliest `filed` row's value
  (`ARG_MIN`); growth = year-over-year change in first-reported value.
  Raw store is used deliberately — the curated `fundamentals_annual` snapshot
  loses true filing dates (its `filed` column carries the latest comparative
  re-report date, e.g. AAPL FY2024 total_assets filed=2025-10-31 instead of the
  original 2024-11-01 10-K). That bug also affects
  `analytics/features.py::_asof_fundamentals` (PIT-late by up to ~12 months).
- Metrics: net_income, revenue, operating_income, gross_profit, total_assets,
  total_liabilities, operating_cash_flow.
- Sub-measures:
  - Profitability (Q_PROF): ROE, ROA, GP/A, CFO/A, GP margin, OP margin.
  - Growth (Q_GROW): YoY growth of net income, revenue, assets, gross profit,
    CFO, operating income (all positive-base only).
  - Safety (Q_SAFE): accruals (negative), leverage (negative).
  - Per-date cross-sectional z, winsorized at 1/99 per column first.
  - Q composite = mean of available category z's, null if fewer than 2 of 3
    categories present. Present for 87% of stock-months.

## Results

### Q L/S: long top-decile quality, short bottom-decile junk

| Metric | Value |
|---|---|
| ann. mean | -0.39% |
| vol | 10.18% |
| **Sharpe** | **-0.04 (t -0.16, n=198)** |
| long-only (d10) | 17.91% ann |
| short-only (d1) | 18.30% ann |
| decade | 2010s +0.04 (t 0.11); 2020s -0.14 (t -0.37) |
| decile slope | -1.0 bp/decile (flat) |
| net-of-cost Sharpe | -0.04 @0bp, -0.06 @5bp, -0.08 @10bp, -0.14 @25bp |
| monthly turnover | long 18%, short 18% |

Flat null: the positive long-only absolute return (18%/yr) is essentially the
universe's average; the cross-sectional spread is indistinguishable from zero.
The Q factor earns nothing L/S in this universe — no promotable edge, no strong
inversion either. Consistent with the 2026-09-12 equity-factor sweep, where the
other equity anomalies (UMD/KLN) also came out flat in this same panel.

### Determinism note (fixes applied during the build)

The first version of this script used `ARG_MIN(value, filed)` in the first-report
dedup. Real data has ~102k (symbol, metric, period_end, filed) groups where the
same `filed` date carries multiple distinct values (restated comparative filings,
weekly re-fetches), and `ARG_MIN` resolves such ties arbitrarily per execution —
the Q L/S Sharpe drifted between runs (-0.15..-0.24). Fixed with a deterministic
`ROW_NUMBER() ... ORDER BY filed, value` tie-break (`load_pit_fundamentals` in the
script). After the fix the run is exactly reproducible.

### Robustness

- Pure profitability (Q_PROF alone): Sharpe -0.03 (t -0.12), long-only d10
  16.52%, short-only d1 16.95%. Same flat-null — not a category-combination
  artifact. (Both runs above are bit-identical given the fixed load.)
- Per-category presence: Q_PROF 100%, Q_GROW 100%, Q_SAFE 99%.

### Constructive short-leg bound (missing delisted junk names)

The survivorship measurement (2026-09-13) found ~167 genuine delisted names
recoverable in `prices`, ~15 in the yfinance panel — far too few to rebuild a
delisting-inclusive Q L/S. Following the bounded-recovery rule, the constructive
bound for the Q L/S is:

- Panel (survivor) Sharpe -0.04.
- If 20% of true junk decile is missing and averages:
  - -2%/mo: +0.40 pp/mo -> Sharpe +0.10
  - -5%/mo: +1.00 pp/mo -> Sharpe +0.30
- If 35% missing:
  - -2%/mo: +0.70 pp/mo -> Sharpe +0.20
  - -5%/mo: +1.75 pp/mo -> Sharpe +0.56

Realistic case lands in the +0.1-0.3 range; even the most generous delisting
assumptions (+0.56 at the unrealistic f=35%, mu=-5%/mo corner) do not clear a
promotable hurdle, and the base-case dead-name recovery in this universe is
nowhere near 20-35% of the junk decile.

### Payout cross-check (direction of the missing 4th leg)

Simfin cashflow (41 symbols overlapping the universe, dividends + equity
repurchases as numerator; EDGAR first-report net_income as denominator;
PIT via `publish_date`):

- 26 symbols / 77 stock-months actually got a payout assigned.
- **Spearman(payout, Q) mean -0.358** (median -0.349, 0/4 dates positive).
  Negative: within this 26-name mega-cap-heavy slice, higher payout names score
  *lower* on the Q proxy — the payout leg would not reinforce quality sorting
  here, it would fight it.
- Bottom-decile names would shift substantially if payout were added (mean
  |rank shift| 0.36, >=2 quintile shift 67%) — so the leg is *not* redundant,
  but its direction is opposite to QMJ's assumption in this slice.

Interpretation: this is directional only and on a handful of mega-caps (the
simfin slice is heavily large-cap; the universe's quality spread lives in the
small/mid tail). A real universe-scale payout leg needs Sharadar SEP/DAILY or
CRSP-class data — Zander's purchase call, unchanged.

## Verdict

- **Q L/S (quality factor): null in this free-data universe.** Cross-sectional
  spread indistinguishable from zero (Sharpe -0.04; Q_PROF alone -0.03). Not
  buildable as a live L/S strategy here. The positive long-only side is just
  universe-average beta, not a factor edge.
- **Payout leg: cannot be built free** (no universe-scale dividends+repurchases);
  directional check says it would not rescue the strategy in its current form.
- Same conclusion as UMD/KLN/momentum sweeps: in this volatile, small-cap-heavy,
  survivor-panel universe the standard equity anomalies come out flat or die.
- Free-data QMJ: **dead end** (unless/until paid SEP/DAILY or CRSP-class data).

## Files

- `experiments/2026-09-13_quality-factor.py` — PIT build + decile backtest +
  payout cross-check.
- OUT: `storage/reports/eval/q_factor_daily.parquet` (198 monthly Q L/S obs).
- Related: `experiments/2026-09-13_survivorship-bias.{py,md}` (measured
  survivorship limits + bounded-recovery rule), `experiments/2026-09-12_equity-factors-sweep.py`
  (low-vol inversion precedent).