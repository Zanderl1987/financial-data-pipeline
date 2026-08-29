"""
strategies/ports/donchian_atr_trail_btc.py -- port of "Donchian Breakout with
ATR Trailing Stop (Trend Following)" (raven_suurineru, tv_url https://www.
tradingview.com/script/NeEiwmDq-Donchian-Breakout-with-ATR-Trailing-Stop-Trend-
Following/), source in storage/tv_scripts/donchian_atr_trail_btc.pine. The
collected source carries its own hardcoded 'BTC BTCUSD | capital.com | Trailing
v3 FINAL' title and Slovak tuning comments (4h/BTCUSD); the mechanism itself is
the classic Turtle-style construct described above.

Author design (from source, verbatim)
-------------------------------------
Long-only by default (allowShort OFF): breakout entry when close pierces the
20-bar Donchian channel EXCLUDING the current bar (ta.highest/lowest[1])
while the 200-EMA trend filter agrees (default ON). An ATR trailing stop is
frozen at the fill at trailMult * ATR (distance captured the bar the position
is opened, never updated) and ratchets with the position's extreme. A
weekend-flatten flag (default ON) closes any open position on Friday >= 20:00
UTC. The position-sizing block (risk % of equity, leverage cap) is engine
policy, not strategy logic.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. Entries gate on ATR being defined: the source's `not na(qty)` guard makes
   qty NaN until the ATR window fills (qtyRaw = riskAmount/slDist), so no
   entry can fire pre-ATR warmup.
2. The trail distance is captured once at the fill bar (the source assigns
   trailDist := slDist only inside the `justIn` branch) and the ratchet level
   is built from PRIOR bars' highs/lows: at bar j >= fill+1 the resting stop
   equals max(price - dist, max_{k in fill+1..j-1} high[k] - dist) for longs
   (mirror for shorts), so a breakout low[j] trades through it exactly as
   Pine's stop order would.
3. exit is via low/high touching the stop (intrabar fills) plus the weekend
   flatten; the engine then closes at next close.
4. Position sizing / risk inputs (useEquity, useRiskCap, riskCapUSD, leverage)
   are not ported -- the engine's own sizing policy applies.
5. The weekend hour compares against the frame datetime (UTC).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema
from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "donchian_atr_trail_btc"

DEFAULT_PARAMS = dict(
    entry_len=20, trend_len=200, use_trend_filter=True,
    atr_len=14, trail_mult=2.5,
    allow_long=True, allow_short=False,
    use_wknd_flat=True, fri_exit_hour=20,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    upper_entry = high.rolling(p["entry_len"]).max().shift(1)
    lower_entry = low.rolling(p["entry_len"]).min().shift(1)
    ema_trend = _ema(close, p["trend_len"])
    atr = atr_wilder(high, low, close, p["atr_len"])

    trend_up_ok = (~p["use_trend_filter"]) | (close > ema_trend)
    trend_dn_ok = (~p["use_trend_filter"]) | (close < ema_trend)

    idx = df.index
    dow = getattr(idx, "dayofweek", None)
    hour = getattr(idx, "hour", None)
    wknd_mask = pd.Series(False, index=df.index)
    if dow is not None and hour is not None:
        wknd_mask = pd.Series(
            np.array(dow) == 4, index=df.index) & (
            pd.Series(np.array(hour), index=df.index) >= p["fri_exit_hour"])

    long_entries = ((close > upper_entry) & trend_up_ok & ~wknd_mask
                    & atr.notna()).fillna(False) & p["allow_long"]
    short_entries = ((close < lower_entry) & trend_dn_ok & ~wknd_mask
                     & atr.notna()).fillna(False) & p["allow_short"]

    atr_arr = atr.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    wknd_arr = wknd_mask.to_numpy()
    tm = p["trail_mult"]

    def long_exit_trigger(j, sig_i, price, frame):
        if wknd_arr[j]:
            return True
        fill = sig_i + 1
        dist = atr_arr[fill] * tm
        peak = high_arr[fill + 1:j].max() if j > fill + 1 else price
        return low_arr[j] <= max(price, peak) - dist

    def short_exit_trigger(j, sig_i, price, frame):
        if wknd_arr[j]:
            return True
        fill = sig_i + 1
        dist = atr_arr[fill] * tm
        trough = low_arr[fill + 1:j].min() if j > fill + 1 else price
        return high_arr[j] >= min(price, trough) + dist

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (long-only Donchian breakout + ATR trail)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/NeEiwmDq-Donchian-Breakout-with-ATR-Trailing-Stop-Trend-Following/",
        tv_author="raven_suurineru",
        tv_script_name="Donchian Breakout with ATR Trailing Stop (Trend Following)",
        mechanism_family="trend",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "Donchian channel highest/lowest excludes the current bar",
            "trail DISTANCE frozen at the fill bar (source sets it only in the "
            "justIn branch); level ratchets against PRIOR bars' extremes",
            "entries gated on ATR defined (mirrors the not na(qty) sizing-"
            "guard)",
            "allow_short default False -> long-only at defaults",
            "weekend flatten uses the frame's weekday/hour (UTC); rating-size/"
            "risk inputs not ported (engine policy)",
        ],
    ),
    build_rule,
)