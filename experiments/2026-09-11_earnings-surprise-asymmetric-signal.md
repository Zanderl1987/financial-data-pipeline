# Earnings surprise event study — ASYMMETRIC SIGNAL FOUND

**Date:** 2026-09-11
**Script:** `experiments/earnings_surprise_event_study.py`
**Data:** `alpha_vantage_earnings` (quarterly, 4,624 rows → 3,668 w/ surprise → 3,668 events across 31 symbols, 1996-2026)

## Question

Does the market react to quarterly earnings surprises (beat vs miss) on the
report date? If so, surprise direction/magnitude is a tradeable signal for
`signal_panel()`.

## Design

- **Events keyed on `reportedDate`** (actual release date), not fiscal period
  end. Fiscal end is known weeks in advance; the surprise becomes public at
  release.
- **`entry_lag` by `reportTime`**: pre-market → 0 (tradeable same day),
  post-market → 1 (next day). Unknown → 1 (conservative).
- **Quarterly only** (annuals noisier, often simultaneous with Q4).
- **Beat vs Miss**: `surprisePct > 0` vs `< 0`. Magnitude filter
  `--min-surprise` tests strength dependence.
- **Significance**: pooled + date-level BH (same-day releases cluster).
- **Benchmark**: SPY (abnormal returns). Window (-10, +63).
- **Price table**: `prices` pinned (avoids load_close probing).
- **31 symbols** (DJI-ish mega-caps: AAPL, MSFT, JPM, etc.) — narrow universe
  but long history (1996+). This is the current Alpha Vantage free-tier
  coverage.

## Result (all surprises)

| Direction | Events | Aligned | Date-level verdict |
|---|---|---|---|
| BEAT | 2,574 | 2,525 | **SIGNIFICANT** all horizons 1-63d (p_adj ~1e-4) |
| MISS | 1,094 | 1,090 | NULL (all p_adj > 0.05; h63 wrong sign) |

**BEAT date-level**: h1 0.42% (t=3.89, p_adj=0.0002), h21 0.74% (t=4.26),
h63 1.59% (t=4.91). **Persistent positive drift** after positive surprise.

**MISS date-level**: all ns. The negative drift only appears at larger
surprise magnitudes.

## Result (|surprise| ≥ 5%)

| Direction | Events | Aligned | Date-level verdict |
|---|---|---|---|
| BEAT | 1,412 | 1,395 | **SIGNIFICANT** all horizons (p_adj ~0) |
| MISS | 283 | 282 | **SIGNIFICANT** h1-h5 (p_adj=0.011) |

**BEAT**: h1 0.73% (t=5.8), h63 2.59% (t=6.6). **MISS**: h1 -1.0% (t=-3.11,
p_adj=0.011), h3 -0.95%, h5 -1.02%. **Signal strengthens with magnitude** and
becomes symmetric for large surprises.

## Interpretation

- **Asymmetric at low magnitudes**: small beats drift up; small misses do
  nothing (market ignores or already priced the downside).
- **Symmetric at high magnitudes**: large beats drift up, large misses drift
  down — the market *does* react to clear surprises, but the threshold is
  around 5% surprise.
- **Post-earnings announcement drift (PEAD)** confirmed for this mega-cap
  universe, with a magnitude gate.
- **Universe limitation**: 31 symbols only. The effect may differ in
  mid/small caps (no data). If/when broader earnings history becomes
  available (Finnhub paid tier, other sources), re-run.

## Caveats

- **Survivorship**: `prices` only has survivors; delisted mega-caps rare.
- **ReportTime accuracy**: Alpha Vantage's pre/post-market tag may have
  errors; entry_lag misclassification would dilute the signal (we see it
  anyway).
- **Overlap with other factors**: earnings surprise correlates with
  momentum/revision factors. Not yet orthogonalized.
- **Date-level n_dates**: 1,623 (beat) / 847 (miss) at full set — plenty of
  independent dates, BH correction is honest.

## Reproduce

    C:\ProgramData\anaconda3\python.exe experiments\earnings_surprise_event_study.py
    C:\ProgramData\anaconda3\python.exe experiments\earnings_surprise_event_study.py --min-surprise 5

## What would change the answer

- **Broader universe** (mid/small caps): if the effect is mega-cap specific,
  expanding coverage could dilute or change it. Need a broader earnings
  source.
- **Orthogonalization**: controlling for momentum/analyst revisions may
  absorb the drift (classic PEAD debate).
- **ReportTime corrections**: if pre/post tags are noisy, a cleaner
  entry_lag (e.g., from exchange timestamps) could sharpen the signal.