"""
strategies/ports/rsi_bb_inside_strategy.py -- port of "RSI + BB Inside
Strategy" (jfiejka, tv_url https://www.tradingview.com/script/X6tAPOil-RSI-
BB-Inside-Strategy/), source in storage/tv_scripts/rsi_bb_inside_strategy.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides. Long when RSI(14) < 30 AND close is inside the Bollinger Bands
(20, 2) -- strictly between the lower and upper band, i.e. "oversold but not
already crashing through the band" (avoids catching a falling knife). Short
is the mirror: RSI > 70 and still inside the bands. Exit (either side) on
`ta.cross(close, bbBasis)` -- a two-way crossing of the BB basis (midline) in
EITHER direction, used identically for both the long and short exit.

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. `ta.cross` (direction-agnostic) is implemented as
   crossover(close, basis) OR crossunder(close, basis) -- true the bar the
   two series change relative order, either way.
2. No stop-loss/take-profit; the sole exit is the basis cross, exactly as
   the source has it applying to both directions.
"""

from __future__ import annotations

import pandas as pd

from analytics.technical import _rsi, _sma
from strategies.ports import base
from strategies.ports import _register, PortInfo

SLUG = "rsi_bb_inside_strategy"

DEFAULT_PARAMS = dict(rsi_len=14, bb_len=20, bb_mult=2.0)


def _bb(close: pd.Series, length: int, mult: float):
    basis = _sma(close, length)
    dev = close.rolling(length).std(ddof=0) * mult
    return basis, basis + dev, basis - dev


def _cross(a: pd.Series, b: pd.Series) -> pd.Series:
    up = (a > b) & (a.shift(1) <= b.shift(1))
    down = (a < b) & (a.shift(1) >= b.shift(1))
    return up | down


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    close = df["close"]

    rsi = _rsi(close, p["rsi_len"])
    basis, upper, lower = _bb(close, p["bb_len"], p["bb_mult"])
    inside = (close > lower) & (close < upper)

    entries = ((rsi < 30) & inside).fillna(False)
    short_entries = ((rsi > 70) & inside).fillna(False)
    basis_cross = _cross(close, basis).fillna(False)

    return {"entries": entries, "exits": basis_cross,
            "short_entries": short_entries, "short_exits": basis_cross}


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides)."""
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
            "ta.cross (direction-agnostic) implemented as crossover OR crossunder",
            "exit condition is identical for both long and short (basis cross), "
            "matching the source exactly",
        ],
    ),
    build_rule,
)
