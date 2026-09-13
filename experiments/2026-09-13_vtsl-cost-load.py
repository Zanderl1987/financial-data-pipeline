#!/usr/bin/env python3
"""
Live-execution cost load on the VTSL overlay (PUT / BXM). THE "last gate before
tradeable" queued follow-up from the 2026-09-13 VTSL writeups.

WHAT THE INDEX LEVELS ALREADY INCLUDE (primary sources, so we don't
double-charge the write side):
  - Whaley (2002), "Return and Risk of CBOE Buy Write Monthly Index": "the bid
    price is used when the call is first written ... the BXM index already
    incorporates an implicit trading cost equal to one half the bid-ask
    spread." Rewriting the call at mid would have ADDED ~6 bps/month
    (~70 bps/yr).
  - Current Cboe methodology (2004+): the new option is "deemed sold at a
    price equal to the volume-weighted average of the traded prices (VWAP)...,
    if no transactions occur ... the last bid price reported before the end of
    the VWAP period." Settlement is cash at SOQ. => ONE option transaction per
    active month (the write), filled at a within-spread trade-weighted price,
    bid-floored, settled for free.
  Consequence: the published index series already nets ~half the option's bid-
  ask spread on the write. The free-data stack has no measured SPX bid/ask
  history (OPTIONS_DATA_SOURCES.md NO-GO section -- OptionMetrics/ORATS/
  Databento are all paid), so charging a modeled full half-spread AGAIN would
  be a double count, and the honest incremental load is:

PRE-REGISTERED COST MODEL (fixed once):
  1. Fees per option write (not in the index): exchange + clearing + ORF + a
     commission allowance. SPX options are quoted per contract ($100
     multiplier; notional ~ SPX x $100). Base -BPS_FEE_MONTH_* assumes a
     sub-basis-point per-contract load; the grid 0/0.5/1/2/5 bps per actIVE
     month brackets from zero-fee to a ~10-50x slippage blow-up.
  2. Residual slippage beyond the quoted VWAP/bid fill: NOT measured
     (no free data); covered by the same monthly grid (the middle rows double
     as a slippage budget).
  3. BXM long-equity-leg implementation (the index assumes you hold the S&P
     500 portfolio, dividend-reinvested; a live book holds the basket, a
     S&P 500 ETF, or ES futures): base annual drag 10 bps/yr (SPY-class ER)
     applied on ACTIVE days only, sensitivity 0/10/25 bps/yr. PUT has no
     equity leg -- its collateral sits in the T-bill account, which the
     methodology already credits; no incremental.

  Fee date: charged once per ACTIVE calendar month on the first active day
  (the month in which the overlay holds the target; position is constant
  within a month by construction, so this is exactly one charge per written
  option). The write-side half-spread is NOT re-charged (already embedded).

QUESTIONS this pass answers:
  Q1 Does the incremental cost load flip any headline number (same-sample
     active window, 2016+ holdout, walk-forward OOS)?
  Q2 Breakeven fee (bps per active month) at which the overlay OOS falls to
     the buy-and-hold Sharpe, and to zero -- unreachable in practice?
  Q3 Does the BXM equity-leg drag (ETF/implementation) matter separately?

Determinism: no RNG; all constructions reuse the published VTSL module
(spec_from_file_location, never copied); argmax/best-cell logic deterministic.

Run: C:\\ProgramData\\anaconda3\\python.exe experiments/2026-09-13_vtsl-cost-load.py
"""
import importlib.util
import json
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ANN = 252
DEV_END = pd.Timestamp("2016-01-01")          # 2016+ holdout, same split as the VTSL writeups
FEE_BPS_MONTH_GRID = [0.0, 0.5, 1.0, 2.0, 5.0]  # bps notional per active month (one write)
BXM_LEG_BPS_YR_GRID = [0.0, 10.0, 25.0]         # bps/yr drag on active days (ETF/basis)
OUT_DIR = "storage/reports/eval"
ASSETS = ["PUT", "BXM"]
FLOORS = {"PUT": 0.82, "BXM": 0.62}             # published k=0 active-window reproduction floors
WFO_OOS_REPRO = {"PUT": 0.97, "BXM": 0.91}      # forward-opt stitched WFA OOS (gross)
HOLDOUT_REPRO = {"PUT": 1.127, "BXM": 1.066}    # 2016+ k=0 holdout Sharpe (gross)


def load_vtsl_module():
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


