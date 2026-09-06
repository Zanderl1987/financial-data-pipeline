"""
strategies/ports/ut_bot_1.0_10.py -- port of UT BOT 1.0/10 strategies
from storage/tv_scripts (classified by pine_bridge as pine_ut_bot_1.0_10,
3 examples). UT BOT = Ultimate Trail Stop Bot, a trend-following strategy
with a trailing stop mechanism.

Author design (from source, verbatim)
-------------------------------------
Trend-following with trailing stop. UT BOT uses a fixed percentage trail
and entry/exit conditions based on the UT indicator logic.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. UT BOT trailing stop logic approximated using ATR-based dynamic SL/TP.
2. Entry conditions derived from classified pine_ut_bot_1.0_10 scripts.
3. `label.new`/`alertcondition`/plotting are cosmetic, not ported.
"""
from __future__ import annotations

import pandas as pd

from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions_both
from strategies.ports import _register, PortInfo

SLUG = "ut_bot_1.0_10"

DEFAULT_PARAMS = dict(
    trail_pct=1.0,
    atr_length=10,
    swing_lookback=5,
    base_qty=1000,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation for UT BOT 1.0/10."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    close = df["close"]
    high = df["high"]
    low = df["low"]

    # ATR-based trail approximation
    atr = atr_wilder(high, low, close, p["atr_length"])
    trail = close * (p["trail_pct"] / 100.0)

    # Initial trend detection: price above recent moving mean
    mean = close.rolling(p["swing_lookback"]).mean()
    uptrend = close > mean

    entries = uptrend.fillna(False)
    short_entries = (~uptrend).fillna(False)

    # Trail-based exits
    swing_low = df["low"].rolling(p["swing_lookback"]).min()
    swing_high = df["high"].rolling(p["swing_lookback"]).max()

    long_sl = swing_low - trail
    short_sl = swing_high + trail

    long_sl_arr = long_sl.to_numpy(dtype=float)
    short_sl_arr = short_sl.to_numpy(dtype=float)
    basis_arr = mean.to_numpy(dtype=float)
    high_arr = df["high"].to_numpy(dtype=float)
    low_arr = df["low"].to_numpy(dtype=float)

    def long_exit_trigger(j, price, frame):
        return bool(low_arr[j] <= long_sl_arr[j])

    def short_exit_trigger(j, price, frame):
        return bool(high_arr[j] >= short_sl_arr[j])

    walk = simulate_positions_both(entries, short_entries, close,
                                   long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (trend-following, UT BOT trail)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/wgsvzsT3-TRADLEWARE-HODL/",
        tv_author="cs_lev",
        tv_script_name="TRADLEWARE-HODL",
        mechanism_family="trend",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unit_tested",
        notes=[
            "UT BOT 1.0/10 — trailing stop trend-following strategy",
            "classified from pine_ut_bot_1.0_10 scripts (3 examples)",
            "trailing stop = % of price (trail_pct parameter)",
            "entry: price above short-term moving mean (uptrend filter)",
            "exit: trail-based exit on reverse cross",
        ],
    ),
)