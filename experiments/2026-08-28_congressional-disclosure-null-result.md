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

## Caveat found while checking the output — `baseline` was unusable (FIXED 2026-08-29)

The `baseline_pct` and `edge_pct` columns first came back absurd (`4.25e9`,
`3.36e+22`). Cause: `event_backtest.py:274` computes the unconditional baseline as the
**cross-sectional mean daily return across every symbol in the close matrix**, then
compounds it. A single symbol with a broken price series explodes it.

Root cause was in the data, not the engine — see the section below — and was fixed on
2026-08-29 (commit `503989c`). **The study was re-run end to end on the cleaned store
and every CAR, t-stat, hit rate and date-level p-value is IDENTICAL to the original
run.** Only the baseline changed, from `3.36e+22` to a plausible curve:

| horizon | 1 | 3 | 5 | 10 | 21 | 63 |
|---|---|---|---|---|---|---|
| baseline % | 0.40 | 1.17 | 1.96 | 4.05 | 8.93 | 49.15 |

That is the confirmation of the claim made at the time: `mean_pct`, `t_stat` and the
whole date-level analysis read only `res.car` / `res.events`, so the contamination never
touched the verdict. The NumPy `invalid value encountered in reduce` warnings that the
first run emitted are also gone.

## Underlying data-quality finding — `prices` contained impossible values (FIXED)

Surfaced by the above, repo-wide rather than specific to this study. `prices` carried
**173,178 impossible rows — 161,783 with NEGATIVE prices** (min close -282.83, min open
-3.6e7), plus 501 symbols with `inf` daily returns.

Two shapes, both traced to Schwab's deep history and both present identically across
independent fetches nine days apart (`price_history_pipeline.py` does no sign
arithmetic, so this is source data, not a transform bug):

- **all-zero OHLCV bars stamped on market holidays** — 1970-02-23 (Washington's
  Birthday), Good Friday, Labor Day, Thanksgiving, Christmas. Padding for non-trading
  days, not real bars.
- **sign-flipped bars that still carry real volume** — COST 1986-07-09 at
  `open -28.31 / volume 1,116,800`, with OHLC ordering internally consistent in
  negative space.

Fixed at curation (`curated._PRICE_SANITY`): raw stays the immutable record of what the
API returned, and any rebuild regenerates a clean snapshot. `prices` went 46,953,549 ->
46,780,941 rows. `futures`, `market_history` and `options_history` are deliberately
excluded — WTI really settled at -$37.63 on 2020-04-20 and options expire worthless.

**Why this study was valid even before the fix:** only 229 of 33,600 congressional
equity rows (0.68%) touched an affected ticker, which cannot move adjusted p-values of
0.61-0.89 below 0.05. The identical re-run confirms it directly.

### Still open — a second, subtler corruption

Internally-consistent but simply wrong values, which no structural rule catches. COST in
1999 reads $1-7 with 170% intraday ranges; it actually traded near $70-80. OHLC ordering
is valid and volume is plausible, so a mechanical filter would risk destroying real
data. Recent history is sound (COST 2024 averages $823.96, correct) and the damage
concentrates in pre-2010 Schwab history. Needs either cross-source validation of deep
history or a decision to truncate `prices` before a cutoff year — deliberately NOT
auto-fixed.

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
