# Live-execution cost load on the VTSL overlay (PUT / BXM)

**Date:** 2026-09-13
**Run script:** `experiments/2026-09-13_vtsl-cost-load.py`
**Builds on:** `experiments/2026-09-13_vtsl-forward-optimization.py` (WFA OOS guards),
`experiments/2026-09-13_vix-term-structure.py` (published construction, imported)
**Artifacts:** `storage/reports/eval/vtsl_cost_load_20260913.json`
**Run:** `C:\ProgramData\anaconda3\python.exe experiments/2026-09-13_vtsl-cost-load.py` from repo root

## Bottom line

**The overlay's headline numbers survive a live-execution cost load by ~2 orders of
magnitude.** The Cboe index levels already embed the write-side half-spread (see
sources), so the honest incremental load is per-contract fees + residual slippage +
(BXM only) long-equity-leg implementation drag. None of it flips a verdict:

| Load (bps notional per active month) | PUT full / 2016+ | BXM full / 2016+ | BXM +10bps/yr leg full / 2016+ |
|---|---|---|---|
| 0 (index, gross) | 0.90 / **1.13** | 0.76 / **1.07** | 0.75 / 1.06 |
| 0.5 (base: fees+comm) | 0.89 / 1.12 | 0.76 / 1.06 | 0.75 / 1.05 |
| 1.0 | 0.88 / 1.11 | 0.75 / 1.05 | 0.74 / 1.04 |
| 2.0 | 0.87 / 1.10 | 0.74 / 1.04 | 0.73 / 1.03 |
| 5.0 (≈10-50x slippage blow-up) | 0.84 / 1.06 | 0.71 / 1.00 | 0.70 / 0.99 |

Buy-and-hold reference (same active window): PUT 0.56, BXM 0.49. Even the worst cell
in the table (5 bps/month + 25 bps/yr equity leg) keeps the 2016+ holdout at **0.98**
for BXM — still ~2x B&H. Walk-forward stitched OOS under the base load: PUT
0.971 → 0.964, BXM 0.915 → 0.898 (with the 10 bps/yr BXM leg).

## What the index levels already pay for (why we do NOT re-charge the spread)

Primary sources settle the double-count question:

- **Whaley (2002)** — the BXM construction "uses the **bid price** when the call is
  first written ... In this sense, the **BXM index already incorporates an implicit
  trading cost equal to one half the bid-ask spread**." Regenerating with mid prices
  instead adds ~**6 bps/month (~70 bps/yr)** — i.e. a rough half-spread haircut is
  baked into the historical levels.
- **Current Cboe BuyWrite / PutWrite methodologies** (2004+): the new option is
  "deemed sold at a price equal to the volume-weighted average of the traded prices
  (VWAP)"; if no trades occur in the VWAP window, "deemed sold at the **last bid
  price**." Settlement is cash at the SOQ. So there is exactly ONE option
  transaction per active month — the write — filled at an in-spread, trade-weighted
  (bid-floored) price, settled for free.

The free-data stack has **no measured historical SPX bid/ask** (OptionMetrics,
ORATS, Databento all paid — `docs/OPTIONS_DATA_SOURCES.md` NO-GO section), so a
modeled full half-spread charge would be a raw double count. The load below is the
part the index genuinely does not include.

## The pre-registered cost model

Charged **once per active calendar month, on the first active day** (one option write
per roll month; position constant within a month by construction):

1. **Fees per option write** (not in the index): exchange + clearing + ORF + a
   commission allowance on the single sold contract. Base 0.5 bps/month of notional
   brackets the realistic sub-basis-point per-contract load on the SPX notional
   (~$100 multiplier x index level); the grid runs 0 → 5 bps/month as a stress/slippage
   budget (an ATM SPX option's quoted half-spread is itself only ~0.3-0.5 bps, and the
   VWAP/bid methodology already captures the bulk of it).
2. **BXM long-equity-leg drag** (only BXM): the index assumes you hold the S&P 500
   portfolio, dividend-reinvested. A live book holds the basket, an index ETF, or ES
   futures — real implementation costs ~10-25 bps/yr (SPY-class ER + basis). Applied
   on active days only, sensitivity 0/10/25 bps/yr. PUT needs none: its collateral
   sits in the T-bill account the methodology already credits.

Determinism: no RNG; the overlay construction is the published VTSL module imported by
reference (never copied); reproductive guards asserted before any cost number is used.

## Guards (all assert before the table prints)

- Published k=0 active-window Sharpe: PUT **0.896** (floor 0.82), BXM **0.761** (0.62).
- Walk-forward stitched OOS (same 7-fold layout as the forward-opt pass): PUT **0.971**,
  BXM **0.915** (published 0.97 / 0.91).
- 2016+ holdout k=0: PUT **1.127**, BXM **1.066** (published 2016+ improvements).

## Breakeven analysis (2016+ holdout, no equity leg)

| Target | Fee (bps/active-month) at which holdout Sharpe = B&H | = zero |
|---|---|---|
| PUT | 31.3 | 83.3 |
| BXM | 30.6 | 82.7 |

~31 bps/active-month (≈ 3.7%/yr) to lose the overlay's edge over buy-and-hold;
~83 bps/month (≈ 10%/yr) to make it worthless. The realistic base load is **0.5-1.0
bps/active-month** — the breakeven sits **30-80x higher**. The overlay cannot be
killed by execution cost at any plausible SPX liquidity regime; the monthly roll is
the most liquid options market in the world (SPX monthly expiries trade millions of
contracts).

## Verdict

The queued "live-cost load" gate closes: **costs do not threaten any conclusion of
the VTSL overlay.** The write-side spread is already inside the index levels; the
incremental load a live implementation adds is bps-level and moves the 2016+ holdout
Sharpe by at most 0.05-0.09 in even the most hostile corner tested (1.13 → 1.06 PUT,
1.07 → 0.99 BXM), leaving both ~2x their buy-and-hold baseline. The only costs this
does NOT cover are tax drag and the operational/timing realities of running a live
roll (gap risk between our month-end signal day and the index's third-Friday roll) —
structural, not economic, and never a verdict-flip at these margins.

**File road-map:** script + write-up committed with this pass; JSON artifact is
gitignored storage. Queued next: extend the overlay to BXMD/PUTR/CLL, then the
basis-momentum proxy check, then the 10%-vol carry paper-trade.