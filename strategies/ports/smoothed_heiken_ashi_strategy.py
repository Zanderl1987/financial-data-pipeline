"""
strategies/ports/smoothed_heiken_ashi_strategy.py -- port of "Smoothed Heiken
Ashi Strategy v1" (yovanygarcia87, tv_url https://www.tradingview.com/script/zmlXzncC/),
source in storage/tv_scripts/smoothed_heiken_ashi_strategy.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides. Double-smoothed Heikin-Ashi candles: step 1 smooths OHLC with an
EMA (len1), step 2 builds Heikin-Ashi candles on that smoothed series, step 3
smoothes the HA candles again with a second EMA (len2). A candle is "green"
when the smoothed-close > smoothed-open, red when below. Enter LONG on the Nth
consecutive green candle (default 2, `confirmBars`), SHORT on the Nth
consecutive red candle; exit on the FIRST opposite-color candle (default,
`exitFirst`). Long/short symmetric, no separate exit logic otherwise.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. Signals are pure functions of bar state (color-streak counters), so the port
   returns raw entry/exit flags for the engine to play out -- no entry-price
   anchoring needed. Because the engine re-entry gate is next_free =
   exit_signal_day + 2, a flip whose short entry lands the bar right after the
   long exit (red[1] closes long, red[2] would open short) is dropped: the
   engine won't take the opposite entry until the bar after the exit, whereas
   Pine positions immediately on the confirm bar. Same convention every other
   side='both' port already accepts.
2. The campaign's analytics.technical._ema uses min_periods=n warmup (NaN until
   n bars), where Pine's ta.ema seeds from bar 0 -- signals are delayed by the
   warmup, not altered once live.
3. startDate/endDate backtest-window inputs, plotcandle/plotshape/alertcondition
   are scaffolding/cosmetic and not ported.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema
from strategies.ports import base
from strategies.ports import _register, PortInfo

SLUG = "smoothed_heiken_ashi_strategy"

DEFAULT_PARAMS = dict(
    len1=10, len2=10, confirm_bars=2,
    exit_first=True, allow_long=True, allow_short=True,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    n = len(df)

    o_s = _ema(o, p["len1"]).to_numpy(dtype=float)
    h_s = _ema(h, p["len1"]).to_numpy(dtype=float)
    l_s = _ema(l, p["len1"]).to_numpy(dtype=float)
    c_s = _ema(c, p["len1"]).to_numpy(dtype=float)

    ha_c = (o_s + h_s + l_s + c_s) / 4.0
    ha_o = np.full(n, np.nan)
    for i in range(n):                              # Pine: na(haO[1]) ? (oS+cS)/2 : ...
        ha_o[i] = (o_s[i] + c_s[i]) / 2.0 if i == 0 else (ha_o[i - 1] + ha_c[i - 1]) / 2.0
    ha_h = np.maximum(h_s, np.maximum(ha_o, ha_c))
    ha_l = np.minimum(l_s, np.minimum(ha_o, ha_c))

    o2 = _ema(pd.Series(ha_o, index=df.index), p["len2"]).to_numpy(dtype=float)
    c2 = _ema(pd.Series(ha_c, index=df.index), p["len2"]).to_numpy(dtype=float)

    is_green = (c2 > o2).astype(bool)
    is_red = (c2 < o2).astype(bool)

    gc = np.zeros(n, dtype=int)
    rc = np.zeros(n, dtype=int)
    for i in range(n):
        gc[i] = gc[i - 1] + 1 if (i > 0 and is_green[i]) else (1 if is_green[i] else 0)
        rc[i] = rc[i - 1] + 1 if (i > 0 and is_red[i]) else (1 if is_red[i] else 0)

    entries = pd.Series((gc == p["confirm_bars"]) & p["allow_long"], index=df.index)
    short_entries = pd.Series((rc == p["confirm_bars"]) & p["allow_short"], index=df.index)
    # exits = first opposite-color candle (Pine gates on position size; the
    # engine only consumes exit flags while a position is open, so the raw
    # color state is equivalent)
    exits = pd.Series(p["exit_first"] & is_red, index=df.index)
    short_exits = pd.Series(p["exit_first"] & is_green, index=df.index)

    return {
        "entries": entries, "exits": exits,
        "short_entries": short_entries, "short_exits": short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, HA color-streak)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/zmlXzncC/",
        tv_author="yovanygarcia87",
        tv_script_name="Smoothed Heiken Ashi Strategy v1",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "raw signal flags (color-streak counters are pure bar state), no "
            "entry-price anchoring; engine re-entry gate (next_free = exit day "
            "+ 2) can drop an immediate flip entry on the bar right after the "
            "exit, same convention as other side='both' ports",
            "_ema warmup (min_periods=n) delays signals vs Pine's bar-0 seed",
            "backtest-window inputs / plotting / alerts not ported",
        ],
    ),
    build_rule,
)