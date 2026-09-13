#!/usr/bin/env python3
"""
Extend the VTSL overlay (2026-09-13_vix-term-structure.py) to the remaining
sellable-vol strategies: BXMD (30-delta buywrite), PUTR (30-delta putwrite),
CLL (collar). PUT and BXM already cleared the forward-optimization loop with
the published k=0 rule (2026-09-13_vtsl-forward-optimization.py, committed
486113b); this pass runs the SAME walk-forward gauntlet on the three new
targets.

PRE-REGISTERED (before any number):

  Signal: SLOPE (OLS beta of ln(implied-vol level) on maturity-months across
  VIX9D/VIX/VIX3M/VIX6M), the published construction; overlay rule k=0
  (long-if-SLOPE>0, cash else) is TRANSFERRED a-priori from the validated
  PUT/BXM overlay -- the new targets are used only to score the transferred
  rule, never to fit it.

  Evaluation window (fixed once, honest per target): dates =
  max(SLOPE.first_valid_date(), target's first dense day) .. 2026-09-11.
  A target that launches after the slope window (CLL 2009) is not credited
  0-return months before it existed.

  Grid: k in {0.00, 0.01, 0.02, 0.03, 0.04, 0.05}, default k=0. NO ma
  (confirmation-months) family: it was pre-registered and confirmed a strict
  loss on both validated targets (forward-opt verdict; default beat the tuned
  ma in PUT and BXM) and is not re-litigated here.

  Non-tunables: monthly month-end signal -> position t0+1 (published
  month_positions()); gross Sharpe objective; 7 expanding-train folds
  (min 5y train, ~1.9y test each); 10,000-path random-grid-selection null;
  PBO + CPCV (embargo-only) on the shared full-sample calendar; dev window
  ends 2015-12-31 with a 2016+ single-shot holdout (descriptive, spent once);
  power floor >= 30 independent rebalance decisions. Costs: NET 10bps one-way
  per position flip reported (the same conservative placeholder as the PUT/BXM
  pass -- cf. 2026-09-13_vtsl-cost-load.md which showed the write-side
  half-spread is already inside the Cboe levels and the incremental live load
  is bps-scale; nothing here is charged that load can overturn).

QUESTIONS:
  Q1 Does the transferred k=0 rule score positive OOS on each new target
     (same-sample timed vs B&H, and WFA stitched OOS)?
  Q2 Did k-selection help on any target (tuned OOS vs 10k random-grid null,
     PBO, CPCV)? After PUT/BXM, the strong prior is "nothing to tune" -- test
     it rather than assume.
  Q3 2016+ holdout for the transferred rule per target.

Determinism: SEED=0, argmax ties broken by grid order; construction imported
from the published modules (never copied). Trials NOT unioned into the shared
evaluation registry (same family-2 rule as the PUT/BXM pass).

Run: C:\\ProgramData\\anaconda3\\python.exe experiments/2026-09-13_vtsl-extension.py
"""
import importlib.util
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

K_GRID = [0.00, 0.01, 0.02, 0.03, 0.04, 0.05]
TARGETS = ["BXMD", "PUTR", "CLL"]
ASSETS_PUBLISHED = ["PUT", "BXM"]          # already validated; not re-run here
OUT_DIR = "storage/reports/eval"
N_RANDOM_PATHS = 10_000
SEED = 0


