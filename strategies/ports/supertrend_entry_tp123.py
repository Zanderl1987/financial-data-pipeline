"""
strategies/ports/supertrend_entry_tp123.py -- port of "Supertrend with Entry,
TP1, TP2 and TP3" (jlockhart1316, tv_url https://www.tradingview.com/script/
njRv2ENC-Supertrend-with-Entry-TP1-TP2-and-TP3/), source in
storage/tv_scripts/supertrend_entry_tp123.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides, flips on the Supertrend direction crossing zero: buy when
`ta.crossunder(direction, 0)` (trend turns up), sell on `ta.crossover`.
Trade levels are anchored to the SIGNAL bar, not the position: entry = close,
stop = entry -/+ ATR * 1.5, and TPs at 1R / 2R / 3R (R = ATR*1.5). Exits scale
out 50% at TP1, 25% at TP2, remainder at TP3, all under the stop. The
author's date-range filter defaults to OFF (`useDateFilterInput=false`), so it
is disabled unless overridden -- it does not restrict the backtest.

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. TP1/TP2/TP3 scale-outs (50/25/rest) collapsed to the FIRST target (1R) or
   the stop -- the full-position engine cannot model partial fills.
2. Because levels anchor to the signal bar (not `position_avg_price`), the
   exit flags are computed directly (no position simulation): forward-filled
   levels from the most recent signal, crossed via high/low touch.
3. Flip entries are dropped by the one-position engine: a sell signal on the
   same bar as a long exit blocks that bar's short entry (next_free rules),
   so the short side only re-enters on the NEXT crossing. Documented, not
   worked around.
4. Stop/TP fills use high/low touch with engine next-close execution.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.ports import base
from strategies.ports import _register, PortInfo

SLUG = "supertrend_entry_tp123"

DEFAULT_PARAMS = dict(
    factor=2.5, atr_period=14, sl_atr_mult=1.5,
    tp1_rr=1.0, tp2_rr=2.0, tp3_rr=3.0,
    use_date_filter=False, start_date="2026-01-01", end_date="2026-12-31",
)


def _crossunder(a: pd.Series, level: float) -> pd.Series:
    return (a < level) & (a.shift(1) >= level)


def _crossover(a: pd.Series, level: float) -> pd.Series:
    return (a > level) & (a.shift(1) <= level)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    _line, direction = base.supertrend(high, low, close,
                                       factor=p["factor"],
                                       atr_period=p["atr_period"])
    buy = _crossunder(direction, 0.0)
    sell = _crossover(direction, 0.0)

    idx = pd.to_datetime(df.index)
    if p["use_date_filter"]:
        in_date = (idx >= pd.Timestamp(p["start_date"])) & \
            (idx <= pd.Timestamp(p["end_date"]))
        buy = buy & in_date
        sell = sell & in_date

    atr = base.atr_wilder(high, low, close, p["atr_period"])
    risk = atr * p["sl_atr_mult"]                      # R = ATR * 1.5

    l_sl = pd.Series(np.where(buy, close - risk, np.nan),
                     index=df.index).ffill()
    l_tp1 = pd.Series(np.where(buy, close + risk * p["tp1_rr"], np.nan),
                      index=df.index).ffill()
    s_sl = pd.Series(np.where(sell, close + risk, np.nan),
                     index=df.index).ffill()
    s_tp1 = pd.Series(np.where(sell, close - risk * p["tp1_rr"], np.nan),
                      index=df.index).ffill()

    long_level_exit = ((high >= l_tp1) | (low <= l_sl)).fillna(False)
    short_level_exit = ((low <= s_tp1) | (high >= s_sl)).fillna(False)

    return {
        "entries": buy,
        "exits": (sell | long_level_exit).fillna(False),
        "short_entries": sell,
        "short_exits": (buy | short_level_exit).fillna(False),
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, flips)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/njRv2ENC-Supertrend-with-Entry-TP1-TP2-and-TP3/",
        tv_author="jlockhart1316",
        tv_script_name="Supertrend with Entry, TP1, TP2 and TP3",
        mechanism_family="trend",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "tiered TP1/TP2/TP3 scale-outs collapsed to first target (1R) or stop",
            "levels anchor to the signal bar (author design), forward-filled",
            "flip entries on the same bar as an opposing exit are dropped by the one-position engine",
            "stop/TP fills use high/low touch with engine next-close execution",
            "author date-range filter off by default, applied only if overridden",
        ],
    ),
    build_rule,
)
