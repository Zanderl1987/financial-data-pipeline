#!/usr/bin/env python3
"""
Forward-optimization / walk-forward + CPCV pass on the in-house futures book
(TSMOM + carry + blend). The financial-data-pipeline-side counterpart of
backtester's catalog/walkforward.py, applied to the two proven signals from
2026-09-12 (TSMOM Sharpe 0.23, carry long-only 0.46).

PRE-REGISTERED (stated before any number is computed, per this repo's
optimizer.py pre-registration discipline):

  Families and grids. Published values are the defaults and MUST appear in
  the grid (a test asserts it, mirroring backtester's "refuses any grid
  missing the published point"):

    family "tsmom":  lookback_months {3,6,12,24} x skip_days {0,31}
                     published = (12, 31)
    family "carry":  min_events {2,3,4,6} x long_short {True,False}
                     published = (3, False)   (False == long-only, the 0.46 line)
    family "blend":  carry_w {0.00,0.25,0.50,0.75,1.00}  (weight on carry leg,
                     weight on tsmom leg = 1-carry_w; component legs use the
                     published TSMOM and published long-only carry)
                     published = 0.50

  Non-tunables (fixed at published values, stated once):
    vol_target 0.40, vol_floor 0.10. The floor was documented 2026-09-12 as a
    PIT-safe predetermined cap against the flat-price glitch windows; tuning
    it would data-snoop the exact thing it protects.
    Roll handling: headline convention (excise detected roll days) fixed.
    Objective: gross Sharpe. No cost assumptions in SEARCH; 10bps-netted
    Sharpe reported for the surviving pair so costs can't silently flip a
    verdict.

QUESTIONS (mirror backtester WALKFORWARD.md):
  Q1 Did tuning help? tuned WFA OOS vs default-params WFA OOS.
  Q2 Did SELECTION help? tuned WFA OOS vs the SAME walk-forward driven by
     RANDOM grid choices (10,000 paths). A walk-forward that makes money
     proves very little -- the tuned OOS must clear what a coin-flip
     selector would produce on the same grid, folds and returns.
  Q3 The full-sample fantasy: best-on-everything picked AND scored on the
     whole sample -- what overfitting would have sold you.
  Q4 PBO + CPCV stability of the best cell AND of the published default on
     the shared full-sample calendar (combinatorial purged CV, embargo-only;
     these are realized daily P&L series, not labels with an t1 to purge
     against).

Power floor (backtester family-2 protocol): a winner must rest on >= 30
independent rebalance decisions -- dates the target weight vector changes --
or its OOS is reported as underpowered, not believed.

Trial bookkeeping: every trial (each (family, params) x each fold) is recorded
in the JSON artifact this script writes. Trials are deliberately NOT unioned
into the shared evaluation registry: the registry seeds the TV campaign's
deflated-Sharpe population, and this is a separate research program on a
separate asset class (futures), same rule as backtester's family 2 (results
never pooled).

Run: C:\\ProgramData\\anaconda3\\python.exe experiments/2026-09-13_futures-forward-optimization.py
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
from experiments import _tsmom_core as core  # noqa: E402

ANN = core.ANN
DEV_END = pd.Timestamp("2016-01-01")     # dev window ends 2015-12-31
N_FOLDS = 7
MIN_TRAIN_DAYS = 7 * 252                 # expanding train; first fold trains 1997->~2006
N_RANDOM_PATHS = 10_000
SEED = 0
OUT_DIR = "storage/reports/eval"


# ------------------------------------------------------------------ published builders
# Reuse the published construction; the TSMOM builder is re-parameterized to
# expose the lookback horizon but asserts it reproduces _tsmom_core exactly at
# the published (12, 31).


def build_tsmom_positions(close: pd.DataFrame, months: float = 12.0,
                          skip_days: int = 31, vol_target: float = core.VOL_TARGET,
                          vol_floor: float = 0.10) -> pd.DataFrame:
    """Month-end sign-of-momentum positions, vol-scaled. Generalizes
    _tsmom_core.build_positions with a lookback_horizon in months; at
    months=12, skip_days=31 reproduces it exactly (horizon = 366 days)."""
    days = np.asarray(close.index.to_pydatetime())
    ret = close.pct_change()
    vol = ret.rolling(core.LOOKBACK).std(ddof=1) * math.sqrt(ANN)
    vol = vol.clip(lower=vol_floor)

    anchors = core.month_end_anchors(close.index)
    horizon_d = round(366.0 * months / 12.0)
    skip_d = dt.timedelta(days=skip_days)
    look_d = dt.timedelta(days=skip_days + horizon_d)
    pos = pd.DataFrame(0.0, index=anchors, columns=close.columns)
    for t0 in anchors:
        ie = core.asof_idx(days, t0 - skip_d)
        ist = core.asof_idx(days, t0 - look_d)
        if ist < 0 or ie < 0:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            mom = np.log(close.iloc[ie] / close.iloc[ist])
        v = vol.loc[t0]
        for sym in close.columns:
            m, vv = mom.get(sym, 0.0), v.get(sym)
            if m == 0 or not np.isfinite(m) or not np.isfinite(vv) or vv <= 0:
                continue
            scale = VOL_TARGET / vv if vol_target is not None else 1.0
            pos.at[t0, sym] = math.copysign(scale, m)
    return pos


VOL_TARGET = core.VOL_TARGET
if __name__ == "__main__":
    _close0 = core.load_close_wide()
    _a = core.build_positions(_close0, skip_days=31)
    _b = build_tsmom_positions(_close0, months=12.0, skip_days=31)
    pd.testing.assert_frame_equal(_a, _b)
    print(f"[guard] build_tsmom_positions(12,31) == _tsmom_core.build_positions: OK "
          f"(max abs diff {( _a - _b).abs().max().max():.2e})")


def load_carry_module():
    """Load the 2026-09-12 carry experiment module so the carry / blend legs
    are the SAME published builders, not copies."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo_root, "experiments"))
    path = os.path.join(repo_root, "experiments", "2026-09-12_carry_futures.py")
    spec = importlib.util.spec_from_file_location("carry_exp_09212", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_carry_scores(mod, close: pd.DataFrame, open_: pd.DataFrame,
                       rm: pd.DataFrame, min_events: int = 3) -> pd.DataFrame:
    return mod.build_carry(close, open_, rm, min_events=min_events)


def build_carry_positions_wrap(pos_builder, scores: pd.DataFrame, close: pd.DataFrame,
                               long_short: bool = False) -> pd.DataFrame:
    return pos_builder(scores, close, long_short=long_short)


def build_blend_positions(mod, scores: pd.DataFrame, close: pd.DataFrame,
                          carry_w: float = 0.5) -> pd.DataFrame:
    mom = build_tsmom_positions(close, months=12.0, skip_days=31)
    car = mod.build_carry_positions(scores, close, long_short=False)
    return (mom * (1.0 - carry_w) + car.reindex(mom.index).fillna(0.0) * carry_w)


# ------------------------------------------------------------------ machinery


def ann_sharpe(ret: "pd.Series | np.ndarray") -> float:
    r = pd.Series(ret).dropna()
    if len(r) < 3 or float(r.std(ddof=1)) == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1) * math.sqrt(ANN))


