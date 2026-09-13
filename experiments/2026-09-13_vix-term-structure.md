# VIX Term-Structure Slope (VTSL) Overlay + Cboe Option-Strategy Indices Backtest

Date: 2026-09-13. Script: `experiments/2026-09-13_vix-term-structure.py`.
Data: `cboe_volatility` (VIX 1990+, VIX9D 2011+, VIX3M 2009+, VIX6M 2008+),
`cboe_strategy_indices` (11 published option-strategy index levels),
`market_history` `^GSPC` (benchmark). All keyless/in-house.

## Question

Catalog item 9: "VIX term-structure slope" — Johnson (2017), *JFQA* "Risk
Premia and the VIX Term Structure" — models **SLOPE** = the OLS beta of
log(VIX-futures price) on maturity. Johnson finds SLOPE forecasts variance-swap
returns (t up to -7); gross Sharpe of the slope-based signal ~1.5. We cannot trade
variance swaps, but we can test the *same information* (the shape of the implied-
vol curve) as a **timing overlay** on investable S&P 500 option strategies.

Our SLOPE: per date, OLS regression of log(implied-vol index level) on
maturity-months across the available curve points {VIX9D:0.30, VIX:1.0,
VIX3M:3.0, VIX6M:6.0} (needs >=2 points; 4 points 2011+, 3 points 2009+,
2 points 2008-2009). Positive = contango (market prices rising vol ahead),
negative = backwardation/panic.

Signal is PIT: slope at month-end close t0 -> position held from t0+1 to next
month-end.

## Results

### A) Long-only reference: option-strategy indices vs SPX

| index | ann % | vol % | Sharpe | maxDD | 2000s | 2010s | 2020s |
|---|---|---|---|---|---|---|---|
| PUT (putwrite) | 7.9 | 13.8 | 0.57 | -37% | 0.19 | 0.74 | 0.71 |
| WPUT (weekly PUT) | 5.5 | 12.1 | 0.45 | -29% | 0.39 | 0.62 | 0.34 |
| PUTR (30-delta PUT) | 8.6 | 15.6 | 0.55 | -46% | 0.55 | 0.61 | 0.51 |
| BXM (buywrite) | 7.1 | 13.7 | 0.51 | -40% | 0.30 | 0.70 | 0.62 |
| BXMD (30-delta BXM) | 11.0 | 15.3 | 0.71 | -47% | 0.24 | 0.82 | 0.71 |
| BXD (DJIA BXM) | 7.1 | 13.6 | 0.52 | -36% | 0.29 | 0.67 | 0.58 |
| BXN (NDX BXM) | 9.2 | 14.5 | 0.63 | -25% | n/a | 0.69 | 0.59 |
| CLL (95-110 collar) | 8.4 | 12.7 | 0.66 | -24% | -0.02 | 0.75 | 0.81 |
| CNDR (iron condor) | 5.1 | 7.3 | 0.70 | -20% | 0.83 | -0.07 | 0.30 |
| BFLY (iron butterfly) | 3.9 | 11.0 | 0.36 | -55% | 0.64 | -0.48 | 0.05 |
| CMBO (covered combo) | 9.5 | 13.2 | 0.72 | -43% | 0.25 | 0.78 | 0.63 |
| **SPX** | **10.0** | **18.0** | **0.55** | **-57%** | **-0.01** | **0.80** | **0.74** |

Vol-targeted to 10% ex-ante Sharpes: BXMD 0.86, CMBO 0.89, CNDR 0.77, BXN 0.67,
PUT 0.57, BXM 0.65 — most *improve* with vol targeting.

Takeaway: several published S&P options strategies dominate SPX on Sharpe at
half-to-two-thirds the vol (BXMD 0.71, CMBO 0.72, CNDR 0.70, CLL 0.66). This is
the well-known option-premium/vol-risk-premium capture, consistent with the
literature. CNDR/BFLY's collapse in the 2010s (Sharpe -0.07 / -0.48) is the
low-realized-vol regime grinding the premium down — a known tail of these
short-vol constructions.

Data coverage note: BXMD/CMBO/CNDR/BFLY/BXD have dense 1986+ history; PUT/WPUT\
PUTR are only trustworthy from 2007+ (their earlier "1991+" rows are 1-2 sparse
points per year and are masked out). BXN (NDX) starts 2009, CLL 2008.
Benchmark note: BXD and BXN are built on the Dow and the Nasdaq-100, not the
S&P 500 — their Sharpe-vs-SPX comparison is directional only (SPX is the only
free benchmark we carry in `market_history`).

### B) SLOPE-timed overlay (the test): with k=0, same-sample

| strategy | B&H Sharpe | TIMED Sharpe | TIMED ann % | TIMED vol % | TIMED maxDD |
|---|---|---|---|---|---|
| PUT | 0.56 | **0.87** | 7.60 | 8.7 | -16% |
| BXM | 0.49 | **0.67** | 5.50 | 8.3 | -17% |
| SPX | 0.55 | 0.51 | 5.20 | 10.2 | -28% |

