"""
strategies/ports/ineficient_market_123_pattern.py -- port of "1-2-3 Pattern +
ATR Filter - Strategy" (abib14bis, tv_url https://www.tradingview.com/script/
t578EfVb/), source in storage/tv_scripts/ineficient_market_123_pattern.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides, price-action 1-2-3 reversal. Alternating fractal pivots (pivot
lookback prd/20 left+right) are kept in a 3-slot ring; on the confirmation of
the 4th... (3rd slot) pivot the pattern is evaluated: bullish when the newest
LOW pivot is higher than the oldest LOW and below the middle HIGH ('1-2-Low'
lower-low -> higher-low), bearish symmetric. Signal only when the relative ATR
is compressed: 14-bar ATR% against its own 50-bar SMA <= 0.85. Entry requires
a flat book (pyramiding 0). Exits are price-anchored each bar: limit take
+tp_pct% / stop -sl_pct% of the position avg price (2.0/9.5 by default), plus a
bar-count TIME STOP 480 bars after the entry bar. Entries and exits
process_orders_on_close.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. The `pattern` lock flag is armed again by the same-bar pivot shift that
   armed the signal (the re-arm line runs after the signal block in the
   source, and any pivot insertion changes the middle slot), so in practice it
   can only suppress a same-bar double signal; the port keeps the identical
   order to stay faithful.
2. `high[prd]`/`low[prd]` at the confirmation bar equal pivot_high/pivot_low's
   value, and `bar_index - prd` is the pivot bar -- both match the base helpers.
3. Stop/target are percentages of `strategy.position_avg_price`, i.e. the
   engine's next-close fill price, so the port uses
   base.simulate_positions_both_indexed with sig_i = signal bar for the
   time-stop bookkeeping (Pine's entry_bar is set at the signal bar, so
   bars-in-position = j - sig_i exactly as in Pine).
4. The flat-only entry gate is replicated by the engine's one-position replay.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _sma
from strategies.ports import base
from strategies.ports.base import atr_wilder, pivot_high, pivot_low, \
    simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "ineficient_market_123_pattern"

DEFAULT_PARAMS = dict(
    prd=20, atr_filter_max=0.85, allow_long=True, allow_short=True,
    tp_pct=0.98, sl_pct=9.5, time_stop_bars=480, show_pattern=True,
    atr_len=14,
)


def _patterns(ph_arr: np.ndarray, pl_arr: np.ndarray, atr_ok: np.ndarray,
              prd: int):
    """Ring-state 1-2-3 evaluation -> (long_sig, short_sig) bool arrays."""
    n = len(atr_ok)
    long_sig = np.zeros(n, dtype=bool)
    short_sig = np.zeros(n, dtype=bool)
    pv = [0.0, 0.0, 0.0]
    ix = [0, 0, 0]
    pos = 0
    pattern = True
    prev_mid = 0.0
    for i in range(n):
        just_h = just_l = False
        if not np.isnan(ph_arr[i]) and pos <= 0:
            pv.pop(); ix.pop()
            pv.insert(0, float(ph_arr[i])); ix.insert(0, i - prd)
            pos = 1; just_h = True
        if not np.isnan(pl_arr[i]) and pos >= 0:
            pv.pop(); ix.pop()
            pv.insert(0, float(pl_arr[i])); ix.insert(0, i - prd)
            pos = -1; just_l = True
        if pattern and ix[2] > 0 and atr_ok[i]:
            p0, p1, p2 = pv[0], pv[1], pv[2]
            if just_l and p0 > p2 and p0 < p1:
                long_sig[i] = True
                pattern = False
            if just_h and p0 < p2 and p0 > p1:
                short_sig[i] = True
                pattern = False
        if pv[1] != prev_mid:
            pattern = True
        prev_mid = pv[1]
    return long_sig, short_sig


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    atr_val = atr_wilder(high, low, close, p["atr_len"])
    atr_pct = atr_val / close * 100
    atr_sma50 = _sma(atr_pct, 50)
    atr_ok = ((p["atr_filter_max"] <= 0)
              | (atr_pct / atr_sma50 <= p["atr_filter_max"])).fillna(False)

    prd = p["prd"]
    ph = pivot_high(high, prd, prd)
    pl = pivot_low(low, prd, prd)
    long_sig, short_sig = _patterns(ph.to_numpy(dtype=float),
                                    pl.to_numpy(dtype=float),
                                    atr_ok.to_numpy(dtype=bool), prd)

    long_entries = pd.Series(long_sig, index=df.index) & p["allow_long"]
    short_entries = pd.Series(short_sig, index=df.index) & p["allow_short"]

    close_arr = close.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    tp_pct, sl_pct = p["tp_pct"], p["sl_pct"]
    tsb = p["time_stop_bars"]

    def long_exit_trigger(j, sig_i, price, frame):
        if low_arr[j] <= price * (1 - sl_pct / 100) or \
                high_arr[j] >= price * (1 + tp_pct / 100):
            return True
        if j - sig_i >= tsb:
            return True
        return False

    def short_exit_trigger(j, sig_i, price, frame):
        if high_arr[j] >= price * (1 + sl_pct / 100) or \
                low_arr[j] <= price * (1 - tp_pct / 100):
            return True
        if j - sig_i >= tsb:
            return True
        return False

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, 1-2-3 + ATR gate)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/t578EfVb/",
        tv_author="abib14bis",
        tv_script_name="1-2-3 Pattern + ATR Filter - Strategy",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "alternating 3-slot pivot ring with same-order add/re-arm so the "
            "pattern lock only prevents a same-bar double signal (Pine order "
            "preserved)",
            "ATR% compression gate = 14-bar Wilder ATR% vs its 50-SMA",
            "TP/SL are % of position avg price (= engine next-close fill), "
            "re-anchored every bar by the walk",
            "time stop counts bars since the SIGNAL bar (Pine sets entry_bar "
            "at the signal bar) -> j - sig_i >= time_stop_bars",
            "engine's one-position replay replicates the flat-only entry gate",
        ],
    ),
    build_rule,
)