def rebalance_decisions(pos: pd.DataFrame, close: pd.DataFrame,
                        d0: pd.Timestamp, d1: pd.Timestamp) -> int:
    """Dates in [d0,d1] on which the held weight vector changed -- the count of
    INDEPENDENT rebalance choices (backtester family-2 power floor)."""
    held = pos.reindex(close.index, method="ffill").shift(1).fillna(0.0)
    win = held[(held.index >= d0) & (held.index <= d1)]
    moved = (win != win.shift(1)).any(axis=1).sum()
    return int(moved)


def family_specs(carry_mod, close, scores_by_me, open_, rm):
    """Each family: cells (label, params), the published default, and a builder
    mapping params -> position frame. Grids must include the defaults (asserted)."""
    specs = {}

    tsmom_cells = list(itertools.product([3, 6, 12, 24], [0, 31]))
    specs["tsmom"] = {
        "cells": [(f"months={m},skip={s}", {"months": float(m), "skip_days": int(s)})
                  for m, s in tsmom_cells],
        "default": (f"months=12,skip=31", {"months": 12.0, "skip_days": 31}),
        "builder": lambda close, p: build_tsmom_positions(close, **p),
    }

    carry_cells = list(itertools.product([2, 3, 4, 6], [False, True]))
    specs["carry"] = {
        "cells": [(f"min_ev={m},ls={b}", {"min_events": int(m), "long_short": bool(b)})
                  for m, b in carry_cells],
        "default": (f"min_ev=3,ls=False", {"min_events": 3, "long_short": False}),
        "builder": lambda close, p: carry_mod.build_carry_positions(
            scores_by_me[p["min_events"]], close, long_short=p["long_short"]),
    }

    blend_cells = [(f"w={w:g}", {"carry_w": float(w)}) for w in (0.0, 0.25, 0.5, 0.75, 1.0)]
    specs["blend"] = {
        "cells": blend_cells,
        "default": (f"w=0.5", {"carry_w": 0.5}),
        "builder": lambda close, p: build_blend_positions(carry_mod,
                                                          scores_by_me[3], close,
                                                          carry_w=p["carry_w"]),
    }

    for name, sp in specs.items():
        assert any(dict(c[1]) == dict(sp["default"][1]) for c in sp["cells"]), \
            f"grid for '{name}' missing published default {sp['default'][0]}"
    return specs


