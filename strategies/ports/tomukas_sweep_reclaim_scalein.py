"""
strategies/ports/tomukas_sweep_reclaim_scalein.py -- port of "Tomukas Daily
Scale-In" (Tomukasss, tv_url https://www.tradingview.com/script/IFSv46kK-
Tomukas-Daily-Scale-In/, strategy() title "Tomukas Sweep Reclaim Scale-In
Strategy"), source in storage/tv_scripts/tomukas_sweep_reclaim_scalein.pine.

Author design (from source, verbatim)
-------------------------------------
Liquidity-sweep + reclaim counter-trend entries, EMA200-filtered. A swing
pivot high/low (8/8) arms a sweep level; the FIRST bar whose range pierces it
beyond minSweepATR but within maxSweepATR consumes the sweep (once per swing,
or re-armed after a reclaim). Within reclaimBars bars a reclaim bar confirms:
long after a low-sweep when close reclaims above the swept low (+buffer), the
bar closes near its high (close location >= 60%), has a body >= 0.05 ATR, and
close sits above the EMA200 (bullBias). Entries are market orders sized as
fixed % of equity (10/10/20/40/80% tiers); up to 4 scale-in adds ladder in at
-0.30%, -0.60%, -0.90%, -1.20% of the base entry (fixed % steps), and the ONLY
exit is a fixed +0.40% take profit computed from the position's AVERAGE entry
price (intrabar high/low touches, close_all on process_orders_on_close=true).
Opposite-side reclaims flip the book when closeOnOpposite (close_all +
opposite entry).

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. The sweep/reclaim state machine (swing level, swept flag, reclaim window)
   is transcribed as an O(n) ndarray loop; Python scalars hold the persistent
   state exactly like Pine's `var`. The both-reclaim bar nullifies both
   conditions, as in the source.
2. scale-in adds cannot be represented as engine positions (single position);
   they are approximated inside the TP exit: while an entry is open, stages
   fill at the FIRST bar whose low/high trades the ladder level
   base*(1 -+ step*k/100), fills approximated at that bar's close, and the
   position-average price is the equity-weighted harmonic mean
   Sum(q)/Sum(q/fill_px). The TP fires when high >= avg*(1+tpPct/100) (longs;
   shorts mirror). Corner deviation: if two stages' levels are first traded on
   the same bar the source only advances one stage per bar, while the model
   fills both.
3. Opposite-reclaim flips are approximated as an EXIT trigger on the opposite
   reclaim bar (the engine drops the same-bar opposite entry via its re-entry
   gate; the source's close_all + new direction becomes "exit, re-enter on the
   next fresh sweep" -- documented deviation). closeOnOpposite=false preserves
   the position (no exit), matching the source.
4. Sweep ATR depth uses safeATR = max(ATR, syminfo.mintick); mintick is
   approximated as 1e-9 of price, i.e. never binding at these depths.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema
from strategies.ports import base
from strategies.ports.base import (atr_wilder, pivot_high, pivot_low,
                                   simulate_positions_both_indexed)
from strategies.ports import _register, PortInfo

SLUG = "tomukas_sweep_reclaim_scalein"

DEFAULT_PARAMS = dict(
    left_bars=8, right_bars=8, atr_len=14,
    min_sweep_atr=0.05, max_sweep_atr=0.80,
    reclaim_bars=3, close_buffer_atr=0.02,
    min_close_location=0.60, min_body_atr=0.05,
    use_ema=True, ema_len=200,
    tp_pct=0.40, scale_step_pct=0.30,
    q1=10.0, q2=10.0, q3=20.0, q4=40.0, q5=80.0,
    close_on_opposite=True,
)


def _sweep_reclaim_signals(d, p):
    """O(n) transcription of Pine's persistent sweep state (see module)."""
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    ph, pl = d["ph"], d["pl"]
    safe_atr = np.maximum(d["atr"], d["tick"])
    ema = d["ema"]
    n = len(c)

    bull_bias = (not p["use_ema"]) | (c > ema)
    bear_bias = (not p["use_ema"]) | (c < ema)
    body_atr = np.abs(c - o) / safe_atr
    bar_range = np.maximum(h - l, d["tick"])
    bull_close_loc = (c - l) / bar_range
    bear_close_loc = (h - c) / bar_range

    sell_reclaim = np.zeros(n, dtype=bool)
    buy_reclaim = np.zeros(n, dtype=bool)

    swing_high = np.nan
    swing_high_bar = -1
    high_swept = False
    high_sweep_bar = -1
    high_sweep_level = np.nan

    swing_low = np.nan
    swing_low_bar = -1
    low_swept = False
    low_sweep_bar = -1
    low_sweep_level = np.nan

    for j in range(n):
        if not np.isnan(ph[j]):
            swing_high = ph[j]
            swing_high_bar = j - p["right_bars"]
            high_swept = False
        if not np.isnan(pl[j]):
            swing_low = pl[j]
            swing_low_bar = j - p["right_bars"]
            low_swept = False

        if (not high_swept and np.isfinite(swing_high) and j > swing_high_bar
                and h[j] > swing_high):
            depth = (h[j] - swing_high) / safe_atr[j]
            if p["min_sweep_atr"] <= depth <= p["max_sweep_atr"]:
                high_swept = True
                high_sweep_bar = j
                high_sweep_level = swing_high
        if (not low_swept and np.isfinite(swing_low) and j > swing_low_bar
                and l[j] < swing_low):
            depth = (swing_low - l[j]) / safe_atr[j]
            if p["min_sweep_atr"] <= depth <= p["max_sweep_atr"]:
                low_swept = True
                low_sweep_bar = j
                low_sweep_level = swing_low

        high_w = high_swept and high_sweep_bar >= 0 \
            and j - high_sweep_bar <= p["reclaim_bars"]
        sell_reclaim[j] = bool(high_w
            and c[j] < high_sweep_level - safe_atr[j] * p["close_buffer_atr"]
            and bear_close_loc[j] >= p["min_close_location"]
            and body_atr[j] >= p["min_body_atr"] and bear_bias[j])
        low_w = low_swept and low_sweep_bar >= 0 \
            and j - low_sweep_bar <= p["reclaim_bars"]
        buy_reclaim[j] = bool(low_w
            and c[j] > low_sweep_level + safe_atr[j] * p["close_buffer_atr"]
            and bull_close_loc[j] >= p["min_close_location"]
            and body_atr[j] >= p["min_body_atr"] and bull_bias[j])

        if sell_reclaim[j]:
            high_swept = False
        if buy_reclaim[j]:
            low_swept = False
        if sell_reclaim[j] and buy_reclaim[j]:
            sell_reclaim[j] = False
            buy_reclaim[j] = False

    return sell_reclaim, buy_reclaim


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    open_, high, low, close = df["open"], df["high"], df["low"], df["close"]

    atr = atr_wilder(high, low, close, p["atr_len"])
    tick = close * 1e-9
    d = dict(
        o=open_.fillna(close).to_numpy(dtype=float),
        h=high.to_numpy(dtype=float),
        l=low.to_numpy(dtype=float),
        c=close.to_numpy(dtype=float),
        atr=atr.to_numpy(dtype=float),
        tick=tick.to_numpy(dtype=float),
        ema=_ema(close, p["ema_len"]).to_numpy(dtype=float),
        ph=pivot_high(high, p["left_bars"], p["right_bars"]).to_numpy(dtype=float),
        pl=pivot_low(low, p["left_bars"], p["right_bars"]).to_numpy(dtype=float),
    )
    sell_reclaim, buy_reclaim = _sweep_reclaim_signals(d, p)

    long_entries = pd.Series(buy_reclaim, index=df.index)
    short_entries = pd.Series(sell_reclaim, index=df.index)

    close_arr = close.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    tp_pct = p["tp_pct"] / 100.0
    step = p["scale_step_pct"] / 100.0
    qs = np.array([p["q1"], p["q2"], p["q3"], p["q4"], p["q5"]], dtype=float)
    close_opp = p["close_on_opposite"]

    def _avg_at(j, sig_i, long_side):
        """Equity-weighted average fill price incl. ladder adds by bar j."""
        sign = -1.0 if long_side else 1.0
        cum_q = qs[0]
        cum_shr = qs[0] / close_arr[sig_i]
        for k in range(1, 5):
            level = close_arr[sig_i] * (1 + sign * step * k)
            for jk in range(sig_i + 1, j + 1):
                hit = low_arr[jk] <= level if long_side \
                    else high_arr[jk] >= level
                if hit:
                    cum_q += qs[k]
                    cum_shr += qs[k] / close_arr[jk]
                    break
        return cum_q / cum_shr

    def long_exit_trigger(j, sig_i, price, frame):
        if close_opp and short_entries.iloc[j]:
            return True
        return high_arr[j] >= _avg_at(j, sig_i, True) * (1 + tp_pct)

    def short_exit_trigger(j, sig_i, price, frame):
        if close_opp and long_entries.iloc[j]:
            return True
        return low_arr[j] <= _avg_at(j, sig_i, False) * (1 - tp_pct)

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger,
        df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, counter-trend sweep reclaim)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/IFSv46kK-Tomukas-Daily-Scale-In/",
        tv_author="Tomukasss",
        tv_script_name="Tomukas Daily Scale-In",
        mechanism_family="reversal",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "sweep/reclaim state machine transcribed as an O(n) Python-scalar "
            "loop holding the persistent Pine var state",
            "scale-in ladder approximated inside the TP exit: stages fill at "
            "first touch of the ladder level; position average is the "
            "equity-weighted harmonic mean; TP = avg*(1+tpPct/100) on high/"
            "low touch",
            "opposite-reclaim flip rendered as exit-on-opposite; engine gate "
            "drops the same-bar reversed entry (re-enter on next sweep)",
            "closeOnOpposite=false leaves the position open (no opposite "
            "exit), per source",
        ],
    ),
    build_rule,
)