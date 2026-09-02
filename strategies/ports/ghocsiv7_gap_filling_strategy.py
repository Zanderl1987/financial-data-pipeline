"""
strategies/ports/ghocsiv7_gap_filling_strategy.py -- port of "Gap Filling
Strategy" (alexgrover, tv_url https://www.tradingview.com/script/De0v1Bsl-
Gap-Filling-Strategy/), source in
storage/tv_scripts/ghocsiv7_gap_filling_strategy.pine. MPL-2.0.

Author design (from source, verbatim)
-------------------------------------
Both sides, mean-reversion to the most recent gap: a daily gap (open that
jumps away from the prior bar's high/low) sets a "fill level" at the prior
bar's extreme; entries counter the gap direction (default non-inverted: BUY
the DOWN gap, SELL the UP gap) once per new session, with an exit limit at the
fill level (`lim`). Positions also close when a new session starts (default
`clw = "New Session"`), so trades are intra-session mean reversion of gaps.

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. `valuewhen(ses and (upgap or dngap), val, 0)` -- the fill level is the MOST
   RECENT gap extreme, forward-held until the next gap; ported as a
   forward-filled level, same shape.
2. Gap definition requires same-TF bars; `time("D")` session change is the
   `change()` of the daily component of the bar's timestamp, which on a daily
   frame is every bar. Without a multi-timeframe input the session boundary
   equals a new index date, so `ses` is the `date != prev date` rule.
3. Entry uses `strategy.entry(when=ses and gap)`, exit `limit=lim` (intrabar
   limit order touched via high/low, filled at engine close). The engine's
   one-position model means a position entered on the gap bar is closed at the
   next fill; `strategy.close_all` at new session is the `clw` default.
4. `invert` input is exposed as a parameter (flips buy/sell gap direction).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.ports import base
from strategies.ports import _register, PortInfo

SLUG = "ghocsiv7_gap_filling_strategy"

DEFAULT_PARAMS = dict(
    invert=False,
    close_when="New Session",   # "New Session" | "New Gap" | "Reverse Position"
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    o, c = df["open"], df["close"]
    h, l = df["high"], df["low"]

    # Session boundary: on a daily frame `time("D")` equals every bar, so a new
    # session is any date change. On intraday frames this would need the TF
    # string; the catalog stores daily bars, so the date-change rule is exact.
    dates = pd.Series(pd.to_datetime(df.index))
    ses = (dates != dates.shift(1)).to_numpy(dtype=bool)
    ses = pd.Series(ses, index=df.index)
    ses[ses.index[0]] = True

    hi1, lo1 = h.shift(1), l.shift(1)
    c1, o1 = c.shift(1), o.shift(1)

    upgap = (o > hi1) & (pd.concat([c, o], axis=1).min(axis=1)
                         > pd.concat([c1, o1], axis=1).max(axis=1))
    dngap = (o < lo1) & (pd.concat([c1, o1], axis=1).min(axis=1)
                         > pd.concat([c, o], axis=1).max(axis=1))

    # val on a gap bar; valuewhen holds the last non-na value.
    val = pd.Series(
        pd.concat([c1, o1], axis=1).max(axis=1).where(upgap,
            pd.concat([c1, o1], axis=1).min(axis=1)),
        index=df.index)
    lim = val.where(ses & (upgap | dngap)).ffill()

    gap_bar = ses & (upgap | dngap)
    if p["invert"]:
        entries = gap_bar & upgap
        short_entries = gap_bar & dngap
    else:
        entries = gap_bar & dngap
        short_entries = gap_bar & upgap

    # Reverse Position close mode: the author's literal condition is `false`
    # (positions only exit via the limit/level exit; the reversal is implied by
    # a new opposite entry, which the one-position engine documents as dropped
    # while a position is open -- see supertrend_entry_tp123 note 3).
    close_signal = pd.Series(False, index=df.index)
    if p["close_when"] == "New Session":
        close_signal = ses
    elif p["close_when"] == "New Gap":
        close_signal = gap_bar
    # else "Reverse Position": leave close_signal False.

    # An exit fires on the last bar before a close_signal, or when price
    # reaches the limit level (high >= lim for a long, low <= lim for a short).
    high_arr = h.to_numpy(dtype=float)
    low_arr = l.to_numpy(dtype=float)
    lim_arr = lim.to_numpy(dtype=float)
    close_arr = close_signal.to_numpy(dtype=bool)

    def long_trigger(j, price, frame):
        return bool(close_arr[j] or
                    (np.isfinite(lim_arr[j]) and high_arr[j] >= lim_arr[j]))

    def short_trigger(j, price, frame):
        return bool(close_arr[j] or
                    (np.isfinite(lim_arr[j]) and low_arr[j] <= lim_arr[j]))

    walk = base.simulate_positions_both(
        entries, short_entries, c,
        long_trigger, short_trigger, df)

    return {
        "entries": entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, gap-fill mean reversion)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/De0v1Bsl-Gap-Filling-Strategy/",
        tv_author="alexgrover",
        tv_script_name="Gap Filling Strategy",
        mechanism_family="mean_reversion",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "fill level = forward-held most-recent gap extreme (valuewhen)",
            "daily frame: session change == date change; intraday would need the TF string",
            "gaps and sessions must both hold on the same bar for an entry",
            "close modes: New Session (default), New Gap, Reverse Position",
            "limit fill touched on high/low, executed at engine next close",
        ],
    ),
    build_rule,
)