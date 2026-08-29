"""
strategies/ports/rsi_divergence_ema_filter.py -- port of "RSI Divergence + EMA
Filter [Proozac]" (Proozac98, tv_url https://www.tradingview.com/script/Obv27ppz-RSI-
Divergence-EMA-Filter-Proozac/), source in
storage/tv_scripts/rsi_divergence_ema_filter.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides. Compares the two most recent confirmed RSI/price pivots: a bullish
divergence fires on a new lower-low price pivot with a higher-low RSI pivot,
a bearish one on a higher-high price pivot with a lower-high RSI pivot
(optional (off) gate: the PRIOR pivot's RSI must have been in OB/OS). Signals
are gated by a 200-EMA trend filter (on by default: long above, short below).
Default exit is an ATR trailing stop (trail_points = trail_offset =
trail_mult*ATR, i.e. 2*trail_mult*ATR behind the trade's extreme); an optional
(off) fixed R:R stop/target anchored to the signal-bar close can be enabled
instead/additionally.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. The trailing stop exits when low[j] (long) breaks
   peak_high(fill..j-1) - 2*trail_mult*ATR[j]. ATR is the CURRENT bar's
   because Pine re-submits the exit every bar with live ATR; the trail's
   extreme is the cumulative high/low since the fill, evaluated on the
   previous bar (matching Pine's intrabar order placement).
2. The optional fixed R:R stop/target is computed at the SIGNAL bar (`close -
   atr_mult_sl*atr` etc. inside the `if long_signal` block), so it is anchored
   via base.simulate_positions_both_indexed -- needed because the trailing
   peak and the signal-bar anchor can coexist and drive the same exit.
3. Divergence is evaluated event-wise: each new pivot confirmation is compared
   against the immediately preceding pivot of the same type, which is exactly
   Pine's persisted prev/last tracking.
4. Defaults leave use_trail=True; if both use_trail and use_rr were False the
   strategy would have no exit at all (engine holds to end of data).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema, _rsi
from strategies.ports import base
from strategies.ports.base import atr_wilder, pivot_high, pivot_low, \
    simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "rsi_divergence_ema_filter"

DEFAULT_PARAMS = dict(
    rsi_len=14, pivot_lb=5, pivot_rb=5, rsi_ob=70, rsi_os=30,
    require_ob_os=False, use_ema_filter=True, ema_len=200,
    use_rr=False, rr_ratio=2.0, atr_mult_sl=1.5, atr_len=14,
    use_trail=True, trail_mult=2.0,
)


def _divergence(confirmations: np.ndarray, pivot_values: np.ndarray,
                rsi_at_pivot: np.ndarray, is_bullish: bool,
                require_extreme: bool, extreme_level: float) -> np.ndarray:
    """Event-wise divergence flags on confirmation bars.

    Pine tracks the two most recent pivots (prev/last) of each type; a
    divergence is evaluated ONLY when a fresh pivot confirms. So for each
    confirmation event, compare against the immediately preceding event of the
    same type. Returns a per-bar bool array, True only on confirmation bars.
    """
    events = np.flatnonzero(confirmations)
    out = np.zeros(len(confirmations), dtype=bool)
    if len(events) < 2:
        return out
    pv = pivot_values[events]
    rv = rsi_at_pivot[events]
    if is_bullish:                                 # low/low pivot + rising RSI
        det = (pv[1:] < pv[:-1]) & (rv[1:] > rv[:-1])
    else:                                          # high/high pivot + falling RSI
        det = (pv[1:] > pv[:-1]) & (rv[1:] < rv[:-1])
    if require_extreme:
        prev_r = rv[:-1]
        det = det & (prev_r >= extreme_level if not is_bullish else prev_r <= extreme_level)
    out[events[1:][det]] = True
    return out


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]
    n = len(df)

    rsi_val = _rsi(close, p["rsi_len"]).to_numpy(dtype=float)
    trend_ema = _ema(close, p["ema_len"])
    atr = atr_wilder(high, low, close, p["atr_len"])

    ph = pivot_high(high, p["pivot_lb"], p["pivot_rb"])
    pl = pivot_low(low, p["pivot_lb"], p["pivot_rb"])
    ph_conf = ph.notna().to_numpy()
    pl_conf = pl.notna().to_numpy()
    ph_arr = ph.to_numpy(dtype=float)
    pl_arr = pl.to_numpy(dtype=float)

    # RSI at the pivot bar itself: a pivot is confirmed pivot_rb bars after its
    # extreme bar, and Pine records `rsi_val[pivot_rb]` at confirmation.
    shift = p["pivot_rb"]
    rsi_at_ph = np.full(n, np.nan)
    rsi_at_pl = np.full(n, np.nan)
    e_ph = np.flatnonzero(ph_conf)
    e_pl = np.flatnonzero(pl_conf)
    rsi_at_ph[e_ph] = np.where(e_ph >= shift, rsi_val[e_ph - shift], np.nan)
    rsi_at_pl[e_pl] = np.where(e_pl >= shift, rsi_val[e_pl - shift], np.nan)

    bear_div = _divergence(ph_conf, ph_arr, rsi_at_ph, is_bullish=False,
                           require_extreme=p["require_ob_os"],
                           extreme_level=p["rsi_ob"])
    bull_div = _divergence(pl_conf, pl_arr, rsi_at_pl, is_bullish=True,
                           require_extreme=p["require_ob_os"],
                           extreme_level=p["rsi_os"])

    above_ema = (close > trend_ema).fillna(False)
    use_ema = p["use_ema_filter"]
    long_entries = pd.Series(bull_div & (not use_ema or above_ema), index=df.index)
    short_entries = pd.Series(bear_div & (not use_ema or ~above_ema), index=df.index)

    atr_arr = atr.to_numpy(dtype=float)
    close_arr = close.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    use_rr, use_trail = p["use_rr"], p["use_trail"]
    rr_ratio, atr_mult_sl = p["rr_ratio"], p["atr_mult_sl"]
    trail_dist = 2.0 * p["trail_mult"]

    def long_exit_trigger(j, sig_i, price, frame):
        if use_trail:
            peak = high_arr[sig_i + 1:j].max() if j > sig_i + 1 else price
            if low_arr[j] <= peak - trail_dist * atr_arr[j]:
                return True
        if use_rr:
            sl = close_arr[sig_i] - atr_mult_sl * atr_arr[sig_i]
            tp = close_arr[sig_i] + rr_ratio * atr_mult_sl * atr_arr[sig_i]
            if low_arr[j] <= sl or high_arr[j] >= tp:
                return True
        return False

    def short_exit_trigger(j, sig_i, price, frame):
        if use_trail:
            trough = low_arr[sig_i + 1:j].min() if j > sig_i + 1 else price
            if high_arr[j] >= trough + trail_dist * atr_arr[j]:
                return True
        if use_rr:
            sl = close_arr[sig_i] + atr_mult_sl * atr_arr[sig_i]
            tp = close_arr[sig_i] - rr_ratio * atr_mult_sl * atr_arr[sig_i]
            if high_arr[j] >= sl or low_arr[j] <= tp:
                return True
        return False

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, divergence + EMA filter)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/Obv27ppz-RSI-Divergence-EMA-Filter-Proozac/",
        tv_author="Proozac98",
        tv_script_name="RSI Divergence + EMA Filter [Proozac]",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "trailing exit breaks peak/trough(prior bars) -/+ 2*trail_mult*ATR "
            "with the CURRENT bar's ATR (Pine re-submits the exit each bar with "
            "live ATR)",
            "fixed R:R stop/target is signal-bar-anchored via "
            "simulate_positions_both_indexed (trail peak + signal-bar anchor "
            "can drive the same exit)",
            "divergence is event-wise prev-vs-last per pivot type, matching "
            "Pine's persisted prev/last tracking",
            "defaults: use_trail=True, use_rr=False",
        ],
    ),
    build_rule,
)