def ann_stats(daily):
    r = pd.Series(daily).dropna()
    mean = r.mean() * ANN
    vol = r.std(ddof=1) * math.sqrt(ANN)
    eq = (1 + r).cumprod()
    return {"ann": mean, "vol": vol, "sharpe": mean / vol,
            "maxdd": float((eq / eq.cummax() - 1).min()), "n": len(r)}


def apply_costs(src: pd.Series, pos: pd.Series, dates: pd.DatetimeIndex,
                fee_bps_per_active_month: float, eq_leg_bps_yr: float) -> pd.Series:
    """src = gross daily P&L; pos = effective position (already t0+1 shifted).
    Costs: (1) fee once per ACTIVE calendar month on the first active day (the
    one option write per roll month; write-side half-spread already in index),
    (2) optional annual equity-leg drag (bps/yr) on active days only (BXM long
    leg implemented via ETF/basket instead of the notional S&P 500)."""
    held = pos.reindex(dates).fillna(0.0)
    active = held > 0.5
    month = active.index.to_period("M")
    first_active_day = active.groupby(month).cumsum() == 1   # True on 1st active day each month
    cost = pd.Series(0.0, index=dates)
    cost[first_active_day] = fee_bps_per_active_month / 1e4
    if eq_leg_bps_yr:
        cost = cost + (eq_leg_bps_yr / 1e4 / ANN) * active.astype(float)
    return (src.reindex(dates).fillna(0.0) - cost)