Long the strategy **only when SLOPE > 0** (contango); cash otherwise. On PUT it
raises Sharpe 0.56 -> 0.87, cuts vol 14 -> 9% and maxDD 37 -> 16% while keeping
most of the return (7.6% vs 7.8% B&H). Long-if-slope<=0 is the mirror image
(Sharpe ~0.0) — the cash-stance during backwardation is what does the work: those
are precisely the months short-vol strategies bleed. The overlay is essentially
"collect the vol premium when the market is calm enough to have a contango curve,
step aside when the curve inverts."

SPX itself doesn't gain Sharpe from timing (0.55 vs 0.51) — the signal is about
the *vol premium*, not equity direction. This matches Johnson: SLOPE prices the
variance risk premium, not the equity premium.

### C) Slope vs next-month returns (why it works)

Slope quintiles -> average next-month: q1 (flat/backwardation) SPX +1.39% PUT
+0.75% vs q5 (steeper contango) SPX +0.30% PUT +0.65% — mildly convex with the
extremes, not cleanly monotonic. Month-level Spearman(slope, next-return): SPX
-0.133 (p=0.069), PUT -0.163 (p=0.025), BXM -0.112 (p=0.126). Weakly negative:
inverted curves modestly predict *better* next months on average, i.e. entering
after panic overreacts. The overlay's edge is not in predicting next-month mean
returns — it's in avoiding the fat left tail (the 2008-crash-style short-vol
blowups cluster in backwardation months).

### D) Threshold sensitivity (not overfit)

PUT k=0 Sharpe 0.87; k=0.02 -> 0.62; k=0.04 -> 0.51. Natural boundary (k=0,
a-priori, pre-specified from Johnson) is the best choice; raising the bar just
cuts exposure to the premium, it doesn't improve selection. No fitting performed.

### E) Out-of-sample discipline

Fixed a-priori rule (k=0), no fitting. Split at midpoint (2017-05).

| instr | dev B&H -> TIMED | OOS B&H -> TIMED |
|---|---|---|
| PUT | 0.47 -> 0.72 | 0.66 -> **1.11** |
| BXM | 0.36 -> 0.52 | 0.63 -> **1.05** |
| SPX | 0.36 -> 0.52 | 0.76 -> 0.92 |

Overlay beats B&H in **both halves, both option strategies**, and the OOS
holdout is the *better* half. Per-decade (PUT): 2000s 0.19->0.47, 2010s
0.74->0.81, 2020s 0.71->1.21. Positive in all three decades.

## Verdict

- **The SLOPE(VIX curve) overlay on vol-selling strategies is the first
  genuinely positive, OOS-robust signal in this pipeline's cross-asset work.**
  Same-sample Sharpe up 0.56->0.87 (PUT), 0.49->0.67 (BXM); OOS holdout 0.66->1.11
  (PUT), 0.63->1.05 (BXM); consistent across both halves, all decades, and the
  threshold is not overfit (k=0 wins a-priori).
- Long-only vol-selling (BXMD/CMBO/CLL) also dominates SPX on Sharpe at lower vol
  — buildable by *actually selling* S&P options (the Cboe indices are published
  execution conventions, usable as the return model), not just as an overlay.
- Caveats: (1) Cboe index levels are hypothetical fills (no real spreads, no
  margin/collateral mechanics) — live implementation via real option writing adds
  execution cost; (2) the pre-2007 Cboe curves for the older indices are Cboe
  backfills, and BXMD/CMBO/CNDR/BFLY 1986-1993 predate listed S&P options (they
  are reconstructed) so surrender their early-decade numbers; (3) short-vol is
  crash-prone (BFLY/CNDR 2010s) — the overlay mitigates but does not eliminate
  left-tail risk; (4) slope needs VIX6M+, so live signal starts 2008.
- **Follow-up worth doing:** (a) full forward-optimization loop (walk-forward +
  CPCV + PBO) on the SAME k parameter grid to measure the selection tail;
  (b) cost-load the live execution (option spreads, collateral drag);
  (c) test the overlay on the two 30-delta variants (BXMD, PUTR) and on CLL's
  collar to see which premium structure it helps most.

Data quality notes:
- PUT's raw table rows pre-2007 are 1-2/yr placeholder points; masked (start year
  = first year with >=100 observations). Without this the "1990s" Sharpe was
  garbage (2 return days over two years).
- Same masking for WPUT (2001+ sparse -> 2006+), PUTR (2001+ sparse), BXN (2009),
  CLL (2008), BXM (2002+ genuine).
- VIX9D/VIX3M/VIX6M all keyless Cboe daily settlements; sample through 2026-09-11.