def load_vtsl_module():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo_root, "experiments"))
    path = os.path.join(repo_root, "experiments", "2026-09-13_vix-term-structure.py")
    spec = importlib.util.spec_from_file_location("vtsl_0913", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_fwo_module():
    """The PUT/BXM forward-opt module (committed): reuse run_family, verdict,
    rebalance_decisions, net_of_fee, ann_sharpe -- the EXACT SAME code that
    scored PUT/BXM, so the three new targets get the identical treatment."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo_root, "experiments"))
    path = os.path.join(repo_root, "experiments", "2026-09-13_vtsl-forward-optimization.py")
    spec = importlib.util.spec_from_file_location("vtsl_fwo_0913", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def k_only_family_specs(slope, ret, dates, targets):
    """k-family only (see docstring: the ma family was a strict loss on the
    validated targets and is not re-litigated). Published default is the
    TRANSFERRED k=0 rule, asserted present in the grid."""
    specs = {}
    for tgt in targets:
        cells = [(f"k={k:g}", {"k": float(k)}) for k in K_GRID]

        def builder(params, _t=tgt):
            return mod.month_positions(slope, ret[_t], threshold=params["k"])

        specs[f"{tgt}.k"] = {
            "cells": cells,
            "default": ("k=0", {"k": 0.0}),
            "target": tgt,
            "builder": builder,
        }
    for name, sp in specs.items():
        assert any(dict(c[1]) == dict(sp["default"][1]) for c in sp["cells"]), name
    return specs


def main() -> None:
    global mod, fwo
    mod = load_vtsl_module()
    fwo = load_fwo_module()
    sys.modules["vtsl_fwo_0913"] = fwo  # module-level globals resolve via the module
    fwo.mod = mod                        # run_family's builders call mod.month_positions

    df = mod.load_daily()
    slope = mod.daily_slope(df[["VIX9D", "VIX", "VIX3M", "VIX6M"]], mod.VOL_SERIES)
    ret = df.pct_change()
    slope_start = slope.first_valid_index()

    print(f"SLOPE-active sample starts {slope_start.date()}; targets {TARGETS} "
          f"(PUT/BXM already validated -- {ASSETS_PUBLISHED}).")

    day_series = {}
    for tgt in TARGETS:
        first = ret[tgt].first_valid_index()
        start = max(slope_start, first)
        dates = ret.index[ret.index >= start]
        pos = mod.month_positions(slope, ret[tgt], threshold=0.0)
        src = (ret[tgt] * pos).reindex(dates).fillna(0.0)
        bh = fwo.ann_sharpe(ret[tgt].reindex(dates))
        day_series[tgt] = {"dates": dates, "pos": pos, "src": src, "bh": bh}
        n_act = int((pos.reindex(dates).fillna(0.0) > 0.5).sum())
        print(f"\n{tgt}: window {start.date()} -> {dates[-1].date()} "
              f"({len(dates)} days); B&H Sharpe {bh:.2f}; "
              f"k=0 timed Sharpe {fwo.ann_sharpe(src):.2f} (gross); "
              f"active days {n_act}")

    # reference the published long-only numbers on the same (slope-window) basis
    print("\nlong-only reference on the SLOPE window (full index sample):")
    sl_dates = ret.index[ret.index >= slope_start]
    for tgt in TARGETS + ASSETS_PUBLISHED:
        print(f"  {tgt:4s} B&H-actual Sharpe {fwo.ann_sharpe(ret[tgt].reindex(sl_dates)):.2f}")

    specs = k_only_family_specs(slope, ret, {t: day_series[t]["dates"] for t in TARGETS},
                                TARGETS)

    master = {
        "run": "2026-09-13_vtsl-extension",
        "targets": TARGETS,
        "published_overlay_targets": ASSETS_PUBLISHED,
        "rule": "transferred k=0 a-priori (no fitting on the new targets)",
        "k_grid": K_GRID, "n_random_paths": N_RANDOM_PATHS, "seed": SEED,
        "ma_family": "omitted (pre-registered strict loss on PUT/BXM)",
        "fee_note": "net reporting = 10bps one-way per position flip "
                    "(conservative; see 2026-09-13_vtsl-cost-load.md)",
        "families": {}}
    daily = {}
    for tgt in TARGETS:
        dts = day_series[tgt]["dates"]
        result, full_series = fwo.run_family(
            f"{tgt}.k",
            {"cells": specs[f"{tgt}.k"]["cells"],
             "default": specs[f"{tgt}.k"]["default"],
             "target": tgt,
             "builder": specs[f"{tgt}.k"]["builder"]},
            ret, dts)
        master["families"][f"{tgt}.k"] = result
        master["families"][f"{tgt}.k"]["buy_and_hold_same_window"] = \
            fwo.ann_sharpe(ret[tgt].reindex(dts))
        daily[tgt] = full_series
        print(f"\n=== family: {tgt}.k (target {tgt}) ===")
        for line in result["verdict"]:
            print("  " + line)

    os.makedirs(OUT_DIR, exist_ok=True)
    jpath = os.path.join(OUT_DIR, "vtsl_extension_20260913.json")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(master, fh, indent=2, cls=_JsonEnc)
    print(f"\nwrote {jpath}")

    for tgt, fser in daily.items():
        dfout = pd.DataFrame({lbl: s for lbl, _, s, _ in fser})
        dfout.to_parquet(os.path.join(OUT_DIR, f"vtsl_wfo_{tgt.lower()}_cells_daily.parquet"))
    print("wrote storage/reports/eval/vtsl_wfo_<target>_cells_daily.parquet")


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