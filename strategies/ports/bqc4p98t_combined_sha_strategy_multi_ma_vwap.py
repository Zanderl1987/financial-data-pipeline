"""
strategies/ports/bqc4p98t_combined_sha_strategy_multi_ma_vwap.py -- port of
"Combined: SHA Strategy + Multi MA/VWAP + EMA Band + 9/20 MA Fill"
(mayankbhatia979, tv_url https://www.tradingview.com/script/bQC4p98T-Combined-
SHA-Strategy-Multi-MA-VWAP-EMA-Band-9-20-MA-Fill/), source in
storage/tv_scripts/bqc4p98t_combined_sha_strategy_multi_ma_vwap.pine.

Author design (from source, verbatim)
-------------------------------------
The combined script is a packaging of three sections; ONLY section 1 (the
Smoothed Heiken Ashi strategy) contains trading logic -- sections 2 and 3 are
pure indicator plots (multi SMA/EMA/VWAP lines and an EMA filled band; section
3 is never implemented, per the collected meta note). The strategy is the
well-known double-smoothed Heikin-Ashi drum: EMA(len1) over each raw OHLC,
Heikin-Ashi candles of that smoothed series, a second EMA(len2) over the HA
close/open; green = smoothed-HA close > open, red below. Enter LONG on the
confirmBars-th consecutive green candle, SHORT on the confirmBars-th
consecutive red candle; exit on the FIRST opposite-color candle (exitFirst).
Long/short symmetric, no other exit logic.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. Mechanism is identical to the sibling port smoothed_heiken_ashi_strategy
   (same construction, same default bar counts); this slug is registered
   separately because the catalog admits scripts, not mechanisms. Green/red
   color uses only the smoothed HA close vs open (the h2/l2 smoothes are
   plot-only here).
2. Raw entry/exit flags only (color-streak counters are pure bar state); the
   engine's next_free re-entry gate (exit day + 2) can drop an immediate flip
   entry on the bar after the exit -- same accepted convention as the sibling.
3. _ema warmup (min_periods=n) delays signals vs Pine's bar-0 seed.
4. The SHA start/end-date backtest window and every plot/alert block are
   scaffolding/cosmetic and are not ported.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema
from strategies.ports import base
from strategies.ports import _register, PortInfo

SLUG = "bqc4p98t_combined_sha_strategy_multi_ma_vwap"

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
    for i in range(n):
        ha_o[i] = (o_s[i] + c_s[i]) / 2.0 if i == 0 else (ha_o[i - 1] + ha_c[i - 1]) / 2.0

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
    exits = pd.Series(p["exit_first"] & is_red, index=df.index)
    short_exits = pd.Series(p["exit_first"] & is_green, index=df.index)

    return {
        "entries": entries, "exits": exits,
        "short_entries": short_entries, "short_exits": short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, smoothed-HA color-streak)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/bQC4p98T-Combined-SHA-Strategy-Multi-MA-VWAP-EMA-Band-9-20-MA-Fill/",
        tv_author="mayankbhatia979",
        tv_script_name="Combined: SHA Strategy + Multi MA/VWAP + EMA Band + 9/20 MA Fill",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "only section 1 (smoothed HA strategy) has trading logic; section "
            "2 is indicator plots, section 3 never implemented -- neither "
            "ported",
            "mechanism identical to smoothed_heiken_ashi_strategy; registered "
            "separately since catalog admits scripts",
            "raw signal flags (color streaks are pure bar state); engine "
            "re-entry gate can drop an immediate flip entry, same convention "
            "as sibling port",
            "_ema warmup delays signals vs Pine bar-0 seed; backtest-window "
            "and plot/alert blocks not ported",
        ],
    ),
    build_rule,
)