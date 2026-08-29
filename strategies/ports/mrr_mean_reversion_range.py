"""
strategies/ports/mrr_mean_reversion_range.py -- port of "MRR (Mean Reversion
Range)" (abdulrehmantatvacare, tv_url https://www.tradingview.com/script/1FgHp6Cv-
MRR-Mean-Reversion-Range/), source in storage/tv_scripts/mrr_mean_reversion_range.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides, but the opposite regime of a trend-follower: only fires when the
market is RANGING (ADX(14,14) below 20). Long: close <= lower Bollinger band
AND RSI(14) <= 30, flat; short symmetric (close >= upper, RSI >= 70). Exit: a
fixed stop set at the signal bar (lower band - atr_stop_mult*ATR for long), a
target at the Bollinger BASIS (re-evaluated each bar the position is open, so
it tracks the moving basis), plus an early "regime shift" exit when ADX rises
> range_max + abort_buffer (default 28) mid-trade.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. The long/short stop level is captured at the SIGNAL bar (`longStop := lower
   - atrStopMult*atrVal` inside the entry block, then persisted), so the port
   anchors it via base.simulate_positions_both_indexed; the basis target and
   the ADX regime abort are current-bar level checks (the source re-submits
   `strategy.exit(..., limit=basis)` every bar).
2. ta.dmi is rebuilt from Wilder primitives (_rma of DM/TR, DX = 100*|+DI-
   -DI|/(+DI+-DI), ADX = Wilder-smoothed DX) with the campaign's documented
   primitive conventions; details can differ from TradingView's exact DMI
   implementation.
3. Signals use `strategy.position_size == 0` (flat) gates; the engine replicates
   that with its own one-position-at-a-time replay.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _rma, _rsi, _sma
from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "mrr_mean_reversion_range"

DEFAULT_PARAMS = dict(
    bb_length=20, bb_mult=2.0, rsi_length=14, rsi_oversold=30,
    rsi_overbought=70, adx_length=14, adx_range_max=20.0,
    adx_abort_buffer=8.0, atr_length=14, atr_stop_mult=1.0,
)


def _dmi_adx(high: pd.Series, low: pd.Series, close: pd.Series,
             n: int) -> pd.Series:
    """Wilder ADX (ta.dmi(n, n)). See port note 2."""
    up = high - high.shift()
    dn = low.shift() - low
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=high.index)
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_r = _rma(tr, n)
    plus_di = 100.0 * _rma(plus_dm, n) / atr_r
    minus_di = 100.0 * _rma(minus_dm, n) / atr_r
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return _rma(dx, n)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    basis = _sma(close, p["bb_length"])
    dev = p["bb_mult"] * close.rolling(p["bb_length"]).std(ddof=0)
    upper = basis + dev
    lower = basis - dev

    rsi = _rsi(close, p["rsi_length"])
    adx = _dmi_adx(high, low, close, p["adx_length"])
    ranging = (adx < p["adx_range_max"]).fillna(False)

    atr = atr_wilder(high, low, close, p["atr_length"])

    long_entries = (ranging & (close <= lower) & (rsi <= p["rsi_oversold"])).fillna(False)
    short_entries = (ranging & (close >= upper) & (rsi >= p["rsi_overbought"])).fillna(False)

    lower_arr = lower.to_numpy(dtype=float)
    upper_arr = upper.to_numpy(dtype=float)
    basis_arr = basis.to_numpy(dtype=float)
    atr_arr = atr.to_numpy(dtype=float)
    adx_arr = adx.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    stop_mult = p["atr_stop_mult"]
    regime_break = p["adx_range_max"] + p["adx_abort_buffer"]

    def long_exit_trigger(j, sig_i, price, frame):
        if low_arr[j] <= lower_arr[sig_i] - stop_mult * atr_arr[sig_i]:
            return True
        if high_arr[j] >= basis_arr[j]:
            return True
        if adx_arr[j] > regime_break:
            return True
        return False

    def short_exit_trigger(j, sig_i, price, frame):
        if high_arr[j] >= upper_arr[sig_i] + stop_mult * atr_arr[sig_i]:
            return True
        if low_arr[j] <= basis_arr[j]:
            return True
        if adx_arr[j] > regime_break:
            return True
        return False

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, range mean-reversion)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/1FgHp6Cv-MRR-Mean-Reversion-Range/",
        tv_author="abdulrehmantatvacare",
        tv_script_name="MRR (Mean Reversion Range)",
        mechanism_family="trend",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "entry-side stop captured at the SIGNAL bar (persisted in the "
            "source), anchored via simulate_positions_both_indexed",
            "basis target and ADX regime-abort are current-bar level checks "
            "(source re-submits strategy.exit(limit=basis) every bar)",
            "ta.dmi rebuilt from Wilder primitives; exact TradingView DMI "
            "implementation may differ",
        ],
    ),
    build_rule,
)