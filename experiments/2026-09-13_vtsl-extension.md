# VTSL overlay extension: BXMD / PUTR / CLL — the transferred rule, scored OOS on the other sellable-vol strategies

**Date:** 2026-09-13
**Run script:** `experiments/2026-09-13_vtsl-extension.py`
**Builds on:** `experiments/2026-09-13_vtsl-forward-optimization.py` (machinery imported, not copied — the EXACT walk-forward code that scored PUT/BXM), `experiments/2026-09-13_vix-term-structure.py` (published SLOPE + `month_positions()`)
**Artifacts:** `storage/reports/eval/vtsl_extension_20260913.json`, `storage/reports/eval/vtsl_wfo_<target>_cells_daily.parquet`
**Run:** `C:\ProgramData\anaconda3\python.exe experiments/2026-09-13_vtsl-extension.py` from repo root

## Bottom line

The validated k=0 rule transfers cleanly to **BXMD** and **PUTR** — same picture as
PUT/BXM: k=0 picked 7/7 folds, tuned OOS ties it, selection clears the random null at
the 99th percentile, PBO < 0.13, CPCV positive in 100% of splits. It does **NOT** transfer
to **CLL**: the collar is not improved by the vol-premium signal, and k-tuning on CLL is
pure noise. CLL is excluded from the promotable set.

| Target | Same-window B&H | Timed k=0 | WFA OOS (tuned = default) | Random-null pct | PBO | CPCV best | 2016+ holdout (k=0) |
|---|---|---|---|---|---|---|---|
| **BXMD** | 0.58 | 0.80 | **0.91** | 0.010 | 0.13 | +0.052, 100% | **1.04** |
| **PUTR** | 0.45 | 0.79 | **0.70** | 0.002 | 0.06 | +0.052, 100% | **0.83** |
| **CLL** | 0.66 | 0.59 | 0.51 (tuned) vs 0.79 (default) | 0.744 (noise) | 0.64 | +0.043, 100% | 0.95 (k=0) / 0.72 (dev-champ k=0.02) |

Expression of the verdict family: BXMD/PUTR = **promotable-forward**, CLL = **do not
time the collar**.

## What was pre-registered (before any number)

- **Transferred rule, no fitting.** k=0 (long-if-SLOPE>0, cash else) came from the
  validated PUT/BXM overlay; BXMD/PUTR/CLL are used only to SCORE it. Per-target window
  = `max(SLOPE start 2008-01-02, target's first dense day)` so a late-launching target
  (CLL 2009) doesn't get credited idle cash it never experienced.
- **Grid** k ∈ {0.00..0.05}, default k=0 (asserted in grid). **No ma family**: it was
  pre-registered and confirmed a strict loss on both validated targets; not re-litigated.
- **Identical machinery** to the PUT/BXM pass: monthly month-end PIT signal, 7
  expanding-train folds (min 5y train), 10,000-path random-grid-selection null (SEED=0),
  PBO + CPCV (embargo-only) on the shared calendar, dev window to 2015-12-31 with a
  2016+ one-shot, power floor ≥ 30 rebalance decisions, net-10bps-per-flip reporting.

## Results

### BXMD (30-delta buywrite) — transfers cleanly

- k=0 chosen by the in-sample argmax **7/7 folds**; tuned OOS == default OOS == **0.91**
  (net-10bps 0.89). Random-null percentile **0.010** — beats 99% of coin-flip selectors.
- PBO **0.13**; CPCV best = default, median +0.052 on 1/3-year slices, positive 100% of
  15 splits. Full-sample fantasy is k=0 at 0.80 (nothing for overfitting to sell).
- 2016+ one-shot: k=0 **1.04** (dev-chosen and transferred identical — no fitting means
  the "dev champion" IS the transferred rule). Same-window B&H 0.58 → timed 0.80.
- Reference: BXMD's prior long-only was 0.71 over the common-SPx sample in the VTSL
  writeup; here on the SLOPE window B&H is 0.58 (rolling expiries / 30-delta mechanics
  shift the common-sample basis — the TIMED numbers are the verdict).

### PUTR (30-delta putwrite) — transfers cleanly, lower absolute OOS

- k=0 7/7 folds; tuned == default == **0.70** (net 0.69). Random-null percentile
  **0.002**. PBO **0.06** (best of the three). CPCV 100% positive.
- 2016+ holdout **0.83** (B&H 0.51). Same-window B&H 0.45 → timed 0.79.
- Same verdict shape as BXMD — the 30-delta putwrite is the weakest absolute
  overlay score of the four validated names (PUT 0.97/BXMD 0.91/— cf. PUT 0.97,
  BXM 0.91, so PUTR's 0.70 is the bottom of the family) but still ~2x its B&H and
  structurally clean.

### CLL (collar) — the honest counter-example: DO NOT time the collar

- Same-window **timed 0.59 does NOT beat B&H 0.66**. The split shows why: pre-2016 the
  overlay HURT (0.15 vs 0.27); 2016+ it adds a hair (0.95 vs 0.93). The signal that
  prices the volatility premium on naked/income strategies does not add it to a
  protective collar, whose economics are equity-beta + cheap protection, not premium
  sales.
- k-selection is **pure noise**: the in-sample argmax drifts to k=0.02 (6/7 folds) and
  its tuned OOS **0.51 falls BELOW the transferred default's 0.79**; random-null
  percentile 0.744; **PBO 0.64**. The one "clean" number (CPCV 100% positive) is
  deceptive here — every cell is a long-collar book and is positive in slices; that is
  the grid's uniform floor, not evidence of selection skill (same phenomenon as the
  PUT/BXM grids, but there the tuned==default at the top of the null).

## Verdict

The VTSL overlay, built for the cash-secured putwrite and covered-call premium, does
not generalize to the collar. Final promotable set: **PUT, BXM, BXMD, PUTR** — all four
validated with the transferred k=0 rule, no tuning won on any grid, all cleared the
full walk-forward gauntlet. CLL is documented as excluded (overlay-on-collar is a
net-negative timing bet and its tuning is noise). The remaining queued work on this
family: the 10%-vol overlay forward paper-trade of the four-name set (the gains trade
vol for tail-risk avoidance, so vol-targeting to a common level is the natural next
step), and a decision on whether the four-name book runs on the strategy-index
replicas as-is.

**File road-map:** script + write-up committed with this pass; JSON/parquet artifacts
are gitignored storage. Note: PUTR's dense series starts 2001 and CLL's 2009 — both
masked per the pre-2007 sparse-tail rule documented in the 2026-09-13 VTSL writeup.