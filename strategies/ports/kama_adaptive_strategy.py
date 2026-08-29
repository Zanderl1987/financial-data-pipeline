"""
strategies/ports/kama_adaptive_strategy.py -- port of "Kaufman Moving Average
Adaptive Strategy by MKB" (muratkbesiroglu, tv_url https://www.tradingview.com/
script/qgTc4zie-Kaufman-Moving-Average-Adaptive-Strategy-by-MKB/), source in
storage/tv_scripts/kama_adaptive_strategy.pine.

Author design (from source, verbatim)
-------------------------------------
Long-only. Hand-computed Kaufman Adaptive Moving Average: noise = 21-bar sum of
|src - src[1]|, signal = |src - src[21]|, efficiency ratio = signal/noise,
smoothing constant = (er*(fastEnd - slowEnd) + slowEnd)^2 with fastEnd=0.666 /
slowEnd=0.0645. KAMA itself is the EMA-like recurrence kama = kama[1] +
smooth*(src - kama[1]). A standard-deviation band sits 0.5*stdev(src,20) above
KAMA; entry long on src crossing UP through KAMA+band, exit on src crossing
DOWN through KAMA. Author: "the strategy works best on the daily timeframe";
no domain lock (pure price-derived MA).

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. Pine's seed for the KAMA recurrence (nz(kama[1]) -> 0.0 when undefined) is
   replicated: before the noise/signal windows are full the value is left NaN
   (signals cannot fire there), and once smooth is finite the recurrence
   resumes from 0.0 exactly as Pine's nz() fallback does.
2. ta.stdev is population stdev (ddof=0) -- the port uses rolling().std(ddof=0).
3. Raw entry/exit flags (both are pure bar-state crosses); the stove's own
   one-position-at-a-time gate replicates position_size logic.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.ports import base
from strategies.ports import _register, PortInfo

SLUG = "kama_adaptive_strategy"

DEFAULT_PARAMS = dict(
    length=21, fast_end=0.666, slow_end=0.0645,
    stdev_length=20, stdev_multiplier=0.5,
)


def _kaufman_adaptive_ma(src: np.ndarray, length: int,
                         fast_end: float, slow_end: float) -> np.ndarray:
    """KAMA via the Pine recurrence (see module docstring)."""
    n = len(src)
    abs_diff = np.abs(np.diff(src))
    out = np.full(n, np.nan)
    for i in range(n):
        if i < length:
            continue
        # signal needs src[i - length]; noise needs abs_diff[i-length:i]
        signal = abs(src[i] - src[i - length])
        noise = abs_diff[i - length:i].sum()
        ef = signal / noise if noise != 0.0 else 0.0
        smooth = (ef * (fast_end - slow_end) + slow_end) ** 2
        prev = out[i - 1] if i > 0 and not np.isnan(out[i - 1]) else 0.0
        out[i] = prev + smooth * (src[i] - prev)
    return out


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    close = df["close"]
    src = close.to_numpy(dtype=float)

    kama = _kaufman_adaptive_ma(src, p["length"], p["fast_end"], p["slow_end"])
    kama_s = pd.Series(kama, index=df.index)
    stdev = close.rolling(p["stdev_length"]).std(ddof=0)
    upper_band = kama_s + p["stdev_multiplier"] * stdev

    entries = (close > upper_band) & (close.shift(1) <= upper_band.shift(1))
    exits = (close < kama_s) & (close.shift(1) >= kama_s.shift(1))

    return {"entries": entries.fillna(False), "exits": exits.fillna(False)}


def build_rule(params: dict = None):
    """Author-default TradeRule (long-only KAMA band entry)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="long",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/qgTc4zie-Kaufman-Moving-Average-Adaptive-Strategy-by-MKB/",
        tv_author="muratkbesiroglu",
        tv_script_name="Kaufman Moving Average Adaptive Strategy by MKB",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "hand-computed KAMA efficiency-ratio recurrence, seeding undefined "
            "previous values at 0.0 like Pine's nz(kama[1])",
            "stdev is population (ddof=0), matching Pine ta.stdev",
            "raw cross entry/exit flags; engine's one-position gate mirrors "
            "position_size==0/position_size>0",
        ],
    ),
    build_rule,
)