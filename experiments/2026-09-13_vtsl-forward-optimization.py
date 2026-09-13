#!/usr/bin/env python3
"""
Forward-optimization / walk-forward + CPCV pass on the VIX term-structure-slope
(VTSL) overlay from 2026-09-13 (experiments/2026-09-13_vix-term-structure.py).
The financial-data-pipeline-side counterpart of backtester's
catalog/walkforward.py, applied to the one signal that survived its first test
(PUT/BXM long-if-SLOPE>0, k=0 a-priori: PUT 0.56->0.87 same-sample, 0.66->1.11
OOS 2017-05+).

PRE-REGISTERED (stated before any number is computed, per this repo's
optimizer.py pre-registration discipline):

  Series definition (fixed once). SLOPE = OLS beta of ln(implied-vol index
  level) on maturity in months across {VIX9D:0.30, VIX:1.0, VIX3M:3.0,
  VIX6M:6.0} (>=2 points, curve usable from 2008-08). Daily realized P&L of a
  cell = target_index_daily_return x effective position, where the position is
  decided at the prior month-end close and held from t0+1 (the SAME
  month_positions() construction as the published experiment -> the published
  result is exactly the k=0 default of family

  Families and grids. The published value is the default and MUST appear in
  the grid (a test asserts it, mirroring backtester's "refuses any grid
  missing the published point"):

    family "<tgt>.k":  threshold k on SLOPE for long-if-SLOPE>k, cash else.
                       k in {0.00, 0.01, 0.02, 0.03, 0.04, 0.05}
                       published = 0.00
    family "<tgt>.ma": confirmation months m for long-if-SLOPE>0 AND SLOPE
                       above its m-month rolling mean. m in {0, 3, 6, 12, 24}
                       published = 0  (m=0 IS the plain k=0 rule; m>0 adds a
                       trend-confirmation filter and is a strict subset of long
                       days)

  Target assets (the two vol-selling strategies the overlay was validated on):
    PUT, BXM. Four families total: put.k, put.ma, bxm.k, bxm.ma.

  Non-tunables (fixed, stated once):
    P&L series trimmed to the SLOPE-active window (SLOPE.first_valid_index()
    onward) so Sharpe comparisons are over the signal's own sample, not years
    of idle cash. (The published 0.87/0.67 numbers included a short pre-signal
    zero-dilution tail; the k=0 guard below reports the trimmed values and a
    reproduction floor is asserted.)
    Objective: gross Sharpe. No cost assumptions in SEARCH; a NET 10bps one-way
    fee is charged per position flip (entry and exit both 10bps, position is
    all-in-or-cash so |d held| x 10bps/day when it moves; zero slippage
    assumed) and reported for tuned + default OOS so costs can't silently flip
    a verdict. NOTE: this placeholder is NOT a live option-writing cost model
    (Cboe indices embed signing-level costs only; live-cost load is a separate
    queued follow-up).

QUESTIONS (mirror backtester WALKFORWARD.md):
  Q1 Did tuning help? tuned WFA OOS vs default-params (k=0 / m=0) WFA OOS.
  Q2 Did SELECTION help? tuned WFA OOS vs the SAME walk-forward driven by
     RANDOM grid choices (10,000 paths). A walk-forward that makes money
     proves very little -- the tuned OOS must clear what a coin-flip
     selector would produce on the same grid, folds and returns.
  Q3 The full-sample fantasy: best-on-everything picked AND scored on the
     whole sample -- what overfitting would have sold you.
  Q4 PBO + CPCV stability of the best cell AND of the published default on
     the shared full-sample calendar (combinatorial purged CV, embargo-only;
     these are realized daily P&L series, not labels with a t1 to purge).

Power floor (backtester family-2 protocol): a winner must rest on >= 30
independent rebalance decisions -- dates the held position changes -- or its
OOS is reported as underpowered, not believed. (Monthly month-end signals x ~18y
puts k=0 comfortably above 30; a cell that barely ever flips may not.)

Trial bookkeeping: every trial (each (family, params) x each fold) is recorded
in the JSON artifact this script writes. Trials are deliberately NOT unioned
into the shared evaluation registry: the registry seeds the TV campaign's
deflated-Sharpe population, and this is a separate research program on a
separate asset class (Cboe strategy indices), same rule as backtester's family
2 (results never pooled).

Determinism: RNG seeded (SEED=0); argmax ties broken by first-in-grid order;
month_positions() is the published PIT construction.

Run: C:\\ProgramData\\anaconda3\\python.exe experiments/2026-09-13_vtsl-forward-optimization.py
"""
import datetime as dt
import importlib.util
import itertools
import json
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ANN = 252
DEV_END = pd.Timestamp("2016-01-01")     # dev window ends 2015-12-31
N_FOLDS = 7
MIN_TRAIN_DAYS = 5 * 252                 # expanding train; first fold trains 2008-08->~2013-05
N_RANDOM_PATHS = 10_000
SEED = 0
FAIR_BPS = 10                            # one-way fee per position flip, net reporting only
OUT_DIR = "storage/reports/eval"

