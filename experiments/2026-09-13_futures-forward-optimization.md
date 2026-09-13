# Forward-optimization / walk-forward + CPCV on the futures book (TSMOM, carry, blend)

**Date:** 2026-09-13
**Run script:** `experiments/2026-09-13_futures-forward-optimization.py`
**Builds on:** `experiments/2026-09-12_tsmom-futures.py` + `experiments/_tsmom_core.py` + `experiments/2026-09-12_carry_futures.py`
**Artifacts:** `storage/reports/eval/futures_forward_optimization_20260913.json` (full trial ledger), `storage/reports/eval/futures_wfo_<family>_cells_daily.parquet`
**Run:** `C:\ProgramData\anaconda3\python.exe experiments/2026-09-13_futures-forward-optimization.py` from repo root

## Bottom line

| Family | Published default | Tuned WFA OOS | Default WFA OOS | Random-null pct of tuned | PBO | Verdict |
|---|---|---|---|---|---|---|
| **tsmom** | (12mo lookback, skip 1mo) | 0.25 | **0.33** | 0.726 | 0.54 | **tuning does NOT help; published default validated** |
| **carry** | (min_events=3, long-only) | **0.43** | 0.36 | 0.139 | 0.11 | tuning helps a little; honestly inconclusive (selection points at min_events=2, same long-only family) |
| **blend** | 50/50 tsmom+long-carry | 0.36 | **0.37** | 0.413 | 0.71 | tuning does NOT help OOS; but both the WF and the 2016+ one-shot drift toward pure carry (w=1) |

"WFA OOS" = stitched out-of-sample Sharpe across 7 expanding-train folds (test windows
2004-10 → 2026-09, ~3.1y each). All gross, roll-masked, monthly rebalance, 40% ex-ante
vol target, vol floor 10%. Randomized-`N` figures are gross; net-of-10bps one-way is in
the per-family tables (costs do not flip any verdict).

## What this pass asked (pre-registered)

The 2026-09-12 TSMOM writeup left "forward-optimization / walk-forward + CPCV" as the
open question. This pass is the financial-data-pipeline-side counterpart of
backtester's `catalog/walkforward.py`, applied to the futures book's two proven signals.

Root question: **would tuning help?** The published parameters were fixed before any
number here was computed (2026-09-12 backtests). The grids, stated up front and checked
to contain the published defaults:

- **tsmom:** `lookback_months {3,6,12,24} x skip_days {0,31}` (8 cells; published 12,31).
  Horizon generalizes `_tsmom_core`'s 366-day window (`round(366*months/12)`), which is
  asserted to reproduce `_tsmom_core.build_positions` byte-for-byte at (12,31).
- **carry:** `min_events {2,3,4,6} x long_short {True,False}` (8 cells; published 3,False).
  Uses the same `build_carry`/`build_carry_positions` module as the 2026-09-12 run.
- **blend:** `carry_w {0.00,0.25,0.50,0.75,1.00}` (5 cells; published 0.50), weight on the
  published long-only carry leg, `1-carry_w` on the published TSMOM leg.

**Non-tunables** (fixed at published values): vol target 0.40, vol floor 0.10 (a
documented PIT-safe cap; tuning it would data-snoop the exact thing it protects), roll
masking fixed, objective = gross Sharpe.

The pass answers four questions (mirroring `WALKFORWARD.md`):

- **Q1** did tuning beat the published default OOS?
- **Q2** did *selection* help — tuned OOS vs the **same** walk-forward driven by random
  grid choices (10,000 paths)? A walk-forward that makes money proves very little: the
  tuned OOS must clear what a coin-flip selector would produce on the same grid, folds
  and returns.
- **Q3** the full-sample fantasy (best-on-everything) — what overfitting would sell you.
- **Q4** PBO + CPCV stability of the best cell and of the published default on the shared
  full-sample calendar (combinatorial purged CV, embargo-only; these are already-realized
  daily P&L series, not labels with a `t1` to purge against).

Power floor (backtester family-2 rule): a winner must rest on >= 30 independent rebalance
decisions — dates the target weight vector changed. All three winners clear ~256.

## Machinery verification

Every cell's full-sample Sharpe reproduces the published 2026-09-12 numbers exactly:

- TSMOM published (12,31): 0.235 (published roll-masked headline 0.23)
- carry (min_ev=3, ls=False): 0.46 (published long-only 0.46); (min_ev=3, ls=True): 0.07
- blend w=0.5: 0.30 (published blend 0.30); w=0: 0.235 (= TSMOM); w=1: 0.46 (= long-carry)

