"""Strategy specs for the paper-trading forward test.

`entries(df)` gives floor-filtered signal flags. `held_exit(df)` gives an exit
function driven by the position the account actually holds (its own signal bar
and entry price), not the port's take-every-signal replay.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from analytics.technical import _ema
from paper.accounts import PRICE_FLOOR
from strategies.ports import (capitulation_stretch_reversion as cap,
                              joey_stochrsi_atr as joey,
                              rsi_bb_inside_strategy as rsibb)
from strategies.ports.base import atr_wilder

ExitFn = Callable[[int, int, float, str], bool]


@dataclass(frozen=True)
class StrategySpec:
    slug: str
    entries: Callable[[pd.DataFrame], dict[str, pd.Series]]
    held_exit: Callable[[pd.DataFrame], ExitFn]


def _floored(df: pd.DataFrame, long_s: pd.Series, short_s: pd.Series | None):
    ok = df["close"] >= PRICE_FLOOR
    long_out = long_s.fillna(False).astype(bool) & ok
    if short_s is None:
        short_out = pd.Series(False, index=df.index)
    else:
        short_out = short_s.fillna(False).astype(bool) & ok
    return {"long": long_out, "short": short_out}


def _joey_entries(df):
    return _floored(df, joey.compute(df)["entries"], None)


def _joey_held_exit(df) -> ExitFn:
    p = joey.DEFAULT_PARAMS
    atr = atr_wilder(df["high"], df["low"], df["close"], p["atr_length"]).to_numpy(float)
    hi = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    mult = p["atr_multiplier"]

    def fn(j, sig_i, entry_price, side):
        r = atr[sig_i] * mult
        return bool(hi[j] >= entry_price + r or lo[j] <= entry_price - r)
    return fn


def _cap_entries(df):
    return _floored(df, cap.compute(df)["entries"], None)


def _cap_held_exit(df) -> ExitFn:
    p = cap.DEFAULT_PARAMS
    close = df["close"].to_numpy(float)
    low = df["low"].to_numpy(float)
    mean = _ema(df["close"], p["mean_len"]).to_numpy(float)
    atr = atr_wilder(df["high"], df["low"], df["close"], p["atr_len"]).to_numpy(float)

    def fn(j, sig_i, entry_price, side):
        if side != "long":
            return False
        if low[j] <= close[sig_i] - p["stop_mult"] * atr[sig_i]:
            return True
        if close[j] >= mean[j]:
            return True
        return bool(j - sig_i >= p["max_bars"])
    return fn


def _rsibb_entries(df):
    out = rsibb.compute(df)
    return _floored(df, out["entries"], out["short_entries"])


def _rsibb_held_exit(df) -> ExitFn:
    p = rsibb.DEFAULT_PARAMS
    basis, _, _ = rsibb._bb(df["close"], p["bb_len"], p["bb_mult"])
    cross = rsibb._cross(df["close"], basis).fillna(False).to_numpy(bool)

    def fn(j, sig_i, entry_price, side):
        return bool(cross[j])
    return fn


STRATEGIES: dict[str, StrategySpec] = {
    joey.SLUG: StrategySpec(joey.SLUG, _joey_entries, _joey_held_exit),
    cap.SLUG: StrategySpec(cap.SLUG, _cap_entries, _cap_held_exit),
    rsibb.SLUG: StrategySpec(rsibb.SLUG, _rsibb_entries, _rsibb_held_exit),
}
