"""
strategies/ports/ucgxklvt_ma_crossover_rsi.py -- port of "MA Crossover + RSI
Strategy" (clayton1139, tv_url https://www.tradingview.com/script/UCGXkLvt-
MA-Crossover-RSI-Strategy/), source in
storage/tv_scripts/ucgxklvt_ma_crossover_rsi.pine.

Author design (from source, verbatim)
-------------------------------------
Long-only. Entry on EMA(9)/EMA(21) bullish crossover while RSI(14) < 70
(avoids buying already-overbought). Exit on the bearish crossunder while
RSI > 30, OR a 2% stop-loss / 4% take-profit anchored to
`strategy.position_avg_price` (both enabled by default).

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. Entry price for the SL/TP anchor is the engine's own next-close fill,
   replayed via `base.simulate_positions` so the levels are computed from
   exactly the price the engine will have paid -- not re-derived independently.
2. The RSI-gated bearish crossunder (`sellSignal`) is folded into the same
   exit-trigger callback as the SL/TP levels, so all three exit conditions
   are evaluated together per bar, matching the source's execution order
   (close-signal `strategy.close` and the `strategy.exit` stop/limit order
   are both live simultaneously once a position is open).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema, _rsi
from strategies.ports import base
from strategies.ports.base import simulate_positions
from strategies.ports import _register, PortInfo

SLUG = "ucgxklvt_ma_crossover_rsi"

DEFAULT_PARAMS = dict(
    fast_len=9, slow_len=21, rsi_len=14, rsi_ob=70, rsi_os=30,
    use_sl=True, sl_pct=2.0, use_tp=True, tp_pct=4.0,
)


def _crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def _crossunder(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    close, high, low = df["close"], df["high"], df["low"]

    fast_ma = _ema(close, p["fast_len"])
    slow_ma = _ema(close, p["slow_len"])
    rsi = _rsi(close, p["rsi_len"])

    bull_cross = _crossover(fast_ma, slow_ma)
    bear_cross = _crossunder(fast_ma, slow_ma)

    entries = (bull_cross & (rsi < p["rsi_ob"])).fillna(False)
    sell_signal = (bear_cross & (rsi > p["rsi_os"])).fillna(False)

    sell_arr = sell_signal.to_numpy()
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    use_sl, sl_pct = p["use_sl"], p["sl_pct"] / 100.0
    use_tp, tp_pct = p["use_tp"], p["tp_pct"] / 100.0

    def exit_trigger(j, price, frame):
        if sell_arr[j]:
            return True
        if use_sl and low_arr[j] <= price * (1 - sl_pct):
            return True
        if use_tp and high_arr[j] >= price * (1 + tp_pct):
            return True
        return False

    walk = simulate_positions(entries, close, exit_trigger, df)
    return {"entries": entries, "exits": walk.exits, "entry_price": walk.entry_price}


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
        tv_url="https://www.tradingview.com/script/UCGXkLvt-MA-Crossover-RSI-Strategy/",
        tv_author="clayton1139",
        tv_script_name="MA Crossover + RSI Strategy",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "SL/TP anchored to the engine's own next-close entry fill via "
            "base.simulate_positions, not an independently re-derived price",
            "RSI-gated bearish crossunder and the SL/TP levels are evaluated "
            "together per bar inside one exit-trigger callback",
        ],
    ),
    build_rule,
)
