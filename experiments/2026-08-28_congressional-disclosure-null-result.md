# Congressional disclosure-date event study — NULL RESULT

**Date:** 2026-08-28
**Script:** `experiments/congressional_disclosure_event_study.py`
**Data:** `congressional_trades` (54,966 rows, full backfill same day)

## Question

Does the market move after a member of Congress *discloses* a trade? If it does,
disclosure dates are a tradeable signal and belong in `signal_panel()`.

## Design

- **Events keyed on `disclosure_date`, never `transaction_date`.** The two differ by up
  to 45 days by statute (median 17 days here), so the transaction date is not public
  when it happens. Keying off it is look-ahead — the error that reversed `oil_shock`
  to null on 2026-07-07.
- **`entry_lag=1`.** A filing is only known to have landed *sometime* that day, so day 0
  is not tradeable. Earliest honest fill is the next close.
- **Buys and sells tested separately**, in opposite directions; pooling would cancel a
  real effect.
- **Common stock only** (House `ST`, Senate `Stock`). Options/bonds/munis carry a ticker
  but their reaction is not the equity's. Exchanges (367 rows) excluded — not directional.
- **Benchmark SPY** (abnormal, not raw, returns). Window (-10, +63) trading days.
- **Deduped to (symbol, disclosure_date, side)** — two members disclosing the same
  ticker the same day is one price event.

Sample: 33,389 qualifying rows → **27,102 events** (12,913 buys / 14,189 sells) →
**23,184 aligned** to the price store (11,007 buys / 12,177 sells) across 2,935 symbols.

## Result

Nothing survives at any horizon, either direction.

**Date-level statistic** — one mean CAR per disclosure date, Benjamini-Hochberg adjusted
across horizons. This is the number to believe: many members disclose on the same day, so
same-day disclosures are not independent draws.

| horizon | BUY mean% | BUY p_adj | SELL mean% | SELL p_adj |
|--------:|----------:|----------:|-----------:|-----------:|
| 1  |  0.01 | 0.892 | -0.07 | 0.609 |
| 3  | -0.02 | 0.892 | -0.08 | 0.609 |
| 5  | -0.09 | 0.892 | -0.06 | 0.609 |
| 10 |  0.02 | 0.892 |  0.69 | 0.609 |
| 21 |  0.06 | 0.892 |  0.33 | 0.609 |
| 63 | -0.28 | 0.892 |  0.28 | 0.609 |

(1,528 independent buy dates, 1,485 sell dates.) Nothing is remotely close to p < 0.05 —
the smallest adjusted p-value is 0.61.

The pooled t-stats look more interesting (buy h63 t = -3.34, sell h1 t = -3.13) but they
are **wrong-signed for a tradeable story in both cases** — disclosed *buys* drifting
*down* at 63 days, and disclosed sells also drifting down. And pooling is exactly the
overstatement the date-level test exists to correct.

**Verdict: NULL. Nothing wired into `signal_panel()`.**

This is consistent with the sentiment (2026-07-06) and oil-shock (2026-07-07) results:
the honest, clustering-aware version of the statistic keeps killing effects that look
real when pooled.

## Caveat found while checking the output — `baseline` is unusable

The `baseline_pct` and `edge_pct` columns in `event_study()`'s horizons table came back
absurd (`4.25e9`, `3.36e+22`). Cause: `event_backtest.py:274` computes the unconditional
baseline as the **cross-sectional mean daily return across every symbol in the close
matrix**, then compounds it. A single symbol with a broken price series explodes it.

**This does not affect the result above.** `mean_pct`, `t_stat`, and the whole date-level
analysis read only from `res.car` / `res.events`; `baseline` feeds `baseline_pct` and
`edge_pct` and nothing else. But **`baseline_pct`/`edge_pct` should not be trusted in any
event study run against `prices`** until the underlying data is cleaned.

## Underlying data-quality finding — `prices` contains impossible values

Surfaced by the above, and repo-wide rather than specific to this study. In `prices`
(2012+, 27,623 symbols):

- **501 symbols with `inf` daily returns** (division by a zero close)
- **536 symbols with `close <= 0`** — 32,436 rows total
- **5,076 symbols with a >500% single-day move**
- 867,149 rows with `close < $0.001`
- Absurd maxima from reverse-split/adjustment artifacts: BINI 7.48e19, ADTX 1.37e11,
  TOPS 5.81e12

These are overwhelmingly OTC/shell tickers. Sanity check: AAPL's max daily move is
15.33%, so the healthy universe is fine.

**Why this study is still valid:** only **229 of 33,600** congressional equity rows
(0.68%) touch an affected ticker — 4 with inf returns, 42 with >500% moves, 15 with
`close <= 0`. A 0.68% contamination cannot move adjusted p-values of 0.61–0.89 to
below 0.05.

## Performance note — `event_backtest` does not scale to wide universes

`load_close()` probes **every** price table per symbol and keeps the longest series.
That is right for the handful-of-symbols studies it was built for, but this study needed
2,935 symbols, so the first run issued roughly 22,000 DuckDB queries against a 47M-row
table (it was still running after 90 minutes at 5,630s CPU — working, not hung).

Worked around in this script, not in the engine: pin `price_table="prices"` (~5x fewer
queries; `prices` already covers 90% of these rows) and run **one** study with a `side`
column, splitting per-side afterwards via `_SideResult`, instead of calling
`event_study()` twice and rebuilding the close matrix each time.

The real fix — a batched `load_close_matrix()` doing one grouped query instead of N —
is left for whoever next needs a wide-universe study.

## Reproduce

```
C:\ProgramData\anaconda3\python.exe experiments/congressional_disclosure_event_study.py
```

Takes ~20 minutes, dominated by the close-matrix build. `--min-gap-days N` de-clusters
repeat events on the same symbol; `--start YYYY-MM-DD` restricts the period.

## What would change the answer

- Restricting to high-conviction subsets (large amount brackets, single-member
  disclosures, committee-relevant sectors) rather than all disclosures pooled.
- Using `transaction_date` for *attribution* while still keying entry off
  `disclosure_date` — asks a different question (are members' picks good?) than the one
  answered here (is the disclosure itself tradeable?).
- Cleaning `prices` first, which would at least make `edge_pct` meaningful.
