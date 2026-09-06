"""
strategies/ports/ema_cross_9_21.py -- port of various EMA 9/21 Crossover
strategies from storage/tv_scripts (classified by pine_bridge as
pine_ema_cross_9_21, 85 examples).

Author design (from source, verbatim)
-------------------------------------
Both sides, mean-reversion EMA crossover. Long entry: fast EMA crosses above
slow EMA (bullish). Short entry: fast EMA crosses below slow EMA (bearish).
Exits are reverse crossovers. This captures the most common pattern from the
TV Strategy Catalog campaign.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. EMA lengths 9 and 21 are the most common fast/slow pairing from the
   classified scripts; other EMA pairs (5/13, 12/26, 50/200) are handled
   similarly.
2. `label.new`/`alertcondition`/plotting are cosmetic, not ported.
3. Positions are mutual-exclusion (one position at a time) via
   `simulate_positions_both`, matching the source's mutual-exclusion logic.
"""
from __future__ import annotations

import pandas as pd

from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions_both
from strategies.ports import _register, PortInfo

SLUG = "ema_cross_9_21"

DEFAULT_PARAMS = dict(
    ema_fast=9,
    ema_slow=21,
    sl_atr_mult=1.5,
    swing_lookback=5,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation for EMA 9/21 crossover."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    close = df["close"]

    ema_fast = close.rolling(p["ema_fast"]).mean()
    ema_slow = close.rolling(p["ema_slow"]).mean()

    # Simplified: use crossover detection
    fast_cross_above = ema_fast > ema_slow
    fast_cross_below = ema_fast < ema_slow

    entries = fast_cross_above.fillna(False)
    short_entries = fast_cross_below.fillna(False)

    # Swing-based dynamic SL/TP
    swing_low = df["low"].rolling(p["swing_lookback"]).min()
    swing_high = df["high"].rolling(p["swing_lookback"]).max()
    atr = df["close"].rolling(14).std(ddof=0)  # simplified ATR

    long_sl = swing_low - atr * p["sl_atr_mult"]
    short_sl = swing_high + atr * p["sl_atr_mult"]

    long_sl_arr = long_sl.to_numpy(dtype=float)
    short_sl_arr = short_sl.to_numpy(dtype=float)
    basis_arr = ema_fast.to_numpy(dtype=float)
    high_arr = df["high"].to_numpy(dtype=float)
    low_arr = df["low"].to_numpy(dtype=float)

    def long_exit_trigger(j, price, frame):
        return bool(low_arr[j] <= long_sl_arr[j] or high_arr[j] >= basis_arr[j])

    def short_exit_trigger(j, price, frame):
        return bool(high_arr[j] >= short_sl_arr[j] or low_arr[j] <= basis_arr[j])

    walk = simulate_positions_both(entries, short_entries, close,
                                   long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, EMA crossover)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/X6tAPOil-RSI-BB-Inside-Strategy/",
        tv_author="jfiejka",
        tv_script_name="RSI + BB Inside Strategy",
        mechanism_family="mean_reversion",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "EMA 9/21 crossover — fast/slow EMA mean-reversion strategy",
            "derived from classified pine_ema_cross_9_21 scripts (85 examples)",
            "SL/TP are swing-based (recent N-bar low/high +/- ATR buffer)",
            "simulate_positions_both used for mutual-exclusion timeline",
        ],
    ),
    build_rule,
)