## Results

### TSMOM — tuning does not help; the published default is the winner

| | tuned (per-fold argmax) | published default (12,31) |
|---|---|---|
| WFA OOS Sharpe (gross / net-10bps) | 0.25 / 0.20 | **0.33 / 0.27** |
| full-sample Sharpe | — | 0.235 |
| rebalance decisions (OOS) | 258 | 258 |

- **Random-null percentile: 0.726** — 72.6% of coin-flip selectors from the same 8-cell
  grid did at least as well. Selection did not help.
- **PBO 0.54** — picking by in-sample rank barely generalizes; the winner is noise-prone.
- Chosen parameters swing fold to fold (24,31 / 24,31 / 3,0 / 24,31 / 24,31 / 24,31 / 24,31):
  6 of 7 folds drifted to `months=24,skip=31`, whose fold-1 (GFC-era 2007-11→2010-12) test
  Sharpe was **−0.30** against the published default's +0.09 — the classic in-sample-chase.
- CPCV stability is good for *both*: best cell `(24,31)` raw OOS median 0.026 (ann ~0.41,
  positive in 100% of 15 CPCV folds); published default raw 0.030 (ann ~0.47, positive
  in 93.3%). The default is the more stable of the two.
- 2016+ one-shot: dev-chosen `(24,31)` 0.47 vs published 0.30. (24 months of lookback
  happened to do well in the last decade; the walk-forward over all 22 OOS years says
  the published 12-month default is the safer pick.)
- Full-sample fantasy: `(24,31)` at 0.44 — the number overfitting would have sold you.

**Verdict: the pre-registered MOP construction (12-month lookback, skip most recent
month) is validated. A grid over lookback/skip cannot beat it out of sample; selection is
noise.** This is the third independent replication of the backtester finding that
in-sample optimality is close to uninformative about out-of-sample performance.

### Carry — robust long-only; mild, inconclusive tuning edge

| | tuned (per-fold argmax) | published default (3, long-only) |
|---|---|---|
| WFA OOS Sharpe (gross / net-10bps) | **0.43 / 0.40** | 0.36 / 0.33 |
| full-sample Sharpe | 0.65 (min_ev=2) | 0.46 |
| rebalance decisions (OOS) | 256 | 256 |

- **Random-null percentile: 0.139** — better than the median selector but inside the
  distribution; not conclusive (0.05 bar would require <= 5th percentile).
- **PBO 0.11** — the one sweep in this pass where IS selection actually generalizes.
- Selection is *stable*, which matters: **all 7 folds picked `min_ev=2, long-only`** — a
  near-default long-only variant, not a regime-chasing flip. The tuning edge is really
  "the long-only half of the carry book, with marginally fewer roll events required".
- CPCV: best `(2,long-only)` raw median 0.040 (ann ~0.64, 100% positive); published
  `(3,long-only)` raw 0.033 (ann ~0.52, 100% positive). Both very stable fold-to-fold.
- Long-only beats long-short at every `min_events`, full-sample (0.65/0.46/0.53/0.59 vs
  0.24/0.07/0.38/0.33) — the short-carry leg persistently drags, consistent with 2026-09-12.
- 2016+ one-shot: dev-chosen 0.85 vs published 0.82 — the two long-only variants are
  effectively the same strategy, and the strategy is robust through the last decade.

**Verdict: carry long-only is the strongest, most stable signal in the futures book.
The walk-forward credibly prefers the minimal-`min_events` long-only cell, but the gain
over the published default (0.43 vs 0.36) is inside the random-selector distribution —
report as the same strategy, not an improved one.**

### Blend — no OOS improvement over the 50/50, but the data leans carry-heavy

| | tuned (per-fold argmax) | published default (w=0.5) |
|---|---|---|
| WFA OOS Sharpe (gross / net-10bps) | 0.36 / 0.33 | **0.37 / 0.31** |
| full-sample Sharpe | 0.46 (w=1) | 0.30 |
| rebalance decisions (OOS) | 256 | 258 |

- **Random-null percentile: 0.413** — inconclusive; PBO 0.71 (noise).
- Every fold chose `w=1` (= pure long-only carry). The full-sample cell curve is
  monotone: 0.24 / 0.26 / 0.30 / 0.37 / 0.46 as carry weight goes 0 → 1. The walk-forward
  and the 2016+ one-shot (w=1: 0.82 vs w=0.5: 0.53) both lean the same way.
