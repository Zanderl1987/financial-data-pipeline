"""
strategies/ports/dual_bollinger_band_cross.py -- port of "Dual Bollinger Band
Cross Strategy v6" (deependrasrivastavalko, tv_url https://www.tradingview.com/
script/vX4g2EH0-Dual-Bollinger-Band-Cross-Strategy-v6/), source in
storage/tv_scripts/dual_bollinger_band_cross.pine.

Author design (from source, verbatim)
-------------------------------------
Long-only. Two Bollinger midlines (SMA basis only -- the StdDev inputs affect
only the upper/lower bands, which the strategy logic never reads): fast
(5, 1) and slow (10, 2). Entry when the fast midline is above the slow
midline AND close is above the fast midline. Exit when close drops below the
slow midline.

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. `fastStdDev`/`slowStdDev` are recorded as DEFAULT_PARAMS for provenance but
   have no effect on the port's signal, exactly matching the source (the
   upper/lower bands are computed and plotted but never referenced by
   `longCondition`/`exitCondition`).
2. A Bollinger basis is just an SMA of `close` over the band length --
   `ta.bb`'s midline and `ta.sma` are identical, so this port uses
   `analytics.technical._sma` directly rather than reimplementing `ta.bb`.
"""

from __future__ import annotations

import pandas as pd

from analytics.technical import _sma
from strategies.ports import base
from strategies.ports import _register, PortInfo

SLUG = "dual_bollinger_band_cross"

DEFAULT_PARAMS = dict(
    slow_length=10, slow_stddev=2.0,
    fast_length=5, fast_stddev=1.0,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    close = df["close"]

    slow_mid = _sma(close, p["slow_length"])
    fast_mid = _sma(close, p["fast_length"])

    entries = (fast_mid > slow_mid) & (close > fast_mid)
    exits = close < slow_mid

    return {"entries": entries.fillna(False), "exits": exits.fillna(False)}


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
        tv_url="https://www.tradingview.com/script/vX4g2EH0-Dual-Bollinger-Band-Cross-Strategy-v6/",
        tv_author="deependrasrivastavalko",
        tv_script_name="Dual Bollinger Band Cross Strategy v6",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "StdDev inputs recorded but unused -- the source's own longCondition/"
            "exitCondition never reference the upper/lower bands, only the midlines",
            "Bollinger basis == SMA of close, so ta.bb's midline is computed via _sma",
        ],
    ),
    build_rule,
)
