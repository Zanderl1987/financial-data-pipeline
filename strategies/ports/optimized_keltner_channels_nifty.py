"""
strategies/ports/optimized_keltner_channels_nifty.py -- port of "Optimized
Keltner Channels Strategy [NIFTY]" (sudhank_naincy, tv_url https://www.
tradingview.com/script/ek2aOo0g-Optimized-Keltner-Channels-Strategy-NIFTY/),
source in storage/tv_scripts/optimized_keltner_channels_nifty.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides, Keltner channel breakout with STOP-ORDER entries: while close is
above the channel (crossUpper), a resting buy-stop at high+tick of the cross
bar trails the pending order; the order is CANCELED if close falls back below
the mid band (`src < ma`) before price reaches the stop, and fills the first
bar whose high prints >= the stop. The channel mid-band is esma(src, 20, exp=
true) (EMA by default) with bands at mid +/- mult * range measure
("Average True Range" -> ATR(10) by default; else True Range or range-length
RMA). Optional OFF-by-default add-ons: a 200-EMA trend filter and a session
window. Exits (also OFF at defaults): a wide ATR stop anchored to the entry
price (re-set each bar with live ATR) and/or a mid-band close exit. With every
optional exit off, the ONLY exit is the opposite-side entry implicitly
reversing the position (Pine pyramiding=0) -- flagged in the meta.json.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. stop-order entries are approximated as bar-close signals on the first bar
   whose high/low trades through the resting stop level (never on the cross
   bar itself, since the stop sits outside that bar's range), honoring the
   same-bar cancel-vs-fill ordering (a fill wins over a cancel on that bar,
   matching a resting-stop that has already executed).
2. The implicit-reversal close is reproduced as an EXIT trigger on the
   opposite side's stop-FILL bar (the shared single-position engine does not
   flip): the position closes when the other side's entry order fills, and the
   engine's next_free gate absorbs the same-bar flip (deviation: the reversed
   position opens on a later signal rather than intrabar).
3. The ATR stop level is entry-price-anchored with the CURRENT bar's ATR (the
   source recomputes entryPrice - atr*mult each bar while in the position);
   the mid-band exit is a close crossunder/crossover of the mid line.
4. The session-hours filter is not ported: `time(timeframe.period, ...)`
   returns na on daily bars and the toggle is OFF by default -- noted instead
   of silently no-op'ing a True setting.
5. No de-dup for `bprice` ticks: the resting stop uses an epsilon tick
   (high*(1+1e-9)); equal-touch fills are forgone, which has no material
   impact on bar-level flag accounting.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema, _rma, _sma
from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "optimized_keltner_channels_nifty"

DEFAULT_PARAMS = dict(
    kc_length=20, mult=2.0, use_exp=True, bands_style="Average True Range",
    atr_length=10,
    use_trend_filter=False, ema_period=200,
    exit_on_mid_band=False, use_atr_stop=False, atr_stop_mult=3.0,
)


def _keltner(high: pd.Series, low: pd.Series, close: pd.Series,
             length: int, mult: float, use_exp: bool,
             bands_style: str, atr_length: int):
    src = close
    ma = _ema(src, length) if use_exp else _sma(src, length)
    if bands_style == "True Range":
        tr = pd.concat([high - low,
                        (high - close.shift()).abs(),
                        (low - close.shift()).abs()], axis=1).max(axis=1)
        rangema = tr
    elif bands_style == "Range":
        rangema = _rma(high - low, length)
    else:                                        # "Average True Range"
        rangema = atr_wilder(high, low, close, atr_length)
    return ma, ma + rangema * mult, ma - rangema * mult


def _stop_order_entries(cross_up: pd.Series, cross_dn: pd.Series,
                        long_allowed: pd.Series, short_allowed: pd.Series,
                        high: np.ndarray, low: np.ndarray,
                        close: np.ndarray, mid: np.ndarray) -> tuple:
    """First-touch fill bars for resting congestion/stop orders per side."""
    n = len(cross_up)
    long_entries = np.zeros(n, dtype=bool)
    short_entries = np.zeros(n, dtype=bool)
    pending_long = False
    pending_short = False
    bprice = sprice = 0.0
    for j in range(n):
        if cross_up.iloc[j] and long_allowed.iloc[j]:
            pending_long = True
            bprice = high[j] * (1 + 1e-9)
        if pending_long:
            if high[j] >= bprice:
                long_entries[j] = True
                pending_long = False
            elif close[j] < mid[j]:
                pending_long = False
        if cross_dn.iloc[j] and short_allowed.iloc[j]:
            pending_short = True
            sprice = low[j] * (1 - 1e-9)
        if pending_short:
            if low[j] <= sprice:
                short_entries[j] = True
                pending_short = False
            elif close[j] > mid[j]:
                pending_short = False
    return long_entries, short_entries


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    ma, upper, lower = _keltner(high, low, close, p["kc_length"], p["mult"],
                                p["use_exp"], p["bands_style"], p["atr_length"])

    ema_filter = _ema(close, p["ema_period"])
    trend_long_ok = (~p["use_trend_filter"]) | (close > ema_filter)
    trend_short_ok = (~p["use_trend_filter"]) | (close < ema_filter)
    long_allowed = trend_long_ok
    short_allowed = trend_short_ok

    cross_up = (close > upper) & (close.shift(1) <= upper.shift(1))
    cross_dn = (close < lower) & (close.shift(1) >= lower.shift(1))

    long_entries, short_entries = _stop_order_entries(
        cross_up.fillna(False), cross_dn.fillna(False),
        long_allowed.fillna(False), short_allowed.fillna(False),
        high.to_numpy(dtype=float), low.to_numpy(dtype=float),
        close.to_numpy(dtype=float), ma.to_numpy(dtype=float))
    long_entries = pd.Series(long_entries, index=df.index)
    short_entries = pd.Series(short_entries, index=df.index)

    atr = atr_wilder(high, low, close, p["atr_length"])
    atr_arr = atr.to_numpy(dtype=float)
    close_arr = close.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    mid_arr = ma.to_numpy(dtype=float)
    use_mid = p["exit_on_mid_band"]
    use_atr = p["use_atr_stop"]
    stop_mult = p["atr_stop_mult"]

    def long_exit_trigger(j, sig_i, price, frame):
        if short_entries.iloc[j]:
            return True                    # other side fills -> implicit flip
        if use_atr and low_arr[j] <= price - atr_arr[j] * stop_mult:
            return True
        if use_mid and close_arr[j] < mid_arr[j] and close_arr[j - 1] >= mid_arr[j - 1]:
            return True
        return False

    def short_exit_trigger(j, sig_i, price, frame):
        if long_entries.iloc[j]:
            return True
        if use_atr and high_arr[j] >= price + atr_arr[j] * stop_mult:
            return True
        if use_mid and close_arr[j] > mid_arr[j] and close_arr[j - 1] <= mid_arr[j - 1]:
            return True
        return False

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, Keltner breakout w/ stop orders)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/ek2aOo0g-Optimized-Keltner-Channels-Strategy-NIFTY/",
        tv_author="sudhank_naincy",
        tv_script_name="Optimized Keltner Channels Strategy [NIFTY]",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "stop-order entries approximated at the first bar whose range "
            "trades the resting stop; fill wins over cancel on the same bar",
            "implicit-reversal close reproduced as an exit trigger on the "
            "opposite stop-fill bar (engine does not flip); reversal position "
            "opens on a later signal (deviation)",
            "ATR stop re-anchored each bar off the fill price (fill price = "
            "engine next-close); mid-band exit = close cross of the mid line",
            "session-hours filter NOT ported (na on daily bars, toggle off by "
            "default)",
            "DEFAULT config has no explicit exit -- reversal-only close; "
            "enable use_atr_stop / exit_on_mid_band to add stops",
        ],
    ),
    build_rule,
)