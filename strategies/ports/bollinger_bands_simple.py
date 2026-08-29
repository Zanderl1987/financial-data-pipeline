"""
strategies/ports/bollinger_bands_simple.py -- port of "Bollinger Bands Simple
Strategy" (Alby1611, tv_url https://www.tradingview.com/script/GDrB3tu3-
Bollinger-Bands-Simple-Strategy/), source in
storage/tv_scripts/bollinger_bands_simple.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides, mean-reversion fade of the 20-period Bollinger Bands (2 stdev).
Long entry: close closes below the lower band, only while flat. Short entry:
close closes above the upper band, only while flat. Both SL and TP are FULLY
DYNAMIC -- not anchored to the entry price -- recomputed from the CURRENT
bar's values every bar the position is open: SL = recent N-bar swing low/high
+/- an ATR buffer (a trailing stop that widens/tracks with new swing
extremes), TP = the current basis (20-SMA), i.e. exit once price mean-reverts
back to the moving average.

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. Because SL/TP never reference the entry price, no `simulate_positions*`
   entry-price anchor is needed for the exit LEVELS themselves -- the exit
   condition is a pure function of the current bar. `simulate_positions_both`
   is still used, but only for its shared one-position-at-a-time mutual-
   exclusion timeline (matching the source's `strategy.position_size == 0`
   entry gate and the engine's own side='both' semantics), not for anchoring
   a level.
2. `label.new`/`alertcondition`/plotting are cosmetic, not ported.
"""

from __future__ import annotations

import pandas as pd

from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions_both
from strategies.ports import _register, PortInfo

SLUG = "bollinger_bands_simple"

DEFAULT_PARAMS = dict(
    bb_length=20, bb_mult=2.0,
    atr_length=14, sl_atr_mult=1.5,
    swing_lookback=5,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    basis = close.rolling(p["bb_length"]).mean()
    dev = p["bb_mult"] * close.rolling(p["bb_length"]).std(ddof=0)
    upper = basis + dev
    lower = basis - dev
    atr = atr_wilder(high, low, close, p["atr_length"])

    swing_low = low.rolling(p["swing_lookback"]).min()
    swing_high = high.rolling(p["swing_lookback"]).max()

    entries = (close < lower).fillna(False)
    short_entries = (close > upper).fillna(False)

    long_sl = swing_low - atr * p["sl_atr_mult"]
    short_sl = swing_high + atr * p["sl_atr_mult"]
    long_sl_arr = long_sl.to_numpy(dtype=float)
    short_sl_arr = short_sl.to_numpy(dtype=float)
    basis_arr = basis.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)

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
    """Author-default TradeRule (both sides, mean-reversion BB fade)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/GDrB3tu3-Bollinger-Bands-Simple-Strategy/",
        tv_author="Alby1611",
        tv_script_name="Bollinger Bands Simple Strategy",
        mechanism_family="mean_reversion",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "SL/TP are fully dynamic (recomputed from the CURRENT bar every "
            "bar), not anchored to the entry price -- swing-low/high +/- ATR "
            "for the trailing stop, basis (20-SMA) for the mean-reversion "
            "target",
            "simulate_positions_both used only for the shared one-position "
            "mutual-exclusion timeline, not for a level anchor",
        ],
    ),
    build_rule,
)