def main() -> None:
    mod = load_vtsl_module()
    df = mod.load_daily()
    slope = mod.daily_slope(df[["VIX9D", "VIX", "VIX3M", "VIX6M"]], mod.VOL_SERIES)
    ret = df.pct_change()
    start = slope.first_valid_index()
    dates = ret.index[ret.index >= start]
    print(f"SLOPE-active sample: {start.date()} -> {dates[-1].date()} ({len(dates)} days)")

    series = {}
    for tgt in ASSETS:
        pos = mod.month_positions(slope, ret[tgt], threshold=0.0)
        src = (ret[tgt] * pos).reindex(dates).fillna(0.0)
        series[tgt] = {"pos": pos, "src": src}
        sh = ann_sharpe(src)
        print(f"[guard] {tgt} published k=0 active-window Sharpe = {sh:.3f} "
              f"(floor {FLOORS[tgt]})")
        assert sh >= FLOORS[tgt], f"{tgt} reproduction FAILED"

    # walk-forward stitched OOS reproduction (same folds as the forward-opt pass)
    for tgt in ASSETS:
        src = series[tgt]["src"]
        train_days = 5 * 252
        oos_dates = dates[train_days:]
        ticks = np.array_split(np.arange(len(oos_dates)), 7)
        folds = [oos_dates[t] for t in ticks]
        pieces = []
        for chunk in folds:
            pieces.append(src[(src.index >= chunk[0]) & (src.index <= chunk[-1])])
        stitched = pd.concat(pieces).dropna()
        sh = ann_sharpe(stitched)
        print(f"[guard] {tgt} WFO stitched OOS = {sh:.3f} (repro {WFO_OOS_REPRO[tgt]})")
        assert abs(sh - WFO_OOS_REPRO[tgt]) < 0.02, f"{tgt} WFO repro drift"

        # 2016+ holdout (k=0, no fitting)
        hd = src[src.index >= DEV_END].dropna()
        sh_h = ann_sharpe(hd)
        print(f"[guard] {tgt} 2016+ holdout = {sh_h:.3f} (repro {HOLDOUT_REPRO[tgt]})")
        assert abs(sh_h - HOLDOUT_REPRO[tgt]) < 0.02, f"{tgt} holdout repro drift"

    print("\nreference (active window, no timing):")
    for tgt in ASSETS:
        bh = ann_sharpe(ret[tgt].reindex(dates))
        print(f"  {tgt} B&H  {bh:.2f}")

    # ---- cost tables ----
    rows = []
    for tgt in ASSETS:
        src = series[tgt]["src"]
        pos = series[tgt]["pos"]
        eq_legs = BXM_LEG_BPS_YR_GRID if tgt == "BXM" else [0.0]
        for leg in eq_legs:
            for fee in FEE_BPS_MONTH_GRID:
                net = apply_costs(src, pos, dates, fee, leg)
                hd = net[net.index >= DEV_END].dropna()
                rows.append({
                    "target": tgt, "eq_leg_bps_yr": leg, "fee_bps_active_month": fee,
                    "full_sharpe": ann_sharpe(net),
                    "full_ann": ann_stats(net)["ann"], "full_vol": ann_stats(net)["vol"],
                    "holdout_2016plus_sharpe": ann_sharpe(hd),
                    "n_charges": int((pos.reindex(dates).fillna(0.0) > 0.5)
                                     .groupby(dates.to_period("M")).cumsum().gt(0.5).sum()),
                })
    cost_tab = pd.DataFrame(rows)

    # ---- breakeven: fee bps/month where holdout Sharpe reaches B&H, and 0 ----
    breakeven = {}
    for tgt in ASSETS:
        src = series[tgt]["src"]
        pos = series[tgt]["pos"]
        hd = src[src.index >= DEV_END].dropna()
        bh_hd = ann_sharpe(ret[tgt].reindex(dates)[dates >= DEV_END])
        # scan fee grid linearly (brute force, deterministic) twice as fine
        lo, hi = 0.0, 200.0
        for goal_name, goal in (("vs_holdout_bh", bh_hd), ("zero", 0.0)):
            f = None
            prev_sh = ann_sharpe(apply_costs(src, pos, dates, lo, 0.0)[dates >= DEV_END])
            for b in np.linspace(lo, hi, 20001):
                sh = ann_sharpe(apply_costs(src, pos, dates, b, 0.0)
                                [dates >= DEV_END])
                if sh <= goal:
                    f = float(b)
                    break
                prev_sh = sh
            breakeven.setdefault(tgt, {})[goal_name] = f
    print(f"\nbreakeven fee (bps/active-month) where 2016+ Sharpe reaches "
          f"B&H={breakeven['PUT'].get('vs_holdout_bh')}-band / zero: "
          f"PUT {breakeven['PUT']['zero']}, BXM {breakeven['BXM']['zero']}")

    # ---- WFO stitched OOS under the base fee load ----
    wfo_rows = []
    for tgt in ASSETS:
        src = series[tgt]["src"]
        pos = series[tgt]["pos"]
        train_days = 5 * 252
        oos_dates = dates[train_days:]
        ticks = np.array_split(np.arange(len(oos_dates)), 7)
        folds = [oos_dates[t] for t in ticks]
        for fee in FEE_BPS_MONTH_GRID:
            net = apply_costs(src, pos, dates, fee,
                              10.0 if tgt == "BXM" else 0.0)
            pieces = []
            for chunk in folds:
                pieces.append(net[(net.index >= chunk[0]) & (net.index <= chunk[-1])])
            wfo_rows.append({"target": tgt, "fee_bps_active_month": fee,
                             "wfo_stitched_sharpe": ann_sharpe(
                                 pd.concat(pieces).dropna())})
    wfo_tab = pd.DataFrame(wfo_rows)

    # ---- net-of-fee monthly returns charting: print a compact table ----
    print("\nmachine vs gross (active window, k=0):")
    for tgt in ASSETS:
        g = ann_sharpe(series[tgt]["src"])
        n = ann_sharpe(apply_costs(series[tgt]["src"], series[tgt]["pos"], dates,
                                   0.5, 10.0 if tgt == "BXM" else 0.0))
        print(f"  {tgt}: gross {g:.3f} -> +fees(base) {n:.3f}")

    master = {
        "run": "2026-09-13_vtsl-cost-load",
        "data": f"{dates[0].date()} -> {dates[-1].date()}",
        "fee_bps_active_month_grid": FEE_BPS_MONTH_GRID,
        "bxm_equity_leg_bps_yr_grid": BXM_LEG_BPS_YR_GRID,
        "note": "write-side half-spread is ALREADY in the index levels "
                "(Whaley 2002: bid-price write = ~6 bps/month; current method: "
                "VWAP fill, bid-floored, cash settlement). Model charges "
                "per-contract fees + residual slippage + (BXM only) "
                "long-equity-leg implementation drag.",
        "sources": [
            "Whaley (2002) Return and Risk of CBOE Buy Write Monthly Index, "
            "J. Derivatives 10(2)",
            "Cboe BuyWrite Indices Methodology (VWAP roll, SOQ settlement)",
            "Cboe PutWrite Indices Methodology (VWAP roll, T-bill collateral)"],
        "cost_table": cost_tab.to_dict("records"),
        "wfo_table": wfo_tab.to_dict("records"),
        "breakeven": {k: v for k, v in breakeven.items()},
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    jpath = os.path.join(OUT_DIR, "vtsl_cost_load_20260913.json")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(master, fh, indent=2, cls=_JsonEnc)
    print(f"\nwrote {jpath}")


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