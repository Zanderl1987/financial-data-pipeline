"""
strategies/ports/ras16l2w_bvol_early_entry.py -- port of "BVOL early entry"
(bereg9020, tv_url https://www.tradingview.com/script/Ras16L2w-bvol-early-
entry/), source in storage/tv_scripts/ras16l2w_bvol_early_entry.pine.

Stage 1 note: this slug is in strategies/stage3.py's MANUAL_OVERRIDE_ADMIT --
its `request.security(bvolSym, timeframe.period, close, ...)` call uses
`timeframe.period` (the CHART's own resolution, not a genuinely higher
timeframe) with `ignore_invalid_symbol=true`, so it carries no repaint risk;
confirmed at collection time (storage/tv_scripts/_roster_strategies_popular_
2026-08-12_batch3.txt).

Author design (from source, verbatim; Russian comments/labels)
----------------------------------------------------------------
Long-only. Entry: a 180-bar z-score of `close` (SMA/stdev basis) crosses
above -0.5 -- an intentionally EARLY mean-reversion entry, before the z-score
would cross back through 0 -- gated by an optional (default ON) close > EMA
200 trend filter, AND a BVOL confirmation layer (default ON): a Bitcoin
volatility index (`BITMEX:BVOL24H`) must have recently been in its own
bottom decile (a "vol was compressed" precondition) and, if
`reqRising` (default ON), currently rising off that low. Exit: an ATR(14)
trailing stop (3x multiplier) off the running peak CLOSE since entry (not
the high) -- ratchets up, never down, and exits when close drops
`atrMult * atr` below that peak.

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. **BVOL confirmation layer is DROPPED, not approximated with a proxy.**
   This repo's data catalog has no BITMEX:BVOL24H series (or any crypto vol
   index) to port faithfully. This is not an invented workaround: the
   source's OWN fallback for a missing/invalid symbol is `bvolCond = (not
   hasBvol) or (...)` -- when `hasBvol` is false, `bvolCond` is
   unconditionally true. The port reproduces exactly that fallback path
   (`hasBvol` is always false here), so the ported entry is the z-score
   cross + trend filter only. This is the SAME degradation the author's own
   code performs when the symbol fails to resolve, not a new approximation
   invented for this port.
2. The ATR trailing-stop-from-peak-close is inherently path-dependent (its
   own state resets per trade), so it is implemented as a per-trade running
   peak inside the `base.simulate_positions` exit-trigger callback rather
   than a vectorized formula.
3. `atr` in the exit condition is read fresh each bar (not locked at entry),
   matching the source's own `ta.atr(atrLen)` recomputed every bar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema
from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions
from strategies.ports import _register, PortInfo

SLUG = "ras16l2w_bvol_early_entry"

DEFAULT_PARAMS = dict(
    z_window=180, z_entry=-0.5,
    use_trend=True, ema_trend_len=200,
    atr_mult=3.0, atr_len=14,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    basis = close.rolling(p["z_window"]).mean()
    dev = close.rolling(p["z_window"]).std(ddof=0)
    z = ((close - basis) / dev).where(dev > 0, 0.0)
    ema_t = _ema(close, p["ema_trend_len"])
    atr = atr_wilder(high, low, close, p["atr_len"])

    z_cross_entry = (z.shift(1) <= p["z_entry"]) & (z > p["z_entry"])
    trend_ok = (not p["use_trend"]) | (close > ema_t)
    # bvolCond dropped -- see module docstring note 1 (equivalent to the
    # source's own hasBvol=False fallback, always true).
    entries = (z_cross_entry & trend_ok).fillna(False)

    close_arr = close.to_numpy(dtype=float)
    atr_arr = atr.to_numpy(dtype=float)
    atr_mult = p["atr_mult"]
    state = {"price": None, "trail_hi": None}

    def exit_trigger(j, price, frame):
        if state["price"] != price:
            state["price"] = price
            state["trail_hi"] = price
        state["trail_hi"] = max(state["trail_hi"], close_arr[j])
        a = atr_arr[j]
        if not np.isfinite(a):
            return False
        return bool(close_arr[j] < state["trail_hi"] - atr_mult * a)

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
        tv_url="https://www.tradingview.com/script/Ras16L2w-bvol-early-entry/",
        tv_author="bereg9020",
        tv_script_name="BVOL early entry",
        mechanism_family="mean_reversion",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "BVOL confirmation layer dropped -- no BITMEX:BVOL24H data in this "
            "repo; reproduces the source's OWN hasBvol=False fallback rather "
            "than inventing a proxy (see module docstring note 1)",
            "ATR trailing stop tracks the running PEAK CLOSE since entry (not "
            "high), implemented as per-trade state inside the exit-trigger "
            "callback since it resets per trade",
            "Stage 1 admitted via MANUAL_OVERRIDE_ADMIT (request.security uses "
            "the chart's own timeframe.period, no repaint risk) -- moot for "
            "this port since the BVOL layer itself is dropped",
        ],
    ),
    build_rule,
)
