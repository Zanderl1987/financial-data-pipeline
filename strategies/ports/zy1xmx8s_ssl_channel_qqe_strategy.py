"""
strategies/ports/zy1xmx8s_ssl_channel_qqe_strategy.py -- port of "SSL Channel +
QQE Strategy" (Pinechord, tv_url https://www.tradingview.com/script/zy1XmX8s-SSL-
Channel-QQE-Strategy/), source in
storage/tv_scripts/zy1xmx8s_ssl_channel_qqe_strategy.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides. Two confirmed filters must agree. SSL channel: Hlv persists state
(close above its 10-SMA high = +1, below its 10-SMA low = -1, else prior); the
channel is bullish when sslUp > sslDown. QQE (RSI 14, smoothed 5, fast factor
4.238, Wilder*2-1=27 reference): a long QQE signal fires on the FIRST bar of a
new stretch where the trend-selected band sits below the smoothed RSI, short
symmetrically. Long entry = SSL bullish AND QQE long; exit = SSL turning
bearish (short exit symmetric, SSL turning bullish).

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. Both signals/exits are pure bar-state functions, so the port emits raw
   flags. When the SSL flip that EXITS a position coincides with the opposite
   QQE entry on the same bar, the engine drops the same-bar entry (its
   next_free = exit day + 2 gate) -- the flip takes effect one bar later than
   Pine. Standard engine convention for side='both' ports.
2. The QQE band-cross trend logic is this script's exact (shifted-reference)
   formulation; band levels are evaluated against the prior-bar band and RSI
   neighbors, matching the Pine recurrence. NaN history (undefined longband on
   early bars) behaves as Pine's comparisons to `na` (false -> use the new
   band), and trend seeds to 1 (nz(trend[1], 1)).
3. The `ThreshHold` input is declared but never referenced in the source, so
   it is not ported.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema, _rsi, _sma
from strategies.ports import base
from strategies.ports import _register, PortInfo

SLUG = "zy1xmx8s_ssl_channel_qqe_strategy"

DEFAULT_PARAMS = dict(
    ssl_len=10, rsi_period=14, rsi_smoothing=5,
    qqe_factor=4.238, wilders_period=27,
)


def _ssl_signals(high: pd.Series, low: pd.Series, close: pd.Series,
                 length: int):
    sma_hi = _sma(high, length).to_numpy(dtype=float)
    sma_lo = _sma(low, length).to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    n = len(close)
    hlv = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(sma_hi[i]) or np.isnan(sma_lo[i]):
            continue
        if c[i] > sma_hi[i]:
            hlv[i] = 1.0
        elif c[i] < sma_lo[i]:
            hlv[i] = -1.0
        else:
            hlv[i] = hlv[i - 1] if i > 0 else np.nan
    ssl_up = np.where(hlv < 0, sma_lo, sma_hi)
    ssl_dn = np.where(hlv < 0, sma_hi, sma_lo)
    valid = ~np.isnan(ssl_up)
    bullish = ((ssl_up > ssl_dn) & valid).astype(bool)
    bearish = ((ssl_up < ssl_dn) & valid).astype(bool)
    return pd.Series(bullish, index=close.index), pd.Series(bearish, index=close.index)


def _qqe_signals(rsi_ma: np.ndarray, factor: float, wilders_period: int):
    """QQE band/trend recurrences -> (qqe_long_signal, qqe_short_signal)."""
    n = len(rsi_ma)
    # AtrRsi = |RsiMa[1]-RsiMa|; dar = ema(ema(AtrRsi, wp), wp) * factor
    atr_rsi = np.abs(np.diff(rsi_ma, prepend=rsi_ma[0]))
    w1 = _ema(pd.Series(atr_rsi), wilders_period).to_numpy(dtype=float)
    dar = _ema(pd.Series(w1), wilders_period).to_numpy(dtype=float) * factor

    lband = np.full(n, np.nan)
    sband = np.full(n, np.nan)
    trend = np.ones(n, dtype=int)
    fast_tl = np.full(n, np.nan)
    for i in range(1, n):
        rs, rs_p = rsi_ma[i], rsi_ma[i - 1]
        lb_prev = lband[i - 1]
        sb_prev = sband[i - 1]
        lcond = (not np.isnan(lb_prev)) and rs_p > lb_prev and rs > lb_prev
        scond = (not np.isnan(sb_prev)) and rs_p < sb_prev and rs < sb_prev
        lband[i] = max(lb_prev, rs - dar[i]) if lcond else rs - dar[i]
        sband[i] = min(sb_prev, rs + dar[i]) if scond else rs + dar[i]

        if i >= 2:
            # ta.cross(RSIndex, shortband[1]) upward -> trend=1
            up = (rs > sband[i - 1]) and (rs_p <= sband[i - 2])
            # ta.cross(longband[1], RSIndex) upward -> trend=-1
            dn = (lband[i - 1] > rs) and (lband[i - 2] <= rs_p)
            trend[i] = 1 if up else (-1 if dn else trend[i - 1])
        else:
            trend[i] = trend[i - 1]
        fast_tl[i] = lband[i] if trend[i] == 1 else sband[i]

    qexl = np.zeros(n, dtype=int)
    qexs = np.zeros(n, dtype=int)
    for i in range(1, n):
        qexl[i] = qexl[i - 1] + 1 if fast_tl[i] < rsi_ma[i] else 0
        qexs[i] = qexs[i - 1] + 1 if fast_tl[i] > rsi_ma[i] else 0
    qqe_long = (qexl == 1).astype(bool)
    qqe_short = (qexs == 1).astype(bool)
    return qqe_long, qqe_short


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    ssl_bullish, ssl_bearish = _ssl_signals(high, low, close, p["ssl_len"])

    rsi = _rsi(close, p["rsi_period"])
    rsi_ma = _ema(rsi, p["rsi_smoothing"]).to_numpy(dtype=float)
    qqe_long, qqe_short = _qqe_signals(rsi_ma, p["qqe_factor"],
                                       p["wilders_period"])

    entries = (ssl_bullish & qqe_long).fillna(False)
    short_entries = (ssl_bearish & qqe_short).fillna(False)
    exits = ssl_bearish.fillna(False)
    short_exits = ssl_bullish.fillna(False)

    return {
        "entries": entries, "exits": exits,
        "short_entries": short_entries, "short_exits": short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, SSL + QQE filtered)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/zy1XmX8s-SSL-Channel-QQE-Strategy/",
        tv_author="Pinechord",
        tv_script_name="SSL Channel + QQE Strategy",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "raw flags; a same-bar exit/opposite-entry collision is resolved "
            "by the engine's next_free gate (flip lands one bar later than "
            "Pine)",
            "QQE band-cross trend uses this script's shifted-reference "
            "formulation; NaN history falls back to the new band and trend "
            "seeds +1",
            "ThreshHold input declared but unused in the source -> not ported",
        ],
    ),
    build_rule,
)