def run_family(name, sp, close, ret_clean):
    dates = close.index
    cells = sp["cells"]
    defaults = sp["default"]
    n_cells = len(cells)

    full_series = []
    positions = {}
    for label, params in cells:
        pos = sp["builder"](close, params)
        positions[label] = pos
        pr = core.portfolio_returns(pos, close, ret=ret_clean)
        src = pr["gross"]
        full_series.append((label, params, src, pr))
    # sort for full-sample matrix (columns ordered 0..n_cells-1 by cell order)
    labels = [c[0] for c in cells]

    oos_dates = dates[MIN_TRAIN_DAYS:]
    ticks = np.array_split(np.arange(len(oos_dates)), N_FOLDS)
    folds_dates = [oos_dates[t] for t in ticks]

    # per-fold per-cell IS Sharpe (train = everything strictly before the fold's
    # test chunk) and per-cell test slices.
    is_sharpe = np.full((N_FOLDS, n_cells), np.nan)
    test_slices = [[None] * n_cells for _ in range(N_FOLDS)]
    fold_rows = []
    for fi, chunk in enumerate(folds_dates):
        train_end = chunk[0]
        for ci, (label, params, src, pr) in enumerate(full_series):
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
            path_cells.append(None)          # no scorable trail: fold contributes nothing
            continue
        path_cells.append(int(np.nanargmax(row)))

    def stitch(path_cells_i, net_bps: "float | None" = None):
        pieces = []
        for fi, ci in enumerate(path_cells_i):
            if ci is None:
                continue
            src = test_slices[fi][ci]
            if net_bps is not None:
                src = src - (net_bps / 1e4)
            pieces.append(src)
        if not pieces:
            return pd.Series(dtype=float)
        return pd.concat(pieces).dropna()

    # net-of-cost at 10bps one-way uses portfolio_returns' turnover-scaled
    # net_10bps column (Cost = per-day |delta weight| summed across the book,
    # charged per unit, divided by active count) -- NOT a flat daily haircut.
    net10_frames = [pr["net_10bps"] for _, _, _, pr in full_series]

    def stitch_net(path_cells_i):
        pieces = []
        for fi, ci in enumerate(path_cells_i):
            if ci is None:
                continue
            chunk = folds_dates[fi]
            s = net10_frames[ci]
            s = s[(s.index >= chunk[0]) & (s.index <= chunk[-1])]
            pieces.append(s)
        if not pieces:
            return pd.Series(dtype=float)
        return pd.concat(pieces).dropna()

    tuned = stitch(path_cells)
    tuned_net10 = stitch_net(path_cells)
    default_ci = next(i for i, (lb, p) in enumerate(cells)
                      if dict(p) == dict(defaults[1]))
    default_path = [default_ci] * N_FOLDS
    default_oos = stitch(default_path)
    default_net10 = stitch_net(default_path)

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
    fantasy = {"params_label": labels[best_full],
               "whole_period_sharpe": float(full_sh[best_full]),
               "note": "picked AND scored on the whole sample"}

    # PBO + CPCV on the shared full-sample calendar (all cells that have data).
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
        return {"params_label": labels[ci], "sharpe": ann_sharpe(s),
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
        dec_counts.append(rebalance_decisions(positions[labels[ci]], close,
                                              chunk[0], chunk[-1]))
    n_decisions = int(sum(dec_counts))

    result = {
        "family": name,
        "cells": [lbl for lbl, _ in cells],
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
                      "sharpe_net10bps": ann_sharpe(tuned_net10)},
        "default_oos": {"sharpe": ann_sharpe(default_oos),
                        "n_days": int(len(default_oos)),
                        "n_rebalance_decisions": int(sum(
                            rebalance_decisions(positions[defaults[0]], close,
                                                folds_dates[fi][0], folds_dates[fi][-1])
                            for fi in range(N_FOLDS))),
                        "power_ok": None,
                        "sharpe_net10bps": ann_sharpe(default_net10)},
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
    close = core.load_close_wide()
    open_ = core.load_open_wide().reindex(close.index)
    rm = core.roll_mask()
    ret_clean = core.clean_returns(mask_rolls=True)
    print(f"loaded {close.shape[1]} symbols, {close.index.min().date()} -> "
          f"{close.index.max().date()}, {len(close)} days")

    carry_mod = load_carry_module()
    scores_by_me = {m: carry_mod.build_carry(close, open_, rm, min_events=m)
                    for m in (2, 3, 4, 6)}

    specs = family_specs(carry_mod, close, scores_by_me, open_, rm)
    print(f"pre-registered grids validated (all contain the published default); "
          f"roll-masked gross, expanding train {MIN_TRAIN_DAYS//252}y, "
          f"{N_FOLDS} test folds, {N_RANDOM_PATHS} random paths\n")

    master = {"run": "2026-09-13_futures-forward-optimization",
              "data": f"{close.index.min().date()} -> {close.index.max().date()}",
              "n_symbols": close.shape[1], "n_folds": N_FOLDS,
              "min_train_days": MIN_TRAIN_DAYS, "n_random_paths": N_RANDOM_PATHS,
              "families": {}}
    daily = {}
    for name, sp in specs.items():
        result, full_series = run_family(name, sp, close, ret_clean)
        master["families"][name] = result
        daily[name] = full_series
        print(f"=== family: {name} ===")
        for line in result["verdict"]:
            print("  " + line)
        print()

    os.makedirs(OUT_DIR, exist_ok=True)
    jpath = os.path.join(OUT_DIR, "futures_forward_optimization_20260913.json")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(master, fh, indent=2, cls=_JsonEnc)
    print(f"\nwrote {jpath}")

    for name, fser in daily.items():
        df = pd.DataFrame({lbl: s for lbl, _, s, _ in fser})
        df.to_parquet(os.path.join(OUT_DIR, f"futures_wfo_{name}_cells_daily.parquet"))
    print("wrote storage/reports/eval/futures_wfo_<family>_cells_daily.parquet")


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