- But this is a weak directional read, not a tuned result: the blend grid's selection is
  the noisiest of the three (PBO 0.71), and the OOS difference between w=0.5 and w=1 is
  within the null.

**Verdict: the published 50/50 blend is not better OOS than its components — pure long-
only carry dominated it both in-sample and in the 2016+ decade. The honest statement is
"carry-heavy versions of the blend are at least as good, and probably better", which is
consistent with the carry family's own verdict.**

## What costs do

Net of 10 bps one-way (turnover-scaled, per `portfolio_returns`):
TSMOM default 0.33→0.27, carry long-only default 0.36→0.33 — costs erode but do not flip
any verdict. Note carry re-ranks the whole book monthly and so has higher turnover than
TSMOM (which only transacts on sign flips); the differential is real but small at these
position scales.

## Protocol notes (what keeps this trustworthy)

- **Pre-registration before any number:** grids, non-tunables, and the four questions
  were fixed first; the script asserts each grid contains its published default (a
  "refuses any grid missing the published point" guard, like backtester).
- **Classic walk-forward:** expanding train (>= 7y), per-fold argmax on train-only IS,
  frozen on the untouched test chunk, OOS stitched across folds. IS never sees the fold's
  test window.
- **The primary statistic is random-selection, not default-vs-tuned:** 10,000 uniformly
  random grid choices over the same folds/returns is the null for "did selection help".
- **Trial ledger:** every (family, params) x fold trial is in the JSON artifact.
  Deliberately **not** unioned into the shared evaluation registry: that registry seeds
  the TV campaign's deflated-Sharpe population, and this is a separate research program
  on a separate asset class (futures) — same rule as backtester family 2 (results never
  pooled).
- **Power floor:** >= 30 independent rebalance decisions required; all winners ~256.
  Caveat (adversarial review 2026-09-13): the count is dates the weight vector changed
  (effectively ~1/month — every monthly vol-rescale changes weights, so it tracks
  month-end anchors, not independent bets), plus a ~2.5% per-fold boundary overcount
  (first-row phantom change). Still clears the floor by ~8x either way.
- **CPCV convention:** `evaluation.optimizer.cpcv_stability` reuses robustness.py's
  annualization-free ranking Sharpe; the "CPCV OOS median" rows in the JSON are raw daily
  Sharpe (annualized in this writeup by x sqrt(252)). Embargo-only purge.
- **One-shot, spent once:** the 2016+ holdout rows are exactly one evaluation of the
  dev-chosen cell and one of the published default — descriptive, not part of the
  selection statistic.

## Adversarial review (2026-09-13; two independent agents, full suite green)

No release-blocking findings; every headline number, null percentile, PBO/CPCV figure,
the dev-argmax + holdout one-shots, and the rebalance-decision counts recompute exactly
from the parquet cells and JSON artifact (RNG fully deterministic — two runs, byte-
identical JSON). Point-in-time structure verified by date-level boundary tests and
truncated-build position reproduction. The honest deltas, none verdict-changing:

- Rebalance-decision counts are effectively ~1/month (monthly vol-rescale churn) with a
  ~2.5% first-row overcount — disclosed in Protocol notes.
- Tuned net-OOS does not charge the winner-switch trade at fold boundaries (charged,
  tsmom tuned net 0.2455 -> 0.2437; only widens a verdict tuning already loses on).
- blend "tuned" (w=1) is literally the published long-only carry — same stored object as
  carry's default, so blend vs carry tuned/default figures share that identity.
- Roll-masking excises returns but keeps the excised symbol in the active-denominator
  (level convention, uniform across cells/folds/null and the 2026-09-12 publication).

## Verdict / next steps

- **TSMOM:** published parameters validated; no tuning upgrade exists in this grid. The
  2000s universe-breadth caveat from the AQR validation stands unchanged.
- **Carry long-only** is the strongest, most stable futures signal and the best selection
  case (PBO 0.11): a `min_events=2, long-only` variant is a defensible micro-upgrade but
  should be reported as the same strategy. The **10%-vol forward paper-trade of carry
  long-only** (and/or the blend) is the natural next build — carry's turnover-corrected
  numbers survive 10 bps.
- The forward-optimization loop the catalog TODO called for is now closed for the three
  futures families; nothing here promotes a new strategy on tuning evidence.
- Blockers unchanged: a faithful TSMOM/carry needs a back-adjusted futures store (rolls
  can be excised, never corrected); equity factor verdicts await the delisting panel.