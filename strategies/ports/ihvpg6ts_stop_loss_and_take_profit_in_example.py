"""
strategies/ports/ihvpg6ts_stop_loss_and_take_profit_in_example.py -- port of
"Stop loss and Take Profit in $$ example" (adolgov, tv_url
https://www.tradingview.com/script/IIfXBy7H-Stop-Loss-and-Take-Profit-in-example/),
source in storage/tv_scripts/ihvpg6ts_stop_loss_and_take_profit_in_example.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides, moving-average cross entries: buy when SMA(14) crosses ABOVE
SMA(28), short when it crosses BELOW. Exits are fixed DOLLAR amounts converted
to points at entry time (`moneyToSLPoints`): take-profit $200, stop-loss $100,
anchored to the entry price (`strategy.position_avg_price`, legacy non-`close()`
exit style that stays active once placed).

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. The dollar->points conversion needs `syminfo.pointvalue` (instrument-
   specific) and the position SIZE the strategy would hold. The engine holds a
   fixed notional ($10,000) per trade, so the accurate reading is
   dollars / notional: TP = +2% ($200 / $10k), SL = -1% ($100 / $10k). Ported
   as entry-anchored percentage levels, not the exact point math TV would use.
2. `strategy.exit`'s stop/limit are intrabar orders; the engine fills at the
   next bar's close. Touch is detected on high/low, execution dated next close.
3. The plot/fill calls are cosmetic, not ported.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.ports import base
from strategies.ports import _register, PortInfo

SLUG = "ihvpg6ts_stop_loss_and_take_profit_in_example"

DEFAULT_PARAMS = dict(
    fast=14, slow=28,
    sl_pct=0.01,      # $100 / $10,000 notional
    tp_pct=0.02,      # $200 / $10,000 notional
)


def _cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    """SMA fast crossing above SMA slow; both shifted, warmup-safe NaN."""
    return (a > b) & (a.shift(1) <= b.shift(1))


def _cross_down(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    fast = close.rolling(p["fast"]).mean()
    slow = close.rolling(p["slow"]).mean()

    entries = _cross_up(fast, slow).fillna(False)
    short_entries = _cross_down(fast, slow).fillna(False)

    # Entry-anchored fixed-dollar SL/TP, as percents of the fixed notional.
    # Levels live on bars where an engine entry exists (NaN elsewhere); the
    # position walk passes the actual entry price so the trigger is exact.
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)

    def long_trigger(j, price, frame):
        return bool(low_arr[j] <= price * (1 - p["sl_pct"])
                    or high_arr[j] >= price * (1 + p["tp_pct"]))

    def short_trigger(j, price, frame):
        return bool(high_arr[j] >= price * (1 + p["sl_pct"])
                    or low_arr[j] <= price * (1 - p["tp_pct"]))

    walk = base.simulate_positions_both(
        entries, short_entries, close,
        long_trigger, short_trigger, df)

    return {
        "entries": entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, MA cross + $-SL/TP)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/IIfXBy7H-Stop-Loss-and-Take-Profit-in-example/",
        tv_author="adolgov",
        tv_script_name="Stop loss and Take Profit in $$ example",
        mechanism_family="trend",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "fixed-$ SL/TP translated to % of the engine's fixed $10k "
            "notional: TP +2% ($200), SL -1% ($100); TV's exact point math "
            "needs syminfo.pointvalue + a position size the engine does not have",
            "entry-anchored levels; stop/limit touched on high/low, filled at engine next close",
        ],
    ),
    build_rule,
)