K_GRID = [0.00, 0.01, 0.02, 0.03, 0.04, 0.05]
MA_GRID = [0, 3, 6, 12, 24]
ASSETS = ["PUT", "BXM"]


def load_vtsl_module():
    """Load the published VTSL experiment module so the SLOPE builder and the
    month_positions() construction are the SAME published code, not copies."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo_root, "experiments"))
    path = os.path.join(repo_root, "experiments", "2026-09-13_vix-term-structure.py")
    spec = importlib.util.spec_from_file_location("vtsl_0913", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ann_sharpe(ret) -> float:
    r = pd.Series(ret).dropna()
    if len(r) < 3 or float(r.std(ddof=1)) == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1) * math.sqrt(ANN))


def rebalance_decisions(pos: pd.Series, dates: pd.DatetimeIndex,
                        d0: pd.Timestamp, d1: pd.Timestamp) -> int:
    """Dates in [d0,d1] on which the held position changed -- the count of
    INDEPENDENT rebalance choices (backtester family-2 power floor).
    pos already carries the effective position during the day's return
    (month_positions applies the t0+1 shift internally)."""
    held = pos.reindex(dates).fillna(0.0)
    win = held[(held.index >= d0) & (held.index <= d1)]
    moved = (win != win.shift(1)).sum()
    return int(moved)


def net_of_fee(src: pd.Series, pos: pd.Series, dates: pd.DatetimeIndex,
               bps: float = FAIR_BPS) -> pd.Series:
    """All-in/cash book: one-way fee charged per unit of position change, so a
    flip (0<->1) costs 2x bps total (entry+exit). PIT, deterministic."""
    held = pos.reindex(dates).fillna(0.0)
    fee = (bps / 1e4) * held.diff().abs().fillna(0.0)
    return src - fee


def family_specs(slope, ret, dates):
    """Families: k (SLOPE threshold) and ma (confirmation months) per target
    asset. Each cell -> effective-position builder -> daily P&L. Grids must
    include the published default (asserted)."""
    specs = {}

    def k_builder(tgt):
        def b(params):
            return mod.month_positions(slope, ret[tgt], threshold=params["k"])
        return b

    def ma_builder(tgt):
        def b(params):
            m = params["m"]
            if m == 0:
                sig = (slope > 0.0).astype(float)
            else:
                sig = ((slope > 0.0) & (slope > slope.rolling(m).mean())).astype(float)
            return mod.month_positions(sig, ret[tgt], threshold=0.0)
        return b

    for tgt in ASSETS:
        k_cells = [(f"k={k:g}", {"k": float(k)}) for k in K_GRID]
        specs[f"{tgt}.k"] = {
            "cells": k_cells,
            "default": ("k=0", {"k": 0.0}),
            "target": tgt,
            "builder": k_builder(tgt),
        }
        ma_cells = [(f"ma={m}", {"m": int(m)}) for m in MA_GRID]
        specs[f"{tgt}.ma"] = {
            "cells": ma_cells,
            "default": ("ma=0", {"m": 0}),
            "target": tgt,
            "builder": ma_builder(tgt),
        }

    for name, sp in specs.items():
        assert any(dict(c[1]) == dict(sp["default"][1]) for c in sp["cells"]), \
            f"grid for '{name}' missing published default {sp['default'][0]}"
    return specs


def run_family(name, sp, ret, dates):
    cells = sp["cells"]
    defaults = sp["default"]
    n_cells = len(cells)

    full_series = []
    positions = {}
    for label, params in cells:
        pos = sp["builder"](params)
        positions[label] = pos
        src = (ret[sp["target"]] * pos).reindex(dates).fillna(0.0)
        full_series.append((label, params, src, pos))

    oos_dates = dates[MIN_TRAIN_DAYS:]
    ticks = np.array_split(np.arange(len(oos_dates)), N_FOLDS)
    folds_dates = [oos_dates[t] for t in ticks]

    is_sharpe = np.full((N_FOLDS, n_cells), np.nan)
    test_slices = [[None] * n_cells for _ in range(N_FOLDS)]
    fold_rows = []
    for fi, chunk in enumerate(folds_dates):
        train_end = chunk[0]
        for ci, (label, params, src, pos) in enumerate(full_series):
            train_part = src[src.index < train_end]
            is_sharpe[fi, ci] = ann_sharpe(train_part)
            test_slices[fi][ci] = src[(src.index >= chunk[0]) &
                                      (src.index <= chunk[-1])]
        fold_rows.append({"fi": fi, "d0": str(chunk[0].date()),
                          "d1": str(chunk[-1].date())})

    # tuned path: argmax IS per fold (first wins ties -> deterministic)
    path_cells = []
    for fi in range(N_FOLDS):
        row = is_sharpe[fi]
        valid = np.isfinite(row)
        if not valid.any():
            path_cells.append(None)
            continue
        path_cells.append(int(np.nanargmax(row)))

    def stitch(path_cells_i):
        pieces = []
        for fi, ci in enumerate(path_cells_i):
            if ci is None:
                continue
            pieces.append(test_slices[fi][ci])
        if not pieces:
            return pd.Series(dtype=float)
        return pd.concat(pieces).dropna()

    net_frames = [(pos, src) for _, _, src, pos in full_series]

    def stitch_net(path_cells_i):
        pieces = []
        for fi, ci in enumerate(path_cells_i):
            if ci is None:
                continue
            chunk = folds_dates[fi]
            pos, src = net_frames[ci]
            n = net_of_fee(src, pos, dates)
            n = n[(n.index >= chunk[0]) & (n.index <= chunk[-1])]
            pieces.append(n)
        if not pieces:
            return pd.Series(dtype=float)
        return pd.concat(pieces).dropna()

    tuned = stitch(path_cells)
    tuned_net = stitch_net(path_cells)
    default_ci = next(i for i, (lb, p) in enumerate(cells)
                      if dict(p) == dict(defaults[1]))
    default_path = [default_ci] * N_FOLDS
    default_oos = stitch(default_path)
    default_net = stitch_net(default_path)

    # random-selection null: same folds, same returns, RANDOM cell per fold.
    test_arr = [[(s.to_numpy(dtype=float) if s is not None else None)
                 for s in test_slices[fi]] for fi in range(N_FOLDS)]
    rng = np.random.default_rng(SEED)
    null_sharpe = np.empty(N_RANDOM_PATHS)
    for p in range(N_RANDOM_PATHS):
        picks = [int(rng.integers(0, n_cells)) for _ in range(N_FOLDS)]
        arrs = [test_arr[fi][picks[fi]] for fi in range(N_FOLDS)]
        x = np.concatenate([a for a in arrs if a is not None])
        null_sharpe[p] = ann_sharpe(x)
    tuned_sh = ann_sharpe(tuned)
    p_random = float((null_sharpe >= tuned_sh).mean()) if np.isfinite(tuned_sh) else None

    # full-sample fantasy: argmax on everything.
    full_sh = np.array([ann_sharpe(src) for _, _, src, _ in full_series])
    best_full = int(np.nanargmax(full_sh))
    fantasy = {"params_label": [c[0] for c in cells][best_full],
               "whole_period_sharpe": float(full_sh[best_full]),
               "note": "picked AND scored on the whole sample"}

    # PBO + CPCV on the shared full-sample calendar (all cells on same dates).
    matrix = pd.concat([src.rename(lb) for lb, _, src, _ in full_series],
                       axis=1, sort=True).dropna(how="any")
    from evaluation.robustness import pbo as _pbo
    from evaluation.optimizer import cpcv_stability as _cpcv
    pbo_res = _pbo(matrix)
    cpcv_best = _cpcv(matrix, best_full)
    cpcv_default = _cpcv(matrix, default_ci)

    # dev-selected one-shot: argmax on dev (through 2015-12-31) only, then
    # evaluate that single param set on 2016+ once.
    dev_shs = np.array([ann_sharpe(src[src.index < DEV_END])
                        for _, _, src, _ in full_series])
    dev_ci = int(np.nanargmax(dev_shs)) if np.isfinite(dev_shs).any() else None
    hod = dates[dates >= DEV_END]

    def one_shot(ci):
        if ci is None or hod.empty:
            return None
        s = full_series[ci][2]
        s = s[s.index >= DEV_END].dropna()
        if len(s) < 20:
            return None
        return {"params_label": [c[0] for c in cells][ci], "sharpe": ann_sharpe(s),
                "n_days": int(len(s))}

    holdout_dev_champ = one_shot(dev_ci)
    holdout_default = one_shot(default_ci)

    # rebalance-decision power floor on the tuned winner OOS.
    dec_counts = []
    for fi, ci in enumerate(path_cells):
        if ci is None:
            dec_counts.append(0)
            continue
        chunk = folds_dates[fi]
        dec_counts.append(rebalance_decisions(positions[[c[0] for c in cells][ci]],
                                              dates, chunk[0], chunk[-1]))
    n_decisions = int(sum(dec_counts))

    labels = [c[0] for c in cells]
    result = {
        "family": name,
        "target": sp["target"],
        "cells": labels,
        "default": defaults[0],
        "n_folds": N_FOLDS,
        "folds": fold_rows,
        "per_fold_chosen": [
            {"fold": fi, "chosen": None if ci is None else labels[ci],
             "train_sharpe": None if ci is None else float(is_sharpe[fi, ci]),
             "test_sharpe": None if ci is None else ann_sharpe(test_slices[fi][ci]),
             "default_test_sharpe": ann_sharpe(test_slices[fi][default_ci])}
            for fi, ci in enumerate(path_cells)],
        "n_folds_chose_published": int(sum(
            1 for fi, ci in enumerate(path_cells)
            if ci is not None and labels[ci] == defaults[0])),
        "tuned_oos": {"sharpe": tuned_sh,
                      "n_days": int(len(tuned)),
                      "n_rebalance_decisions": n_decisions,
                      "power_ok": bool(n_decisions >= 30),
                      "sharpe_net10bps": ann_sharpe(tuned_net)},
        "default_oos": {"sharpe": ann_sharpe(default_oos),
                        "n_days": int(len(default_oos)),
                        "n_rebalance_decisions": int(sum(
                            rebalance_decisions(positions[defaults[0]], dates,
                                                folds_dates[fi][0], folds_dates[fi][-1])
                            for fi in range(N_FOLDS))),
                        "power_ok": None,
                        "sharpe_net10bps": ann_sharpe(default_net)},
        "random_null": {"n_paths": N_RANDOM_PATHS,
                        "median": float(np.nanmedian(null_sharpe)),
                        "p90": float(np.nanpercentile(null_sharpe, 90)),
                        "p95": float(np.nanpercentile(null_sharpe, 95)),
                        "p99": float(np.nanpercentile(null_sharpe, 99)),
                        "max": float(np.nanmax(null_sharpe)),
                        "tuned_oos_percentile_of_random": p_random},
        "fantasy": fantasy,
        "pbo": {"pbo": pbo_res.get("pbo"), "reason": pbo_res.get("pbo_reason", ""),
                "n_configurations": pbo_res.get("n_configurations")},
        "cpcv_best": cpcv_best,
        "cpcv_default": cpcv_default,
        "holdout_2016plus": {"dev_selected_champion": holdout_dev_champ,
                             "published_default": holdout_default},
        "verdict": verdict(name, tuned_sh, default_oos, p_random, n_decisions,
                           pbo_res, cpcv_best, cpcv_default, labels, best_full,
                           full_sh, default_ci),
    }
    return result, full_series


def verdict(name, tuned_sh, default_oos, p_random, n_decisions,
            pbo_res, cpcv_best, cpcv_default, labels, best_full, full_sh,
            default_ci):
    out = []
    dsh = ann_sharpe(default_oos)
    if not np.isfinite(tuned_sh) or not np.isfinite(dsh):
        return ["NO SCORABLE TUNED OOS CHAMPION -- every fold failed its IS window."]
    rel = "BEATS" if tuned_sh > dsh else ("TIES" if abs(tuned_sh - dsh) < 1e-9 else "does NOT beat")
    out.append(f"tuned OOS {tuned_sh:.2f} vs published-default OOS {dsh:.2f} -- tuning {rel} published")
    out.append(f"random-grid-selection null percentile of tuned OOS: {p_random:.3f} "
               f"(0.95 = tuned only beats top-5% of coin-flip selectors)")
    if p_random > 0.50:
        out.append("SELECTION DID NOT HELP: a coin-flip selector from the same grid "
                   "does at least as well >50% of the time")
    elif p_random <= 0.05:
        out.append("SELECTION LIKELY HELPED: tuned OOS beats >=95% of random selectors "
                   "(best-of-N caveat applies)")
    else:
        out.append("SELECTION INCONCLUSIVE: tuned OOS inside the middle of the "
                   "random-selector distribution")
    if n_decisions < 30:
        out.append(f"WARNING: tuned winner rests on only {n_decisions} independent "
                   f"rebalance decisions (< floor 30) -- treat as underpowered")
    b = pbo_res.get("pbo")
    if b is None:
        out.append(f"PBO unavailable ({pbo_res.get('pbo_reason', '?')})")
    elif b >= 0.4:
        out.append(f"PBO {b} -- picking by IS rank barely generalizes; winner is noise-prone")
    else:
        out.append(f"PBO {b} -- selection by IS rank generalizes acceptably (caveat: "
                   f"small N={pbo_res.get('n_configurations')})")
    cmed = cpcv_best.get("cpcv_oos_sharpe_median")
    if cmed is None:
        out.append(f"CPCV(best) unavailable ({cpcv_best.get('cpcv_reason', '?')})")
    elif cmed <= 0 or cpcv_best.get("cpcv_pct_positive", 100.0) < 60.0:
        out.append(f"CPCV(best {labels[best_full]} IS {full_sh[best_full]:.2f}) OOS median {cmed} "
                   f"({cpcv_best.get('cpcv_pct_positive')}% positive) -- not stable fold-to-fold")
    else:
        out.append(f"CPCV(best {labels[best_full]} IS {full_sh[best_full]:.2f}) OOS median {cmed} "
                   f"({cpcv_best.get('cpcv_pct_positive')}% positive) -- reasonably stable")
    dmed = cpcv_default.get("cpcv_oos_sharpe_median")
    if dmed is None:
        out.append(f"CPCV(default) unavailable ({cpcv_default.get('cpcv_reason', '?')})")
    else:
        out.append(f"CPCV(default {labels[default_ci]}) OOS median {dmed} "
                   f"({cpcv_default.get('cpcv_pct_positive')}% positive)")
    return out


# ------------------------------------------------------------------ run


def main() -> None:
    global mod
    mod = load_vtsl_module()

    df = mod.load_daily()
    slope = mod.daily_slope(df[["VIX9D", "VIX", "VIX3M", "VIX6M"]], mod.VOL_SERIES)
    ret = df.pct_change()
    start = slope.first_valid_index()
    dates = ret.index[ret.index >= start]
    print(f"SLOPE-active sample: {start.date()} -> {dates[-1].date()} "
          f"({len(dates)} days); target assets {ASSETS}; grids "
          f"k={K_GRID}, ma={MA_GRID} (defaults k=0 / ma=0 = the published rule)")

    # published-reproduction guard (k=0, active-window basis)
    for tgt, floor in (("PUT", 0.82), ("BXM", 0.62)):
        pos0 = mod.month_positions(slope, ret[tgt], threshold=0.0)
        src0 = (ret[tgt] * pos0).reindex(dates).fillna(0.0)
        sh = ann_sharpe(src0)
        print(f"[guard] {tgt} published k=0 active-window Sharpe = {sh:.3f} "
              f"(published 0.87 / 0.67 same-sample; reproduction floor {floor})")
        assert sh >= floor, f"{tgt} published k=0 reproduction FAILED ({sh:.3f} < {floor})"

    # reference: buy-and-hold (no timing) on the same window
    print("\nreference (active window, no timing):")
    for tgt in ASSETS:
        bh = ann_sharpe(ret[tgt].reindex(dates))
        print(f"  {tgt} B&H Sharpe {bh:.2f}")

    specs = family_specs(slope, ret, dates)
    print(f"pre-registered grids validated (all contain the published default); "
          f"expanding train {MIN_TRAIN_DAYS//252}y, {N_FOLDS} test folds, "
          f"{N_RANDOM_PATHS} random paths, SEED={SEED}\n")

    master = {"run": "2026-09-13_vtsl-forward-optimization",
              "data": f"{dates[0].date()} -> {dates[-1].date()}",
              "n_days": len(dates), "n_folds": N_FOLDS,
              "min_train_days": MIN_TRAIN_DAYS, "n_random_paths": N_RANDOM_PATHS,
              "fee_bps_one_way": FAIR_BPS,
              "note": "net reporting = 10bps one-way per position flip "
                      "(not a live option-writing cost model; that load is a "
                      "separate queued follow-up)",
              "families": {}}
    daily = {}
    for name, sp in specs.items():
        result, full_series = run_family(name, sp, ret, dates)
        master["families"][name] = result
        daily[name] = full_series
        print(f"=== family: {name} (target {sp['target']}) ===")
        for line in result["verdict"]:
            print("  " + line)
        print()

    os.makedirs(OUT_DIR, exist_ok=True)
    jpath = os.path.join(OUT_DIR, "vtsl_forward_optimization_20260913.json")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(master, fh, indent=2, cls=_JsonEnc)
    print(f"\nwrote {jpath}")

    for name, fser in daily.items():
        dfout = pd.DataFrame({lbl: s for lbl, _, s, _ in fser})
        dfout.to_parquet(os.path.join(OUT_DIR, f"vtsl_wfo_{name}_cells_daily.parquet"))
    print("wrote storage/reports/eval/vtsl_wfo_<family>_cells_daily.parquet")


class _JsonEnc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, pd.Timestamp):
            return o.isoformat()
        return super().default(o)


if __name__ == "__main__":
    main()