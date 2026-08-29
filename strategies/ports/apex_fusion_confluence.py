"""
strategies/ports/apex_fusion_confluence.py -- port of "Apex Fusion AI| Smart Trend
Engine" (Tomukasss, tv_url https://www.tradingview.com/script/qQUC1ncY-Apex-Fusion-
AI-Smart-Trend-Engine/), source in storage/tv_scripts/apex_fusion_confluence.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides, a weighted-confluence SCORE system (nothing 'AI'): add points for
EMA-200 trend (20), SuperTrend direction (15), volume spike > 1.2x 20-period
SMA (15), MACD+rising-histogram slope (10), N-bar momentum (10), a Break of
Structure vs the most recent fractal pivot (15), being inside a 0.618-0.705
Fibonacci retracement zone of the 80-bar swing (10), and closeness to a naive
'POC' proxy = the close of the bar with the highest volume in 80 bars (5).
Long entry when the long score >= scoreNeed (80); short entry on the symmetric
score. The stop (close -/+ 1.5*ATR) and R:R target are computed at the signal
bar (inside the entry blocks) and submitted as a pending strategy.exit.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. supertrend is implemented as the reference pine_supertrend from the Pine v6
   docs (hl2 bands, nz-prev band conditioning, direction sign -1 = up / +1 =
   down). The source calls ta.supertrend(stFactor, stAtrLen) -- factor first,
   which matches the documented (factor, atrPeriod) signature, and stDir < 0
   therefore means an uptrend exactly as the author's green plot shows.
2. `lastHigh`/`lastLow` in the source are monotonic carry-forward of the latest
   fractal pivot, so BOS = close beyond the most recent CONFIRMED pivot high/
   low (ph.ffill()).
3. The POC proxy uses the close of the highest-volume bar rather than the true
   fixed-range volume profile (the source says so itself, line 57-60). Highest
   bar in the trailing 80-bar window, matching ta.highestbars(volume, 80).
4. Stop/target are signal-bar snapshots, so the port uses
   base.simulate_positions_both_indexed.
5. No position_size gate in the source (pyramiding=0); the engine's
   one-position replay replicates that, and a same-bar exit/opposite-entry
   collision is resolved by the engine's next_free gate.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _ema, _sma
from strategies.ports import base
from strategies.ports.base import atr_wilder, pivot_high, pivot_low, \
    simulate_positions_both_indexed, supertrend
from strategies.ports import _register, PortInfo

SLUG = "apex_fusion_confluence"

DEFAULT_PARAMS = dict(
    ema_len=200, st_atr_len=10, st_factor=3.0,
    macd_fast=12, macd_slow=26, macd_signal=9,
    mom_len=10, vol_len=20, pivot_len=3,
    fib_lookback=80, atr_len=14, rr=2.0, score_need=80,
    sl_atr_mult=1.5,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close, volume = df["high"], df["low"], df["close"], df["volume"]

    ema_trend = _ema(close, p["ema_len"])
    atr = atr_wilder(high, low, close, p["atr_len"])

    _line, direction_s = supertrend(high, low, close,
                                    factor=p["st_factor"],
                                    atr_period=p["st_atr_len"])
    direction = direction_s.to_numpy()

    macd_line = _ema(close, p["macd_fast"]) - _ema(close, p["macd_slow"])
    signal_line = _ema(macd_line, p["macd_signal"])
    hist = macd_line - signal_line

    momentum = close - close.shift(p["mom_len"])
    vol_ma = _sma(volume, p["vol_len"])

    ph = pivot_high(high, p["pivot_len"], p["pivot_len"])
    pl = pivot_low(low, p["pivot_len"], p["pivot_len"])
    last_high = ph.ffill()
    last_low = pl.ffill()

    swing_high = high.rolling(p["fib_lookback"]).max()
    swing_low = low.rolling(p["fib_lookback"]).min()
    fib_range = swing_high - swing_low
    long_fib_618 = swing_high - fib_range * 0.618
    long_fib_705 = swing_high - fib_range * 0.705
    short_fib_618 = swing_low + fib_range * 0.618
    short_fib_705 = swing_low + fib_range * 0.705

    vol_arr = volume.to_numpy(dtype=float)
    close_arr = close.to_numpy(dtype=float)
    n = len(df)
    poc_price = np.full(n, np.nan)
    win = p["fib_lookback"]
    for j in range(win - 1, n):
        m = int(np.argmax(vol_arr[j - win + 1:j + 1]))
        poc_price[j] = close_arr[j - win + 1 + m]
    poc_s = pd.Series(poc_price, index=df.index)

    ema_bull = close > ema_trend
    ema_bear = close < ema_trend
    st_bull = pd.Series(direction == -1, index=df.index)
    st_bear = pd.Series(direction == 1, index=df.index)
    macd_bull = (macd_line > signal_line) & (hist > hist.shift(1))
    macd_bear = (macd_line < signal_line) & (hist < hist.shift(1))
    mom_bull = momentum > 0
    mom_bear = momentum < 0
    vol_spike = volume > vol_ma * 1.2
    bull_bos = last_high.notna() & (close > last_high)
    bear_bos = last_low.notna() & (close < last_low)
    long_fib_zone = ((close <= long_fib_618) & (close >= long_fib_705)).fillna(False)
    short_fib_zone = ((close >= short_fib_618) & (close <= short_fib_705)).fillna(False)
    near_poc = ((close - poc_s).abs() <= atr).fillna(False)

    long_score = (20 * ema_bull + 15 * st_bull + 15 * vol_spike + 10 * macd_bull
                  + 10 * mom_bull + 15 * bull_bos + 10 * long_fib_zone + 5 * near_poc)
    short_score = (20 * ema_bear + 15 * st_bear + 15 * vol_spike + 10 * macd_bear
                   + 10 * mom_bear + 15 * bear_bos + 10 * short_fib_zone + 5 * near_poc)

    long_entries = (long_score >= p["score_need"]).fillna(False)
    short_entries = (short_score >= p["score_need"]).fillna(False)

    atr_arr = atr.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    rr, sm = p["rr"], p["sl_atr_mult"]

    def long_exit_trigger(j, sig_i, price, frame):
        sl = close_arr[sig_i] - sm * atr_arr[sig_i]
        tp = close_arr[sig_i] + (close_arr[sig_i] - sl) * rr
        return low_arr[j] <= sl or high_arr[j] >= tp

    def short_exit_trigger(j, sig_i, price, frame):
        sl = close_arr[sig_i] + sm * atr_arr[sig_i]
        tp = close_arr[sig_i] - (sl - close_arr[sig_i]) * rr
        return high_arr[j] >= sl or low_arr[j] <= tp

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger, df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, weighted confluence score)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/qQUC1ncY-Apex-Fusion-AI-Smart-Trend-Engine/",
        tv_author="Tomukasss",
        tv_script_name="Apex Fusion AI| Smart Trend Engine",
        mechanism_family="hybrid",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "supertrend via base.supertrend (repo's canonical community "
            "variant, direction -1 = up); the source's ta.supertrend(factor, "
            "atrPeriod) call maps factor first and stDir < 0 = bullish, as the "
            "author's green plot shows",
            "BOS uses monotonic carry-forward of the latest confirmed fractal "
            "pivot (ph.ffill())",
            "POC proxy is the highest-VOLUME bar's close in the 80-bar window "
            "(the source itself flags this as an approximation)",
            "stop/target are signal-bar snapshots -> indexed walk",
            "13 params vs meta's noted 9 (input count from source)",
        ],
    ),
    build_rule,
)