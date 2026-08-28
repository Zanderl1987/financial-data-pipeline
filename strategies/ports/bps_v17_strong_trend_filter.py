"""
strategies/ports/bps_v17_strong_trend_filter.py -- port of "BPS v17 - Strong
Trend Filter" (georgefushi, tv_url https://www.tradingview.com/script/
CxLXiMwm-BPS-v17-Strong-Trend-Filter/), source in
storage/tv_scripts/bps_v17_strong_trend_filter.pine.

Author design (from source, verbatim)
-------------------------------------
Both sides. A fixed-weight composite score: momentum (RSI>53 AND MACD line
above signal AND 10-bar ROC>0, else -1) x0.35 + breadth (close above both the
50- and 200-SMA, else -1) x0.3 + valuation (close more than 4% below the
200-SMA, else -1) x0.2 + a volume-spike term (1.5 if volume > 1.5x its
20-bar average, else 0), smoothed by a 3-bar EMA. Long when the smoothed
score crosses above 0.4 while volume is spiking and price is in an uptrend
(both SMAs); short on the mirror-image crossunder of -0.4. Every threshold in
this script is a hardcoded literal -- there are no `input.*()` declarations
at all (screen_source finds param_count=0).

Exit is a fixed-percent stop/limit, but NOT anchored to the entry price: the
source's `strategy.exit("Exit Long", "BPS Long", stop=close*0.975,
limit=close*1.05)` runs unconditionally every bar using that bar's OWN
`close`, so in Pine terms it replaces the pending stop/limit order with a
fresh price every bar -- the effective level is always 2.5%/5% off the MOST
RECENT bar's close, not the entry price. This is a documented quirk of the
source, carried through literally rather than "corrected" to an
entry-anchored level (see note 1).

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. Stop/limit are NOT anchored to entry price. Ported literally: the order
   placed after bar t's close (using close[t]) is live for bar t+1's
   high/low touch, so the port uses close.shift(1) as the reference level --
   a continuously-trailing band around the last bar's close, not a fixed
   stop/target off the trade's own entry.
2. The source has no explicit `strategy.close()` -- a position only closes
   via the stop/limit band or an opposite-direction `strategy.entry()`
   (which TradingView's default pyramiding=0 behavior treats as
   close-and-reverse). The opposite signal is folded into `exits`/
   `short_exits` to approximate that reversal, matching the precedent in
   strategies/ports/supertrend_entry_tp123.py note 3.
"""

from __future__ import annotations

import pandas as pd

from analytics.technical import _ema, _rsi, _sma
from strategies.ports import base
from strategies.ports import _register, PortInfo

SLUG = "bps_v17_strong_trend_filter"

# Every threshold below is hardcoded in the source (param_count=0 per
# screen_source) -- these are recorded for provenance, not tunable inputs.
DEFAULT_PARAMS = dict(
    rsi_len=14, macd_fast=12, macd_slow=26, macd_signal=9,
    ma50_len=50, ma200_len=200, vol_len=20, vol_mult=1.5, roc_len=10,
    score_smooth=3, long_thresh=0.4, short_thresh=-0.4,
    stop_pct=0.025, limit_pct=0.05,
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]

    rsi = _rsi(close, p["rsi_len"])
    macd_line = _ema(close, p["macd_fast"]) - _ema(close, p["macd_slow"])
    signal_line = _ema(macd_line, p["macd_signal"])
    ma50 = _sma(close, p["ma50_len"])
    ma200 = _sma(close, p["ma200_len"])
    vol_sma = _sma(volume, p["vol_len"])
    strong_vol = volume > vol_sma * p["vol_mult"]
    roc = (close / close.shift(p["roc_len"]) - 1.0) * 100.0

    trend_bull = (close > ma200) & (close > ma50)
    trend_bear = (close < ma200) & (close < ma50)

    momentum_score = ((rsi > 53) & (macd_line > signal_line) & (roc > 0)) \
        .map({True: 1.3, False: -1.0})
    breadth_score = trend_bull.map({True: 1.3, False: -1.0})
    val_score = ((close / ma200 - 1.0) < -0.04).map({True: 1.0, False: -1.0})
    vol_score = strong_vol.map({True: 1.5, False: 0.0})

    total_raw = (momentum_score * 0.35 + breadth_score * 0.3
                 + val_score * 0.2 + vol_score)
    total_smoothed = _ema(total_raw, p["score_smooth"])

    long_signal = (base_crossover(total_smoothed, p["long_thresh"])
                   & strong_vol & trend_bull)
    short_signal = (base_crossunder(total_smoothed, p["short_thresh"])
                    & strong_vol & trend_bear)

    prev_close = close.shift(1)
    long_stop_exit = (low <= prev_close * (1 - p["stop_pct"])) | \
        (high >= prev_close * (1 + p["limit_pct"]))
    short_stop_exit = (high >= prev_close * (1 + p["stop_pct"])) | \
        (low <= prev_close * (1 - p["limit_pct"]))

    entries = long_signal.fillna(False)
    short_entries = short_signal.fillna(False)
    exits = (long_stop_exit.fillna(False) | short_entries)
    short_exits = (short_stop_exit.fillna(False) | entries)

    return {"entries": entries, "exits": exits,
            "short_entries": short_entries, "short_exits": short_exits}


def base_crossover(s: pd.Series, level: float) -> pd.Series:
    return (s > level) & (s.shift(1) <= level)


def base_crossunder(s: pd.Series, level: float) -> pd.Series:
    return (s < level) & (s.shift(1) >= level)


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, flips)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/CxLXiMwm-BPS-v17-Strong-Trend-Filter/",
        tv_author="georgefushi",
        tv_script_name="BPS v17 - Strong Trend Filter",
        mechanism_family="trend",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "every threshold is hardcoded in the source (no input.*() calls at all)",
            "stop/limit are NOT entry-anchored -- ported literally as a band "
            "trailing the PRIOR bar's close, per the source's own quirk (see "
            "module docstring)",
            "opposite-direction signal folded into exits/short_exits to "
            "approximate Pine's default close-and-reverse behavior",
        ],
    ),
    build_rule,
)
