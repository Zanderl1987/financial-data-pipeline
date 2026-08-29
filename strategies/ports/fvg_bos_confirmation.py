"""
strategies/ports/fvg_bos_confirmation.py -- port of "Fair Value Gap Strategy
with Break of Structure Confirmation" (AIScripts, tv_url https://www.tradingview.com/
script/jyhTizLX-Fair-Value-Gap-Strategy-with-Break-of-Structure-Confirmation/),
source in storage/tv_scripts/fvg_bos_confirmation.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides. Track the most recent confirmed swing pivot (10/10 bars) as
"structure"; a Break of Structure (BOS) prints when close crosses that level.
A Fair Value Gap (3-candle imbalance: low > high[2] for a bullish gap, or
high < low[2] for a bearish one) is only "armed" if a BOS printed within
fvgExpiry bars of the gap bar, and stays live fvgExpiry bars from its own
formation. Entry: price dips back into the gap while flat (long: low <= gap-top
AND close >= gap-bottom). Exit: stop just below the gap-bottom minus
slBuffer*ATR, take-profit 2.0*ATR above the entry-bar close -- both levels
fixed at the signal bar. Symmetric for shorts.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. The SL/TP levels are computed inside the `if entryCond` block at the SIGNAL
   bar t (`stop = bFvgL - atr*slBuffer`, `limit = close + atr*tpMult`), so the
   port anchors them to bar t via base.simulate_positions_both_indexed rather
   than to the engine's next-close fill price.
2. Pine clears the armed gap to `na` on entry, so a gap arms at most ONE trade.
   The port forward-fills the latest armed gap without tracking consumption, so
   a re-entry on the same gap is possible if price re-dips into it after an
   early exit inside the expiry window -- rare on daily bars (the 2*ATR target
   pulls price away from the gap). [approx]
3. barstate.isconfirmed / plotshape / alertcondition are cosmetic, not ported.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.ports import base
from strategies.ports.base import atr_wilder, pivot_high, pivot_low, \
    simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "fvg_bos_confirmation"

DEFAULT_PARAMS = dict(
    swing_len=10, fvg_expiry=20, atr_length=14,
    tp_mult=2.0, sl_buffer=0.1,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]
    n = len(df)
    idx = np.arange(n)

    atr = atr_wilder(high, low, close, p["atr_length"])

    # structure = latest confirmed swing pivot, forward-filled
    last_swing_high = pivot_high(high, p["swing_len"], p["swing_len"]).ffill()
    last_swing_low = pivot_low(low, p["swing_len"], p["swing_len"]).ffill()

    bos_up = (close > last_swing_high).fillna(False)
    bos_dn = (close < last_swing_low).fillna(False)

    # most recent BOS bar per type (persisted via forward-fill)
    bos_up_bar = pd.Series(np.where(bos_up.to_numpy(), idx.astype(float), np.nan),
                           index=df.index).ffill()
    bos_dn_bar = pd.Series(np.where(bos_dn.to_numpy(), idx.astype(float), np.nan),
                           index=df.index).ffill()
    recent_bos_up = (idx - bos_up_bar.to_numpy() <= p["fvg_expiry"])
    recent_bos_dn = (idx - bos_dn_bar.to_numpy() <= p["fvg_expiry"])

    # fair-value-gap formation bars, gated by a nearby BOS
    bull_gap = (low > high.shift(2)).fillna(False).to_numpy() & recent_bos_up
    bear_gap = (high < low.shift(2)).fillna(False).to_numpy() & recent_bos_dn

    b_fvg_h = pd.Series(np.where(bull_gap, low.to_numpy(), np.nan),
                        index=df.index).ffill()
    b_fvg_l = pd.Series(np.where(bull_gap, high.shift(2).to_numpy(), np.nan),
                        index=df.index).ffill()
    b_fvg_bar = pd.Series(np.where(bull_gap, idx.astype(float), np.nan),
                          index=df.index).ffill()
    s_fvg_h = pd.Series(np.where(bear_gap, low.shift(2).to_numpy(), np.nan),
                        index=df.index).ffill()
    s_fvg_l = pd.Series(np.where(bear_gap, high.to_numpy(), np.nan),
                        index=df.index).ffill()
    s_fvg_bar = pd.Series(np.where(bear_gap, idx.astype(float), np.nan),
                          index=df.index).ffill()

    b_valid = (idx - b_fvg_bar.to_numpy() <= p["fvg_expiry"])
    s_valid = (idx - s_fvg_bar.to_numpy() <= p["fvg_expiry"])

    long_entries = pd.Series(
        b_valid & (low.to_numpy() <= b_fvg_h.to_numpy())
        & (close.to_numpy() >= b_fvg_l.to_numpy()),
        index=df.index)
    short_entries = pd.Series(
        s_valid & (high.to_numpy() >= s_fvg_l.to_numpy())
        & (close.to_numpy() <= s_fvg_h.to_numpy()),
        index=df.index)

    atr_arr = atr.to_numpy(dtype=float)
    b_fvg_l_arr = b_fvg_l.to_numpy(dtype=float)
    s_fvg_h_arr = s_fvg_h.to_numpy(dtype=float)
    close_arr = close.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    tp_mult, sl_buffer = p["tp_mult"], p["sl_buffer"]

    def long_exit_trigger(j, sig_i, price, frame):
        stop = b_fvg_l_arr[sig_i] - atr_arr[sig_i] * sl_buffer
        target = close_arr[sig_i] + atr_arr[sig_i] * tp_mult
        return bool(low_arr[j] <= stop or high_arr[j] >= target)

    def short_exit_trigger(j, sig_i, price, frame):
        stop = s_fvg_h_arr[sig_i] + atr_arr[sig_i] * sl_buffer
        target = close_arr[sig_i] - atr_arr[sig_i] * tp_mult
        return bool(high_arr[j] >= stop or low_arr[j] <= target)

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, gap-reversal with BOS gate)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/jyhTizLX-Fair-Value-Gap-Strategy-with-Break-of-Structure-Confirmation/",
        tv_author="AIScripts",
        tv_script_name="Fair Value Gap Strategy with Break of Structure Confirmation",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "SL/TP are computed at the SIGNAL bar (stop below gap-bottom minus "
            "slBuffer*ATR, target 2.0*ATR from the entry-bar close) and held "
            "constant -- anchored via base.simulate_positions_both_indexed, "
            "not the engine's next-close fill price",
            "the armed FVG is modeled as a forward-filled latest-formation "
            "series without per-gap consumption tracking, so a re-entry on the "
            "same gap after an early exit inside the expiry window is possible "
            "where the source's `bFvgH := na` reset blocks it [approx]",
        ],
    ),
    build_rule,
)