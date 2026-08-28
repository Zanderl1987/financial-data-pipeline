"""
strategies/ports/joey_stochrsi_atr.py -- port of "Joey Strategy" (Geckin,
tv_url https://www.tradingview.com/script/eDyfoKkr-Joey-Strategy/), source in
storage/tv_scripts/joey_stochrsi_atr.pine.

Author design (from source, verbatim)
-------------------------------------
Long-only. Stochastic %K (smoothed 3) crosses above %D (smoothed 3) while %K
is still below 30 (oversold territory) -- market-order entry. Take-profit and
stop-loss are symmetric 1.5x ATR(14) off the entry price, both locked in the
instant the position opens and held for the life of the trade (no trailing).
No `request.security`, no session logic. Author's doc: "The strategy can
work on any candlestick timeframe, but it was originally designed for
1-day candlesticks."

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. Entry price is the engine's own next-close fill (`base.simulate_positions`),
   used as the anchor for both the ATR stop and target -- matching the
   source's `strategy.position_avg_price` anchor exactly, since a
   next-close-fill engine has no separate concept of a different fill price.
2. ATR is measured once at the moment a NEW position opens (`newLongPosition`
   in the source) and held fixed for the trade; the port locks the ATR value
   at the entry bar via `base.simulate_positions`' own entry-price snapshot
   rather than recomputing it every bar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _sma
from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions
from strategies.ports import _register, PortInfo

SLUG = "joey_stochrsi_atr"

DEFAULT_PARAMS = dict(
    stoch_length=14, oversold=30, smooth_k=3, smooth_d=3,
    atr_length=14, atr_multiplier=1.5,
)


def _stoch_k(high: pd.Series, low: pd.Series, close: pd.Series,
            length: int, smooth_k: int) -> pd.Series:
    ll = low.rolling(length).min()
    hh = high.rolling(length).max()
    raw = 100 * (close - ll) / (hh - ll)
    return _sma(raw, smooth_k)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    k = _stoch_k(high, low, close, p["stoch_length"], p["smooth_k"])
    d = _sma(k, p["smooth_d"])

    long_signal = ((k > d) & (k.shift(1) <= d.shift(1)) & (k < p["oversold"])) \
        .fillna(False)

    atr = atr_wilder(high, low, close, p["atr_length"])
    # ATR value locked at the SIGNAL bar (the source reads it the instant the
    # position opens, one bar before the engine's own next-close fill).
    atr_at_signal = pd.Series(
        np.where(long_signal, atr, np.nan), index=df.index).ffill()
    atr_arr = atr_at_signal.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    mult = p["atr_multiplier"]

    def exit_trigger(j, price, frame):
        risk = atr_arr[j] * mult
        return bool(high_arr[j] >= price + risk or low_arr[j] <= price - risk)

    walk = simulate_positions(long_signal, close, exit_trigger, df)
    return {"entries": long_signal, "exits": walk.exits,
            "entry_price": walk.entry_price}


def build_rule(params: dict = None):
    """Author-default TradeRule (long-only)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="long",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/eDyfoKkr-Joey-Strategy/",
        tv_author="Geckin",
        tv_script_name="Joey Strategy",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "TP/SL anchored to the engine's own next-close entry fill via "
            "base.simulate_positions, matching the source's position_avg_price",
            "ATR is locked at the signal bar (one bar before the engine's own "
            "fill), matching the source's own-instant-of-entry measurement",
        ],
    ),
    build_rule,
)
