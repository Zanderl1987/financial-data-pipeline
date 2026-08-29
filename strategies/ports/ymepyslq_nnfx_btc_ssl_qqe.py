"""
strategies/ports/ymepyslq_nnfx_btc_ssl_qqe.py -- port of "NNFX BTC SSL+QQE -
SignalForge" (SignalForge-Ai, tv_url https://www.tradingview.com/script/ymepYSLq/),
source in storage/tv_scripts/ymepyslq_nnfx_btc_ssl_qqe.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides, NNFX-style "2 cops" filter + Keltner overextension check. Baseline:
src vs its 20-EMA (sslBull). QQE Mod simplified: RSI(6) double-smoothed with
two 5-EMAs, histogram = fast - slow (qqeBull = histogram > 0). Keltner channel
(20-EMA mid, 1.5*ATR(20) width): entries only while src is INSIDE the channel
(not overextended). Entry long on src crossing up through the SSL line while
qqeBull and inside the channel; short symmetric (crossunder + not qqeBull).
Exits: either the baseline/QQE filter flips (not qqeBull or not sslBull for
long; opposite for short), OR a fixed R-multiple stop/target anchored to the
entry price (stop = entry +/- 1.5*ATR, target = entry +/- 3.0*ATR, ATR live).

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. The stop/target are sized off `strategy.position_avg_price` with the CURRENT
   bar's ATR every bar (`strategy.exit` re-submitted in the live position
   block), so the port anchors them to the engine's own next-close fill price
   and the current bar's ATR via base.simulate_positions_both -- no signal-bar
   indexing needed.
2. `invertSig` is a UI toggle, default OFF; ported as a param defaulting to
   False.
3. backtest startDate/endDate is a test-window control, not strategy logic; the
   campaign's engine already runs its own dev/holdout split, so it is not
   ported.
"""

from __future__ import annotations

import pandas as pd

from analytics.technical import _ema, _rsi
from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions_both
from strategies.ports import _register, PortInfo

SLUG = "ymepyslq_nnfx_btc_ssl_qqe"

DEFAULT_PARAMS = dict(
    ssl_len=20, kc_len=20, kc_mult=1.5,
    qqe_rsi_len=6, qqe_smooth=5, atr_len=14,
    stop_mult=1.5, tp_mult=3.0, invert_sig=False,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    ssl_line = _ema(close, p["ssl_len"])
    ssl_bull = close > ssl_line

    kc_atr = atr_wilder(high, low, close, p["kc_len"])
    kc_mid = _ema(close, p["kc_len"])
    kc_upper = kc_mid + kc_atr * p["kc_mult"]
    kc_lower = kc_mid - kc_atr * p["kc_mult"]
    inside_kc = ((close >= kc_lower) & (close <= kc_upper)).fillna(False)

    rsi = _rsi(close, p["qqe_rsi_len"])
    qqe_fast = _ema(rsi, p["qqe_smooth"])
    qqe_slow = _ema(qqe_fast, p["qqe_smooth"])
    qqe_bull = (qqe_fast - qqe_slow) > 0

    cross_up = (close > ssl_line) & (close.shift(1) <= ssl_line.shift(1))
    cross_dn = (close < ssl_line) & (close.shift(1) >= ssl_line.shift(1))

    long_entries = (cross_up & qqe_bull & inside_kc).fillna(False)
    short_entries = (cross_dn & ~qqe_bull & inside_kc).fillna(False)

    if p["invert_sig"]:
        long_entries, short_entries = ~long_entries, ~short_entries

    atr = atr_wilder(high, low, close, p["atr_len"])
    atr_arr = atr.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    qqe_bull_arr = qqe_bull.fillna(False).to_numpy()
    ssl_bull_arr = ssl_bull.fillna(False).to_numpy()
    stop_mult, tp_mult = p["stop_mult"], p["tp_mult"]

    def long_exit_trigger(j, price, frame):
        if low_arr[j] <= price - atr_arr[j] * stop_mult or \
                high_arr[j] >= price + atr_arr[j] * tp_mult:
            return True
        if not (qqe_bull_arr[j] and ssl_bull_arr[j]):
            return True
        return False

    def short_exit_trigger(j, price, frame):
        if high_arr[j] >= price + atr_arr[j] * stop_mult or \
                low_arr[j] <= price - atr_arr[j] * tp_mult:
            return True
        if qqe_bull_arr[j] or ssl_bull_arr[j]:
            return True
        return False

    walk = simulate_positions_both(long_entries, short_entries, close,
                                   long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, SSL+QQE+Keltner)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/ymepYSLq/",
        tv_author="SignalForge-Ai",
        tv_script_name="NNFX BTC SSL+QQE - SignalForge",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "stop/target anchored to the engine's next-close fill with the "
            "current bar's ATR (re-submitted each bar in the source), via "
            "base.simulate_positions_both",
            "filter-flip exits are level checks on current-bar QQE/SSL state, "
            "matching the source's strategy.position_size-gated closes",
            "invert_sig param (default False) ported; backtest date window not "
            "ported (test scaffolding)",
        ],
    ),
    build_rule,
)