# Forward-optimization / walk-forward + CPCV on the VTSL overlay (PUT/BXM shorting precipitate)

**Date:** 2026-09-13
**Run script:** `experiments/2026-09-13_vtsl-forward-optimization.py`
**Builds on:** `experiments/2026-09-13_vix-term-structure.py` (published SLOPE construction + `month_positions()` — loaded by reference, not copied)
**Artifacts:** `storage/reports/eval/vtsl_forward_optimization_20260913.json` (full trial ledger), `storage/reports/eval/vtsl_wfo_<family>_cells_daily.parquet`
**Run:** `C:\ProgramData\anaconda3\python.exe experiments/2026-09-13_vtsl-forward-optimization.py` from repo root
**Determinism:** JSON artifact byte-identical across 3 runs (RNG seeded, argmax ties broken by grid order).

## Bottom line

|  Family | Published default | Tuned WFA OOS | Default WFA OOS | Random-null pct of tuned |  PBO  | Verdict |
|---|---|---|---|---|---|---|
| **put.k**  | k=0 (SLOPE>0) | **0.97** | **0.97** | 0.026 | 0.27 | tuning TIES published; k=0 picked 7/7 folds |
| **put.ma** | ma=0 (no confirmation) | 0.95 | **0.97** | 0.073 | 0.27 | confirmation filter does NOT help; default wins |
| **bxm.k**  | k=0 (SLOPE>0) | **0.91** | **0.91** | 0.020 | 0.24 | tuning TIES published; k=0 picked 7/7 folds |
| **bxm.ma** | ma=0 (no confirmation) | 0.89 | **0.91** | 0.064 | 0.34 | confirmation filter does NOT help; default wins |

"WFA OOS" = stitched out-of-sample Sharpe across 7 expanding-train folds on the
SLOPE-active daily sample 2008-01-02 → 2026-09-11 (4,703 days; test windows 2013-05 →
2026-09, ~1.9y each). All gross, published PIT construction (signal at month-end close,
position from t0+1), trimmed to the SLOPE-active window. Net-of-10bps one-way per
position flip is in the per-family tables (costs do not flip any verdict — see caveats).

## What this pass asked (pre-registered)

The 2026-09-13 VTSL writeup left "forward-optimization loop (CPCV/PBO) on the k grid"
as the top queued follow-up. This is the same discipline as the futures
forward-optimization pass (same expanding walk-forward + CPCV + PBO + 10k
random-grid-selection null), applied to the one signal in the cross-asset book that
survived a first test (PUT/BXM long-if-SLOPE>0, k=0 a-priori).

Root question: **would tuning the overlay's knobs beat the published rule?** The
published parameters were fixed before any number here was computed. The grids, stated
up front and checked to contain the published defaults:

- **family `<tgt>.k`:** threshold `k` on SLOPE for long-if-SLOPE>k, cash else.
  `k in {0.00, 0.01, 0.02, 0.03, 0.04, 0.05}` — published **k=0**.
- **family `<tgt>.ma`:** trend-confirmation months: long only when SLOPE>0 **and** SLOPE
  is above its m-month rolling mean. `m in {0, 3, 6, 12, 24}` — published **m=0** (which
  IS the plain k=0 rule; m>0 is a strict subset of long days).
- Targets: **PUT** and **BXM** (the two vol-selling strategies the overlay was validated
  on). Four families: put.k, put.ma, bxm.k, bxm.ma.

**Non-tunables** (fixed once): P&L trimmed to the SLOPE-active window (2008-01+), so
Sharpe comparisons are over the signal's own sample rather than years of idle cash;
objective = gross Sharpe; `month_positions()` is the published t0+1 construction.

The pass answers the same four questions as the futures book (mirroring `WALKFORWARD.md`):

- **Q1** did tuning beat the published default OOS?
- **Q2** did *selection* help — tuned OOS vs the **same** walk-forward driven by random
  grid choices (10,000 paths)?
- **Q3** the full-sample fantasy (best-on-everything) — what overfitting would sell you.
- **Q4** PBO + CPCV stability of the best cell and of the published default on the shared
  full-sample calendar (combinatorial purged CV, embargo-only; realized daily P&L series,
  not labels with a `t1`).

Power floor (backtester family-2 rule): a winner must rest on >= 30 independent rebalance
decisions — dates the held position changes. Tuned winners clear it (31 k-family / 41
ma-family, monthly month-end signals over 18y).

## Machinery verification

Every cell's construction is the published one (module loaded by reference).
`month_positions()` reused verbatim; SLOPE the published OLS beta. Published
reproduction guard (k=0, active window): PUT **0.90** (published 0.87 same-sample), BXM
**0.76** (published 0.67 same-sample). Both pass their asserted floors — the published
k=0 overlay is exactly the k=0 default cell of grid `put.k`/`bxm.k`. Buy-and-hold
reference on the same window: PUT 0.56, BXM 0.49 (the overlay's 0.90/0.76 are on top of
these).

