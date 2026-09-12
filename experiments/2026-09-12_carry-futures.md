# Carry + Carry x TSMOM on our 44 futures (2026-09-12)

Companion to `experiments/2026-09-12_carry_futures.py`. Same infrastructure as
the TSMOM backtest (`experiments/2026-09-12_tsmom-futures.md`), same vol
targeting (40% ex-ante, 10% vol floor), monthly month-end rebalance, positions
effective t0+1, roll-day returns excised.

## Bottom line

| strategy | Sharpe | ann. mean | vol | maxDD |
|---|---|---|---|---|
| TSMOM (reference) | 0.23 | 4.0% | 17.2% | -48.7% |
| Carry long-short | 0.07 | 0.5% | 6.8% | -43.5% |
| **Carry long-only** | **0.46** | **4.8%** | **10.4%** | **-36.9%** |
| Carry x TSMOM 50/50 | 0.24 | 2.3% | 9.3% | -27.7% |
| TSMOM + long-carry blend | 0.30 | 2.9% | 9.8% | -29.8% |

Decade Sharpe: carry long-only 2000s 0.33 / 2010s 0.42 / 2020s **0.83**;
long-carry blend 0.27 / 0.48 / 0.82. TSMOM reference reproduces the headline
0.23 from the earlier writeup.

The single cleanest takeaway: **long-only futures carry (roll-gap basis) is the
strongest pure signal in the futures book** (Sharpe 0.46, and ~0.7-0.8 through
the 2020s) — better than TSMOM alone. Folding it into TSMOM as a 50/50 blend
lifts 0.23 -> 0.30 with maxDD -30% (vs -49%).

## Construction

Carry proxy — the futures store is continuous FRONT-MONTH only (no back-month,
no spot), so KMPV's (F1/F2) carry is not directly computable. Proxy = the roll
gap: on the day the front contract expires the price jumps from the expiring F1
to the new front (the former F2); that overnight gap embeds the basis.

    carry_event = -ln(open[t] / close[t-1])   on detected roll days
    carry_score = mean(carry_event over trailing-252d roll events, min 3)
                  annualized by the instrument's own median roll interval

Caveat: the gap also carries one day of the asset's own return — this is a
directionally-correct *proxy* for the basis, good for cross-sectional ranking,
not for absolute carry levels. Long-only = top half of each month's carry
ranking; long-short = top half minus bottom half; both vol-scaled to ~half the
TSMOM target so the blends sum to 40% vol budget.

Span of coverage: carry score available for an average 33/44 instruments from
late-90s/2000s on (11,424 instrument-month observations). 1990s columns are NaN
(too few roll events in a year).

## Reading

- The short side is the drag (long-only 0.46 vs long-short 0.07) — consistent
  with the published carry literature: *longing* backwardation contracts pays
  (commodities/energy/metal carry), *shorting* the contango-heavy ags/softs
  does not pay (they're chronically weak); the strong 2020s carry returns agree
  with the recent outperformance of carry/factor premia.
- Carry x TSMOM as a sign-combination adds little (0.24) because the two
  signals overlap in direction for exactly the energy/metal names carry likes.
  The TSMOM + long-carry blend (0.30) is the more sensible everyday package.
- Same data caveats as TSMOM: front-month stitching, flat-price glitch windows
  (vol floor), no back-month series. A back-adjusted store would let the carry
  proxy be replaced with the true F1/F2 carry.