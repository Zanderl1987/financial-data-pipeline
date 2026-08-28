"""
strategies/ports/tradleware_hodl.py -- port of "TRADLEWARE-HODL" (cs_lev,
tv_url https://www.tradingview.com/script/wgsvzsT3-TRADLEWARE-HODL/), source
in storage/tv_scripts/tradleware_hodl.pine.

Author design (from source, verbatim)
-------------------------------------
Long-only, one trade for the whole backtest: a "buy once and hold forever"
baseline reference (the author's own docstring: "Hypothesis: baseline
reference -- buy once at the start date and hold forever"). Buys 99.95% of
equity on the first bar at or after `startDate` (default 2018-01-01),
provided no trade has closed yet and the position is flat. Closes the entire
position on the last bar of the chart (`bar_index >= last_bar_index - 1`) or
after `endDate` (default 2099-12-31, i.e. effectively never in this
campaign's data window).

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. This produces exactly ONE trade per symbol (entry on the first eligible
   bar, exit on the last bar of the frame) -- a deliberate benchmark, not an
   edge claim. `evaluation.stats.permutation_trades`'s pnl_p has essentially
   no power on a single-trade series; this port is provided for completeness
   and cross-strategy comparison, not because a meaningful significance test
   is expected from it.
2. `strategy.entry`'s own re-entry guard (`closedtrades == 0 and
   position_size == 0`) is not reproduced explicitly: since the exit never
   fires before the last bar, the one-position engine only ever executes the
   entry once regardless, so a plain level condition (`date >= start_date`)
   is sufficient and produces the identical single trade.
3. Exit is approximated as "true from the second-to-last bar onward" --
   matching the source's `bar_index >= last_bar_index - 1`, which becomes
   true one bar before the final bar and stays true through it.
"""

from __future__ import annotations

import pandas as pd

from strategies.ports import base
from strategies.ports import _register, PortInfo

SLUG = "tradleware_hodl"

DEFAULT_PARAMS = dict(
    start_date="2018-01-01",
    end_date="2099-12-31",
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    idx = pd.to_datetime(df.index)
    n = len(idx)

    start = pd.Timestamp(p["start_date"])
    end = pd.Timestamp(p["end_date"])

    entries = pd.Series(idx >= start, index=df.index)

    if n >= 2:
        last_trigger = idx[-2]
        exits = pd.Series((idx >= last_trigger) | (idx > end), index=df.index)
    else:
        exits = pd.Series(idx > end, index=df.index)

    return {"entries": entries.fillna(False), "exits": exits.fillna(False)}


def build_rule(params: dict = None):
    """Author-default TradeRule (long-only, one trade for the whole frame)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="long",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/wgsvzsT3-TRADLEWARE-HODL/",
        tv_author="cs_lev",
        tv_script_name="TRADLEWARE-HODL",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "buy-and-hold baseline: exactly one trade per symbol, provided for "
            "cross-strategy comparison rather than as an edge claim",
            "single-trade permutation p-value has essentially no statistical power",
            "exit approximated as true from the second-to-last bar onward, "
            "matching the source's bar_index >= last_bar_index - 1",
        ],
    ),
    build_rule,
)
