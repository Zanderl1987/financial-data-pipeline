"""
strategies/ports/fractal_memory_strategy.py -- port of "Fractal Memory Strategy
[Jayadev Rana]" (bluealgocapital, tv_url https://www.tradingview.com/script/
7gcvFvlg-Fractal-Memory-Strategy-Jayadev-Rana/), source in
storage/tv_scripts/fractal_memory_strategy.pine.

Author design (from source, verbatim)
-------------------------------------
Fractal/memory method: an ATR-multiple chandelier trail (close -/+
trailMult*ATR, ratcheting with its own direction) defines the regime by FLIPS
of the direction state. At each flip the script scans scanDepth of trailing
history for the least-squared-error match of the LAST winLen bars' ATR-wise
returns (template), measured over raw returns normalized by the flip bar's
return stdev. The "forecast direction" is the sign of the accumulated return
over the fcLen bars IMMEDIATELY PRECEDING the best-matched window. When
needAgree (default ON) a flip only becomes a real signal if that historical
forecast agrees (nonnegative for longs, nonpositive for shorts); a re-trail
for a fresh flip is seeded then. Entries are market orders; per entry the
script places a 3-leg exit ladder -- TP1 (I 33%) at +1R, TP2 (33%) at +2R,
the remainder at +3R, all with a common hard stop at slMult*R below/above the
entry, where R = ATR*(1.2 + volRank) and volRank = percentile rank of ATR over
200 bars. A confirmed opposite signal flips/closes the book.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. The chandelier flip state machine is a scalar loop; fresh-flip trail
   seeding happens on the flip bar exactly as in Pine (var ordering).
2. The forecast (nearest-return-shape match + preceding-window net return) is
   recomputed ONLY on flip bars, gating the same bar's signal; its Pine
   last-value persistence across flips is reproduced with a carried scalar.
3. The 3-leg partial TP ladder (33/33/34% at +1R/+2R/+3R, shared stop) cannot
   be expressed in the engine's single-position contract; it is collapsed to:
   the position exits at the FIRST bar whose range touches any exit level
   (stop la slMult*R or the +1R target). The residual-leg futures (price
   reaching +2R/+3R, or a later stop on the unfilled legs) are dropped.
   Anchor eP = close of the SIGNAL bar and R = atr[sig]*(1.2+volRank[sig]),
   both frozen at entry as the source's exit orders are.
4. Opposite-signal flips close the position (source strategy.close), rendered
   as an exit trigger on the opposite CONFIRMED signal bar; the engine's
   next_free gate drops the same-bar flip entry (re-enter on the next flip).
5. Ghost candles, the panel table, alerts, and the last-bar fcDir display are
   cosmetic and are not ported. tpPercents are carried as params though the
   collapsed exit uses only the TP1 (+1R) level.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema
from strategies.ports import base
from strategies.ports.base import atr_wilder, percentrank
from strategies.ports.base import simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "fractal_memory_strategy"

DEFAULT_PARAMS = dict(
    win_len=30, scan_depth=750, fc_len=50,
    atr_len=14, trail_mult=4.0, need_agree=True,
    sl_mult=1.5, tp1_pct=33, tp2_pct=33,
    trade_dir="Both",
)


def _flip_signals(c, atr, mult):
    """Chandelier trail direction state: flip bars (Pine scalar ordering)."""
    n = len(c)
    trail = np.nan
    direction = 1
    flip_up = np.zeros(n, dtype=bool)
    flip_dn = np.zeros(n, dtype=bool)
    for j in range(n):
        up_stop = c[j] - atr[j] * mult
        dn_stop = c[j] + atr[j] * mult
        if direction == 1:
            trail = up_stop if np.isnan(trail) else max(trail, up_stop)
        else:
            trail = dn_stop if np.isnan(trail) else min(trail, dn_stop)
        new_dir = direction
        if direction == 1 and c[j] < trail:
            new_dir = -1
        elif direction == -1 and c[j] > trail:
            new_dir = 1
        flip_up[j] = new_dir == 1 and direction == -1
        flip_dn[j] = new_dir == -1 and direction == 1
        if flip_up[j]:
            trail = up_stop
        if flip_dn[j]:
            trail = dn_stop
        direction = new_dir
    return flip_up, flip_dn


def _forecast_dir(ret, sd, j, win_len, fc_len, depth):
    """Pine scan block: best offset + sign of preceding fcLen returns."""
    tpl = ret[j - win_len + 1:j + 1]          # ret[k], k = 0..winLen-1
    best_err = 1e20
    best_off = -1
    for off in range(fc_len + 1, depth + 1):
        if j - off - win_len + 1 < 0:
            break
        cand = ret[j - off - win_len + 1:j - off + 1]
        e = float(np.sum((tpl - cand) ** 2))
        if e < best_err:
            best_err = e
            best_off = off
    if best_off <= 0:
        return 0
    s = float(ret[j - best_off - fc_len:j - best_off].sum())
    return 1 if s > 0 else -1


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]
    n = len(df)

    atr = atr_wilder(high, low, close, p["atr_len"]).to_numpy(dtype=float)
    vol_rank = (percentrank(pd.Series(atr, index=df.index), 200) / 100.0).to_numpy(dtype=float)
    flip_up, flip_dn = _flip_signals(close.to_numpy(dtype=float), atr, p["trail_mult"])

    ret = np.log(close.to_numpy(dtype=float) / close.to_numpy(dtype=float))
    rc = close.to_numpy(dtype=float)
    div = np.empty_like(rc)
    div[0] = np.nan
    div[1:] = rc[1:] / rc[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = np.log(div)
    ret = np.nan_to_num(ret, nan=0.0, posinf=0.0, neginf=0.0)
    ret_s = pd.Series(ret, index=df.index).rolling(p["win_len"], min_periods=1).std()  # ddof=1 = Pine ta.stdev
    ret_sd = ret_s.to_numpy(dtype=float)

    filt_depth = min(p["scan_depth"], 300)
    allow_long = p["trade_dir"] != "Short only"
    allow_short = p["trade_dir"] != "Long only"

    fc_agree = 0
    long_sig = np.zeros(n, dtype=bool)
    short_sig = np.zeros(n, dtype=bool)
    for j in range(n):
        if (flip_up[j] or flip_dn[j]) and j > p["win_len"] + p["fc_len"] + filt_depth:
            sd0 = float(ret_sd[j]) + 1e-10 if np.isfinite(ret_sd[j]) else 1e-10
            ag = _forecast_dir(ret, sd0, j, p["win_len"], p["fc_len"], filt_depth)
            if ag != 0:
                fc_agree = ag
        respect = not p["need_agree"] or fc_agree >= 0
        agree_dn = not p["need_agree"] or fc_agree <= 0
        if flip_up[j]:
            long_sig[j] = respect and allow_long
        if flip_dn[j]:
            short_sig[j] = agree_dn and allow_short

    close_arr = close.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    sl_mult = p["sl_mult"]

    def _entry_exits(j, sig_i, long_side):
        e_p = close_arr[sig_i]
        unit_r = atr[sig_i] * (1.2 + vol_rank[sig_i])
        if long_side:
            return low_arr[j] <= e_p - sl_mult * unit_r or high_arr[j] >= e_p + unit_r
        return high_arr[j] >= e_p + sl_mult * unit_r or low_arr[j] <= e_p - unit_r

    def long_exit_trigger(j, sig_i, price, frame):
        if short_sig[j]:
            return True
        return _entry_exits(j, sig_i, True)

    def short_exit_trigger(j, sig_i, price, frame):
        if long_sig[j]:
            return True
        return _entry_exits(j, sig_i, False)

    long_entries = pd.Series(long_sig, index=df.index)
    short_entries = pd.Series(short_sig, index=df.index)
    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger,
        df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, fractal-memory flips)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/7gcvFvlg-Fractal-Memory-Strategy-Jayadev-Rana/",
        tv_author="bluealgocapital",
        tv_script_name="Fractal Memory Strategy [Jayadev Rana]",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "chandelier trail flip machine + per-flip nearest-shape forecast "
            "(min-SSE over scanDepth offsets, sign of preceding fcLen net "
            "return) gating the same-bar signal; agree value carried across "
            "flips like Pine var",
            "3-leg TP ladder (33/33/34% at +1R/+2R/+3R, shared slMult*R stop) "
            "collapsed to full-position exit on the first bar touching any "
            "exit level (stop or +1R); eP & R frozen at the signal bar",
            "opposite confirmed signal exits the position; engine next_free "
            "gate drops the same-bar flip entry",
            "ghost candles / panel / alerts / last-bar fcDir display not ported",
        ],
    ),
    build_rule,
)