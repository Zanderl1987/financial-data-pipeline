"""
strategies/ports/capitulation_stretch_reversion.py -- port of "Capitulation
Stretch Reversion [Jayadev Rana]" (bluealgocapital, tv_url https://www.
tradingview.com/script/WWuTjyDx-Capitulation-Stretch-Reversion-Jayadev-Rana/),
source in storage/tv_scripts/capitulation_stretch_reversion.pine.

Author design (from source, verbatim)
-------------------------------------
Mean-reversion in the direction of the dominant drift: longs only while close
is above the 200-EMA trend line (bearish regime shorts, OFF by default). A
setup arms when close is stretched at least stretchMult*ATR(14) away from a
5-EMA reversion mean AND capBars consecutive lower closes confirm capitulation
(downRun) AND the setup bar itself reverses up (green bar). Entry is a market
order on the confirmed close (fills next open). In-position exits: a hard
protective stop stopMult*ATR from the entry close (intrabar), the mean target
(a close back at/above the 5-EMA), and a maxBars time stop (market close).
Regime alignment is evaluated at the entry bar only.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. downRun/upRun streak counters are precomputed groupby-cumcount series
   (Pine's persistent var counters), reset the same way (close vs close[1],
   first bar False).
2. The stop is anchored to the SIGNAL bar's close and ATR (stopPx := close -
   stopMult*atr on the entry bar) and checked against each bar's LOW from the
   fill bar on (intrabar semantics approximated as a bar-level breach;
   engine fills the exit at the next close).
3. The mean target and time stop are close-based, gated on barstate.
   isconfirmed, mapping to the same trigger; the time stop fires on the bar
   where barsHeld >= maxBars, i.e. j - sig_i >= maxBars (fill bar counts the
   first held bar with barsHeld = 1).
4. Dashboard/display blocks are plot-only, not ported. tradeShorts is OFF by
   default per the source tooltip (index-style instruments); enabling it
   makes the rule two-sided.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema, _rma, _sma
from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "capitulation_stretch_reversion"

DEFAULT_PARAMS = dict(
    trend_len=200, mean_len=5, atr_len=14,
    stretch_mult=0.5, cap_bars=2,
    stop_mult=3.0, max_bars=10,
    trade_longs=True, trade_shorts=False,
)


def _streaks(mask: pd.Series) -> pd.Series:
    """Consecutive-True run length ending at each bar (0 where mask is False)."""
    mask = mask.fillna(False)
    grp = (mask != mask.shift(1, fill_value=False)).cumsum()
    return mask.groupby(grp).cumcount() + 1


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    open_, high, low, close = df["open"], df["high"], df["low"], df["close"]

    mean = _ema(close, p["mean_len"])
    trend = _ema(close, p["trend_len"])
    atr = atr_wilder(high, low, close, p["atr_len"])

    down_run = _streaks(close < close.shift(1))
    up_run = _streaks(close > close.shift(1))

    regime_bull = close > trend
    regime_bear = close < trend

    stretch_long = close < mean - p["stretch_mult"] * atr
    stretch_short = close > mean + p["stretch_mult"] * atr
    rev_bar_up = close > open_
    rev_bar_dn = close < open_

    long_setup = (p["trade_longs"] & regime_bull & stretch_long
                  & (down_run >= p["cap_bars"]) & rev_bar_up)
    short_setup = (p["trade_shorts"] & regime_bear & stretch_short
                   & (up_run >= p["cap_bars"]) & rev_bar_dn)

    long_entries = long_setup.fillna(False)
    short_entries = short_setup.fillna(False)

    close_arr = close.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    mean_arr = mean.to_numpy(dtype=float)
    atr_arr = atr.to_numpy(dtype=float)
    stop_mult = p["stop_mult"]
    max_bars = p["max_bars"]

    def long_exit_trigger(j, sig_i, price, frame):
        stop = close_arr[sig_i] - stop_mult * atr_arr[sig_i]
        if low_arr[j] <= stop:
            return True
        if close_arr[j] >= mean_arr[j]:
            return True
        return j - sig_i >= max_bars

    def short_exit_trigger(j, sig_i, price, frame):
        stop = close_arr[sig_i] + stop_mult * atr_arr[sig_i]
        if high_arr[j] >= stop:
            return True
        if close_arr[j] <= mean_arr[j]:
            return True
        return j - sig_i >= max_bars

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger,
        df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (long-only, shorts OFF per source default)."""
    merged = {**DEFAULT_PARAMS, **(params or {})}
    side = "both" if merged["trade_shorts"] else "long"
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side=side,
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/WWuTjyDx-Capitulation-Stretch-Reversion-Jayadev-Rana/",
        tv_author="bluealgocapital",
        tv_script_name="Capitulation Stretch Reversion [Jayadev Rana]",
        mechanism_family="mean_reversion",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "stops anchored to the signal-bar close & ATR (stopPx set at "
            "entry), checked bar-level against low/high from fill bar on",
            "mean target & time stop are close-based; time stop fires when "
            "j - sig_i >= max_bars (fill bar = held bar 1)",
            "downRun/upRun streaks vectorized to groupby-cumcount",
            "tradeShorts OFF at default (per source tooltip); enabling makes "
            "the rule two-sided",
        ],
    ),
    build_rule,
)