## Results

### PUT — k=0 is the whole story; nothing to tune

| | tuned (per-fold argmax) | published default (k=0) |
|---|---|---|
| WFA OOS Sharpe (gross / net-10bps) | **0.97 / 0.95** | **0.97 / 0.95** |
| full-sample Sharpe | — | 0.90 |
| rebalance decisions (OOS) | 31 | 31 |

- **Chosen parameter every fold: k=0, 7/7.** Tuned OOS == default OOS exactly.
- **Random-null percentile 0.026** — only 2.6% of coin-flip selectors from the same
  6-cell grid did at least as well. The grid itself is uniformly positive
  (random-null median 0.75, p95 0.94): there is nothing in the k dimension to select.
- **PBO 0.27** — selection by IS rank generalizes acceptably.
- **CPCV:** best cell (k=0) OOS median 0.059 (annualized ~0.24 on 1/3-year slices),
  positive in 100% of 15 splits; default identical. Stable fold-to-fold.
- 2016+ one-shot: dev-chosen and published are both k=0 — **1.13** on 2016+, up from the
  published 2017-05+ holdout's 1.11.
- Full-sample fantasy: k=0 at 0.90 — the "overfit" number is indistinguishable from the
  default, because the default is the argmax.

**Verdict: tuning does not help because there is nothing to tune — the pre-registered
k=0 threshold is the robust pick on every fold of every statistic.** The random-null
percentile says the k grid adds no selection risk it didn't already own.

### ma confirmation (PUT and BXM) — the filter costs, it does not add

| | tuned (per-fold argmax) | published default (ma=0) |
|---|---|---|
| PUT WFA OOS (gross / net-10bps) | 0.95 / 0.92 | **0.97 / 0.95** |
| BXM WFA OOS (gross / net-10bps) | 0.89 / 0.86 | **0.91 / 0.89** |

- Tuned OOS **does NOT beat** the published default in either family — the one fold that
  drifted (fold 2, both targets) picked ma=12 and lost ground.
- Random-null percentile 0.073 (PUT) / 0.064 (BXM): selection inconclusive (the ma grid is
  also uniformly positive — random-null medians 0.71-0.76, p95 0.90-0.97).
- PBO 0.27 / 0.34. CPCV default (ma=0) identical to the k-family numbers; best cell is
  ma=0 in both.

**Verdict: adding a 3-24m trend-confirmation on top of SLOPE>0 removes winning months
without removing losing ones — it is a strict-loss filter on the published signal.**
The plain rule stands.

### Net of the 10bps placeholder fee

Net-10bps (per position flip) knocks ~0.02-0.03 off every OOS Sharpe — a flip on this
all-in/all-out monthly book costs 20bps round-trip. Nothing flips: put.k 0.97 gross →
0.95 net, bxm.k 0.91 → 0.89, tuned==default unchanged. **Caveat: this is a flat-trade
placeholder, not a live option-writing cost model** — Cboe index levels embed
signature-level signing costs only; actual bid/ask, exchange fees and margin on the
S&P 500 putwrite/buywrite each month are not loaded. That is the other queued follow-up;
nothing here predates it.

## Conclusion

The VTSL overlay survives the full forward-optimization gauntlet the futures book
developed:

1. **No tuning promoted.** The pre-registered k=0 rule was picked 7/7 folds by the
   in-sample-argmax selector, ties the tuned OOS, and beats it on the ma grids.
2. **Selection non-risk.** The whole k/ma grid space is positive; random selectors make
   money but the default's tuned OOS sits at the 97-98th percentile of the 10k-path null —
   the edge is in the signal, not in the grid.
3. **Stable.** PBO < 0.4 (0.24-0.34), CPCV positive in 100% of 15 splits for both targets
   and both the best and default cells, and the 2016+ holdout strengthens (PUT 1.13, BXM
   1.07).
4. **Costs do not yet flip anything** at 10bps one-way; the live option-writing cost load
   remains the honest next economic question before this is tradeable.

This is the first signal in the cross-asset book to clear the walk-forward loop with the
published parameters intact (the futures book's carry came closest but showed a
micro-tune; TSMOM's tuning actively hurt). Same family-2 rule as the futures pass:
trials NOT unioned into the shared evaluation registry.

**File roadmap:** script + writeup committed with this pass; JSON and cell-daily parquet
artifacts are gitignored storage. Queued next: live-execution cost load for index-option
writing, then extend the overlay to the other vol-selling strategies (BXMD/PUTR/CLL).