#!/usr/bin/env python3
"""
Cross-asset futures carry backtest, plus carry x TSMOM combination.

Carry proxy (in-house data limitation): the futures store is continuous FRONT-
MONTH only, no back-month / no spot -> the classic Koijen-Moskowitz-Pedersen-
Vrugt (2018) carry = (F1/F2 spread) is not directly computable. We proxy it with
the ROLL GAP: on the day the front contract expires the price jumps from the
expiring F1 to the new front (the former F2); that overnight gap embeds the
basis. carry_event = -ln(open[t]/close[t-1]) on detected roll days (same
detector as _tsmom_core.roll_mask), averaged over trailing-252d roll events
(min 3), annualized by the instrument's own median roll interval.

Read the caveat loudly: the gap also contains one day of the asset's own
return; this is a *proxy*, directionally the roll basis, not the exact F1/F2
carry. Compare rankings, not absolute levels.

Strategies (each vol-targeted 40% ex-ante, positions effective t0+1):
  A) TSMOM sign (reference, MOP construction as _tsmom_core)
  B) CARRY: long high-carry half, short low-carry half (cross-sectional median)
  C) 50/50 combined score (momentum sign x 0.5 + carry sign x 0.5), vol-scaled
"""
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _tsmom_core import (  # noqa: E402
    ANN,
    VOL_TARGET,
    ann_sharpe,
    load_close_wide,
    load_open_wide,
    max_drawdown,
    month_end_anchors,
    portfolio_returns,
    roll_mask,
)

OUT = "storage/reports/eval/carry_futures_daily.parquet"


def build_carry(close: pd.DataFrame, open_: pd.DataFrame,
                rm: pd.DataFrame, min_events: int = 3) -> pd.DataFrame:
    """PIT monthly carry score per instrument (annualized, demeaned-level units)."""
    days = pd.Series(np.asarray(close.index.to_pydatetime()), index=close.index)
    gap = (open_ / close.shift(1)).apply(np.log)  # +carry direction
    anchors = month_end_anchors(close.index)
    scores = pd.DataFrame(np.nan, index=anchors, columns=close.columns)
    # per-instrument roll interval (median days between events) -> annualization
    events = {}
    for sym in close.columns:
        ev = np.flatnonzero(rm[sym].values)
        events[sym] = ev
    for t0 in anchors:
        cutoff = t0 - pd.Timedelta(days=366)
        cidx = close.index.asof(cutoff)
        if pd.isna(cidx):
            continue
        i0 = close.index.get_loc(cidx)
        for sym in close.columns:
            ev = events.get(sym)
            if ev is None or len(ev) == 0:
                continue
            sel = ev[close.index[ev] < t0]
            if len(sel) < min_events:
                continue
            window = sel[sel >= i0]
            if len(window) < min_events:
                window = sel[-min_events:]
            g = gap[sym].iloc[window]
            g = g[np.isfinite(g)]
            if len(g) < min_events:
                continue
            # median days between consecutive roll events -> annualization
            ev_ts = close.index[window]
            med_iv = np.median(np.diff(ev_ts).astype("timedelta64[D]").astype(float))
            if not np.isfinite(med_iv) or med_iv <= 0:
                continue
            ann_factor = 365.0 / med_iv
            scores.at[t0, sym] = float(-g.mean() * ann_factor)
    return scores


def build_carry_positions(scores: pd.DataFrame, close: pd.DataFrame,
                          vol_floor: float = 0.10,
                          long_short: bool = True) -> pd.DataFrame:
    ret = close.pct_change()
    vol = ret.rolling(252).std(ddof=1) * math.sqrt(ANN)
    vol = vol.clip(lower=vol_floor)
    pos = pd.DataFrame(0.0, index=scores.index, columns=close.columns)
    for t0 in scores.index:
        row = scores.loc[t0]
        good = row[np.isfinite(row)]
        if len(good) < 6:
            continue
        med = good.median()
        v = vol.loc[t0]
        for sym in good.index:
            scale = 0.5 * VOL_TARGET / v[sym] if v[sym] > 0 else 0.0
            if long_short:
                pos.at[t0, sym] = scale if good[sym] > med else -scale
            else:
                pos.at[t0, sym] = scale if good[sym] > med else 0.0
    return pos


def build_combined_positions(scores: pd.DataFrame, close: pd.DataFrame,
                             vol_floor: float = 0.10) -> pd.DataFrame:
    mom = __import__("_tsmom_core").build_positions(close, skip_days=31,
                                                    vol_floor=vol_floor)
    car = build_carry_positions(scores, close, vol_floor)
    combined = mom.copy() * 0.5 + car.reindex(mom.index).fillna(0.0) * 0.5
    return combined.where(mom != 0, 0.0)


def decade_stats(daily: pd.Series) -> str:
    parts = []
    for d in sorted(set(daily.index.year // 10 * 10)):
        sub = daily[daily.index.year // 10 * 10 == d]
        parts.append(f"{d}s {ann_sharpe(sub):.2f}")
    return ", ".join(parts)


def main():
    close = load_close_wide()
    open_ = load_open_wide().reindex(close.index)
    rm = roll_mask()
    scores = build_carry(close, open_, rm)

    days = np.asarray(close.index.to_pydatetime())
    anchors = month_end_anchors(close.index)
    ret_clean = close.pct_change()[~rm]  # excise roll jumps (TSMOM convention)

    print("=== CARRY + CARRY x TSMOM on 44 futures ===")
    print(f"carry score coverage: {scores.notna().sum().sum():,} instrument-months "
          f"(avg {scores.notna().sum(axis=1).mean():.0f}/month)\n")

    pos_mom = __import__("_tsmom_core").build_positions(close, skip_days=31, vol_floor=0.10)
    pos_car = build_carry_positions(scores, close)
    pos_car_long = build_carry_positions(scores, close, long_short=False)
    pos_com = build_combined_positions(scores, close)
    # TSMOM + long-only carry blend (no short-carry drag): 50/50 positions
    pos_carblend = pos_mom * 0.5 + pos_car_long.reindex(pos_mom.index).fillna(0.0) * 0.5

    for label, pos, is_cleaned in [
        ("TSMOM (reference)", pos_mom, True),
        ("CARRY long-short", pos_car, True),
        ("CARRY long-only", pos_car_long, True),
        ("CARRY x TSMOM 50/50", pos_com, True),
        ("TSMOM + long-carry blend", pos_carblend, True),
    ]:
        if is_cleaned:
            pr = portfolio_returns(pos, close, ret=ret_clean)
        else:
            pr = portfolio_returns(pos, close)
        g = pr["gross"]
        cum = (1 + g).cumprod()
        print(f"  {label:26s} Sharpe {ann_sharpe(g):6.2f}  "
              f"ann.mean {g.mean()*ANN*100:6.2f}%  vol {g.std()*math.sqrt(ANN)*100:5.1f}%  "
              f"maxDD {(cum/cum.cummax()-1).min()*100:6.1f}%")
        print(f"               decades: {decade_stats(g)}")

    # save daily net series for the composites
    pr_com = portfolio_returns(pos_com, close, ret=ret_clean)
    pr_car = portfolio_returns(pos_car, close, ret=ret_clean)
    out = pd.DataFrame({
        "tsmom": pr_com["gross"],
        "carry_ls": pr_car["gross"],
        "carryxtsmom": pr_com["gross"],
    })
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out.to_parquet(OUT)
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()