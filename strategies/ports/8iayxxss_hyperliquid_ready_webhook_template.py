"""
strategies/ports/8iayxxss_hyperliquid_ready_webhook_template.py -- port of
"Hyperliquid-Ready Webhook Strategy Template [PopsPineDev]" (PopsPineDev, tv_url
https://www.tradingview.com/script/8iAYXXsS-Hyperliquid-Ready-Webhook-Strategy-
Template/), source in storage/tv_scripts/8iayxxss_hyperliquid_ready_webhook_template.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides, deliberately simple EMA-cross + RSI-filter logic (the script's real
value is the webhook payload plumbing, which is an alert/ops concern, not a
trading rule). Long: 21-EMA crosses above 55-EMA AND RSI(14) >= 50. Short:
21-EMA crosses below 55-EMA AND RSI(14) <= 50. allowLongs/allowShorts toggles
both default ON. SL/TP levels are captured at the signal bar: longSL =
close - slMult*ATR, longTP = close + tpMult*ATR (slMult 1.5, tpMult 3.0,
ATR(14)), and fired later from the entry's strategy.exit(..., stop, limit).
Leaving/leveraging both keys off produces no trades.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. The SL/TP are computed inside the entry `if` block, i.e. anchored to the
   SIGNAL bar close, so they are placed via base.simulate_positions_both_indexed
   and trigger whenever the stop or limit level is crossed on a later bar.
2. qtyPct/levX/hookSecret are webhook-payload fields with no backtest role in
   the source's own simulator (percent-of-equity sizing is engine policy
   anyway); they are not ported. On-entry alert messages carry the same levels
   as the exit order, so entry anchoring is consistent with the source.
3. The signal fires on the confirmed bar (process_orders_on_close) -- same
   semantics as the engine's next-close fill.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema, _rsi
from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "8iayxxss_hyperliquid_ready_webhook_template"

DEFAULT_PARAMS = dict(
    fast_len=21, slow_len=55, rsi_len=14,
    rsi_long_min=50, rsi_short_max=50,
    atr_len=14, sl_mult=1.5, tp_mult=3.0,
    allow_longs=True, allow_shorts=True,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    ema_fast = _ema(close, p["fast_len"])
    ema_slow = _ema(close, p["slow_len"])
    rsi = _rsi(close, p["rsi_len"])
    atr = atr_wilder(high, low, close, p["atr_len"])

    cross_up = (ema_fast > ema_slow) & (ema_fast.shift(1) <= ema_slow.shift(1))
    cross_dn = (ema_fast < ema_slow) & (ema_fast.shift(1) >= ema_slow.shift(1))

    long_entries = (cross_up & (rsi >= p["rsi_long_min"])).fillna(False)
    short_entries = (cross_dn & (rsi <= p["rsi_short_max"])).fillna(False)
    if not p["allow_longs"]:
        long_entries = pd.Series(False, index=df.index)
    if not p["allow_shorts"]:
        short_entries = pd.Series(False, index=df.index)

    atr_arr = atr.to_numpy(dtype=float)
    close_arr = close.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)

    def long_exit_trigger(j, sig_i, price, frame):
        sl = close_arr[sig_i] - p["sl_mult"] * atr_arr[sig_i]
        tp = close_arr[sig_i] + p["tp_mult"] * atr_arr[sig_i]
        return low_arr[j] <= sl or high_arr[j] >= tp

    def short_exit_trigger(j, sig_i, price, frame):
        sl = close_arr[sig_i] + p["sl_mult"] * atr_arr[sig_i]
        tp = close_arr[sig_i] - p["tp_mult"] * atr_arr[sig_i]
        return high_arr[j] >= sl or low_arr[j] <= tp

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, EMA cross + RSI filter)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/8iAYXXsS-Hyperliquid-Ready-Webhook-Strategy-Template/",
        tv_author="PopsPineDev",
        tv_script_name="Hyperliquid-Ready Webhook Strategy Template [PopsPineDev]",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "SL/TP captured inside the entry block -> signal-bar-anchored via "
            "simulate_positions_both_indexed",
            "webhook-payload fields (qtyPct, levX, hookSecret) are ops/alert "
            "concerns with no backtest role; not ported",
            "confirmed-bar signals (process_orders_on_close) match the engine's "
            "next-close fill semantics",
        ],
    ),
    build_rule,
)