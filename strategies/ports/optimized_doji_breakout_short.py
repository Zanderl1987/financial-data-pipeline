"""
strategies/ports/optimized_doji_breakout_short.py -- port of "Optimized Doji
Breakout Strategy (Short Only) V6" (deependrasrivastavalko, tv_url
https://www.tradingview.com/script/CzLuDuzf-Optimized-Doji-Breakout-Strategy-
Short-Only-V6/), source in
storage/tv_scripts/optimized_doji_breakout_short.pine.

Author design (from source, verbatim)
-------------------------------------
Short-only. A confirmed Doji (body <= 10% of its own range) arms a one-bar
trigger; if the very next candle closes red, enter short. Stop-loss is fixed
at the Doji candle's own HIGH; take-profit is a dynamic R-multiple off that
risk (3.0x in a high-volatility regime -- ATR above its 20-bar average --
else 1.5x), evaluated at the signal bar. An "emergency exit" closes the
position immediately on the first green candle close, regardless of the
stop/target. The arm-trigger self-cancels if the bar after the Doji is NOT
red (never entered) -- but since the trigger only ever looks one bar ahead,
this cancellation is structurally equivalent to just checking "was the PRIOR
bar a confirmed Doji" at each bar, with no separate cancellation state to
model.

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. The source computes `entry_price := open` (the signal bar's OWN open) and
   sizes risk off that value, expecting a same-bar fill. That assumption
   doesn't hold even in TradingView's own execution model (a market order
   submitted during bar t's script evaluation fills at bar t+1's open, not
   bar t's), and doesn't hold for this campaign's engine either, which fills
   at the CLOSE of the bar after the signal. The port uses the engine's own
   next-close fill (via `base.simulate_positions`) as the entry price for
   the R-multiple risk calculation, rather than replicating the source's
   own-open assumption.
2. The stop level (Doji high) and the volatility-regime R-multiple are both
   "locked in" at the signal bar and held constant for the life of the trade
   via a forward-filled series, matching the precedent in
   strategies/ports/hybrid_breakout_vcp.py and supertrend_entry_tp123.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions
from strategies.ports import _register, PortInfo

SLUG = "optimized_doji_breakout_short"

DEFAULT_PARAMS = dict(
    atr_length=14, atr_ma_length=20,
    doji_body_ratio_max=0.10, rr_high_vol=3.0, rr_low_vol=1.5,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    open_, high, low, close = df["open"], df["high"], df["low"], df["close"]

    atr = atr_wilder(high, low, close, p["atr_length"])
    atr_ma = atr.rolling(p["atr_ma_length"]).mean()
    rr = pd.Series(np.where(atr > atr_ma, p["rr_high_vol"], p["rr_low_vol"]),
                   index=df.index)

    candle_body = (close - open_).abs()
    candle_range = high - low
    is_doji = (candle_range > 0) & \
        ((candle_body / candle_range) <= p["doji_body_ratio_max"])

    is_red_followup = close < open_
    entries = (is_doji.shift(1).fillna(False) & is_red_followup).fillna(False)

    doji_high_at_signal = pd.Series(
        np.where(entries, high.shift(1), np.nan), index=df.index).ffill()
    rr_at_signal = pd.Series(
        np.where(entries, rr, np.nan), index=df.index).ffill()

    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    open_arr = open_.to_numpy(dtype=float)
    close_arr = close.to_numpy(dtype=float)
    sl_arr = doji_high_at_signal.to_numpy(dtype=float)
    rr_arr = rr_at_signal.to_numpy(dtype=float)

    def exit_trigger(j, price, frame):
        sl = sl_arr[j]
        risk = sl - price
        tp = price - risk * rr_arr[j]
        green = close_arr[j] >= open_arr[j]
        return bool(high_arr[j] >= sl or low_arr[j] <= tp or green)

    walk = simulate_positions(entries, close, exit_trigger, df)
    return {"entries": entries, "exits": walk.exits, "entry_price": walk.entry_price}


def build_rule(params: dict = None):
    """Author-default TradeRule (short-only)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="short",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/CzLuDuzf-Optimized-Doji-Breakout-Strategy-Short-Only-V6/",
        tv_author="deependrasrivastavalko",
        tv_script_name="Optimized Doji Breakout Strategy (Short Only) V6",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "entry price uses the engine's own next-close fill (base.simulate_"
            "positions), not the source's own-bar-open assumption (which "
            "doesn't hold even under TradingView's own execution model)",
            "stop (Doji high) and volatility-regime R-multiple are locked in "
            "at the signal bar via forward-fill, held constant for the trade",
            "the source's trigger self-cancellation needs no separate state: "
            "since it only ever looks one bar ahead, it collapses to "
            "'was the prior bar a confirmed Doji'",
        ],
    ),
    build_rule,
)
