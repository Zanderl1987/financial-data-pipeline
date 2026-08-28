"""
strategies/ports/high_activity_penny_stock.py -- port of "High Activity Penny
Stock Strategy V6" (Pridarasx, tv_url https://www.tradingview.com/script/
NfihxKxr-High-Activity-Penny-Stock-Strategy-V6/), source in
storage/tv_scripts/high_activity_penny_stock.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides. Entry on a Supertrend(2.0, 10) direction flip (long on the
flip-to-uptrend bar, short on flip-to-downtrend), gated by a volume filter
(volume > 1.2x its 20-bar average) and a 50-SMA trend filter (close above/
below the SMA for long/short respectively; both filters default ON). Exit is
the FIRST of: a 10%/5% take-profit/stop-loss anchored to
`strategy.position_avg_price` (always active), or an optional (default ON)
"trend flip" exit that closes as soon as the Supertrend direction reverts,
independent of the gated entry conditions. "Penny stock" is marketing, not a
mechanism lock -- generic, equity-portable.

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. Entry price for the TP/SL anchor is the engine's own next-close fill,
   replayed via `base.simulate_positions_both` -- which merges the long and
   short signal streams into ONE mutually-exclusive timeline, exactly like
   evaluation.trades.simulate_symbol does for side='both' rules (a short
   signal while long is still open is dropped, and vice versa). Running the
   two sides through independent position walks would miss that shared gate.
2. The "exit on trend flip" condition is a LEVEL check on the raw Supertrend
   direction (matching the source's `strategy.position_size > 0 and
   stDirection > 0`), not an edge -- it stays true every bar the position is
   on the wrong side of the trend, same as the source.
"""

from __future__ import annotations

import pandas as pd

from analytics.technical import _sma
from strategies.ports import base
from strategies.ports.base import simulate_positions_both, supertrend
from strategies.ports import _register, PortInfo

SLUG = "high_activity_penny_stock"

DEFAULT_PARAMS = dict(
    st_atr_period=10, st_multiplier=2.0,
    vol_threshold=1.2, use_vol_filter=True,
    sma_length=50, use_sma_filter=True,
    tp_pct=10.0, sl_pct=5.0, exit_on_flip=True,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]

    _line, direction = supertrend(high, low, close,
                                  factor=p["st_multiplier"],
                                  atr_period=p["st_atr_period"])
    avg_volume = _sma(volume, 20)
    sma_value = _sma(close, p["sma_length"])

    vol_ok = (not p["use_vol_filter"]) | (volume > avg_volume * p["vol_threshold"])
    trend_ok_buy = (not p["use_sma_filter"]) | (close > sma_value)
    trend_ok_sell = (not p["use_sma_filter"]) | (close < sma_value)

    buy_signal = (direction < 0) & (direction.shift(1) >= 0) & vol_ok & trend_ok_buy
    sell_signal = (direction > 0) & (direction.shift(1) <= 0) & vol_ok & trend_ok_sell

    entries = buy_signal.fillna(False)
    short_entries = sell_signal.fillna(False)

    dir_arr = direction.to_numpy()
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    tp_pct, sl_pct = p["tp_pct"] / 100.0, p["sl_pct"] / 100.0
    exit_on_flip = p["exit_on_flip"]

    def long_exit_trigger(j, price, frame):
        if low_arr[j] <= price * (1 - sl_pct) or high_arr[j] >= price * (1 + tp_pct):
            return True
        if exit_on_flip and dir_arr[j] > 0:
            return True
        return False

    def short_exit_trigger(j, price, frame):
        if high_arr[j] >= price * (1 + sl_pct) or low_arr[j] <= price * (1 - tp_pct):
            return True
        if exit_on_flip and dir_arr[j] < 0:
            return True
        return False

    walk = simulate_positions_both(entries, short_entries, close,
                                   long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, independent long/short position walks)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/NfihxKxr-High-Activity-Penny-Stock-Strategy-V6/",
        tv_author="Pridarasx",
        tv_script_name="High Activity Penny Stock Strategy V6",
        mechanism_family="trend",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "TP/SL anchored to the engine's own next-close entry fill via "
            "base.simulate_positions",
            "trend-flip exit is a level check on Supertrend direction, matching "
            "the source (stays true every bar on the wrong side, not an edge)",
            "'penny stock' marketing is not a mechanism lock -- generic, "
            "equity-portable per the collection-time note",
        ],
    ),
    build_rule,
)
