"""
strategies/ports/bist30_sp500_atr_momentum_rider.py -- port of "BIST30 to S&P 500
| ATR Momentum Rider" (newton61, tv_url https://www.tradingview.com/script/
jd1KSVn7/), source in
storage/tv_scripts/bist30_sp500_atr_momentum_rider.pine.

Author design (from source, verbatim)
-------------------------------------
Long-only momentum rider. A three-HMA stack (fast 8, mid 9, slow 20) defines a
regime = HMA-F > HMA-M > HMA-S; a setup fires on the FIRST bar of a new regime
(rawSetup) only if the slow HMA's 3-day ATR-normalized slope is >= -0.18
ATR/day (locked research constant). Entry orders are market, filled at the
next open. The K1 state machine then rides the position: peak price tracks
A(split)cumulative highs; when the favorable excursion (peak-entry)/entryATSATR
reaches k1A = 1.5 ATR the trail activates, then a frozen % trail k1T = 5.75% of
the peak close-breaches (close <= peak*(1-k1T/100)) exit after a k1W = 4-bar
wait. Fresh setups while in a position rotate the entry at the next open
(close old id + enter new, pyramiding=2 / close_entries_rule=ANY).

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. HMA is computed from scratch (2*WMA(half)-WMA(full), re-weighted over
   sqrt(len)) since no primitive exists; matches Pine ta.wma weighting (most
   recent bar has the largest weight).
2. The setup-rotation (close old entry + open the new id at the same open when
   another setup fires mid-position) is approximated as HOLD-THROUGH: the
   engine's single position stays open until the K1 close. Pine's re-roll only
   refreshes the entryATR baseline; directional exposure is the same continuous
   long. Documented rather than replicated (the engine cannot close+reopen on
   one tick).
3. entryATR is the ATR on the SETUP bar (the source persists pendingEntryATR =
   atrValue at the order bar), and the peak uses highs from the fill bar on.
   Both map onto simulate_positions_both_indexed's sig_i/fill timing.
4. The K1 exit is close-based (allowed only bar_index >= entryBar + k1W),
   matching the source's close-breach market exit at the next open.
5. The start/end-Year backtest window is not ported (test scaffolding; the
   campaign's engine runs its own dev/holdout split), and the display toggles
   are plot-only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema, _rma, _sma
from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "bist30_sp500_atr_momentum_rider"

DEFAULT_PARAMS = dict(
    fast_length=8, mid_length=9, slow_length=20, atr_length=14,
    slope_lookback=3, slope_min=-0.18,
    k1a=1.5, k1t=5.75, k1w=4,
)


def _wma(source: pd.Series, length: int) -> pd.Series:
    """Pine ta.wma: linear weights length..1, heaviest on the newest bar."""
    if length < 1:
        return source
    w = np.arange(1, length + 1, dtype=float)
    return source.rolling(length).apply(lambda x: np.dot(x, w) / w.sum(),
                                        raw=True)


def _hma(source: pd.Series, length: int) -> pd.Series:
    """Pine-style Hull moving average."""
    half = max(1, length // 2)
    root = max(1, int(round(np.sqrt(length))))
    return _wma(2.0 * _wma(source, half) - _wma(source, length), root)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    hma_f = _hma(close, p["fast_length"])
    hma_m = _hma(close, p["mid_length"])
    hma_s = _hma(close, p["slow_length"])

    regime = (hma_f > hma_m) & (hma_m > hma_s)
    raw_setup = regime & ~regime.shift(1, fill_value=False)
    atr = atr_wilder(high, low, close, p["atr_length"])
    slope3 = (hma_s - hma_s.shift(p["slope_lookback"])) / p["slope_lookback"] / atr
    slope_ok = slope3.notna() & (slope3 >= p["slope_min"])
    setup = (raw_setup & slope_ok & atr.notna()).fillna(False)

    long_entries = setup
    short_entries = pd.Series(False, index=df.index)

    atr_arr = atr.to_numpy(dtype=float)
    close_arr = close.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    k1a, k1t, k1w = p["k1a"], p["k1t"], p["k1w"]

    def long_exit_trigger(j, sig_i, price, frame):
        entry_atr = atr_arr[sig_i]
        fill = sig_i + 1
        peak = high_arr[fill:j + 1].max() if j >= fill else price
        mfe = (peak - price) / entry_atr if entry_atr > 0 else np.nan
        if np.isnan(mfe) or mfe < k1a:
            return False
        trail = peak * (1.0 - k1t / 100.0)
        return j - fill >= k1w and close_arr[j] <= trail

    def short_exit_trigger(j, sig_i, price, frame):
        return False

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger,
        df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (long-only HMA-stack momentum rider)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="long",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/jd1KSVn7/",
        tv_author="newton61",
        tv_script_name="BIST30 to S&P 500 - ATR Momentum Rider",
        mechanism_family="trend",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "HMA rebuilt from ta.wma semantics; slope filter is 3-day "
            "ATR-normalized on the slow HMA",
            "mid-position setup rotation approximated as hold-through (engine "
            "cannot close+reopen on the same bar); entryATR baseline of a "
            "roided entry not refreshed",
            "entryATR = ATR at the setup bar; peak = highs from the fill bar",
            "K1 exit is close-based after a k1W-bar wait; trail is a frozen "
            "% of the peak",
            "date window & display toggles not ported",
        ],
    ),
    build_rule,
)