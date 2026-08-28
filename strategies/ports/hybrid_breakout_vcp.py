"""
strategies/ports/hybrid_breakout_vcp.py -- port of "Hybrid Breakout | VCP-Inspired
Trend" (blitz_locked, tv_url https://www.tradingview.com/script/hezSShJr-
Hybrid-Breakout-VCP-Inspired-Trend/), source in
storage/tv_scripts/hybrid_breakout_vcp.pine.

Author design (from source, verbatim)
-------------------------------------
Long-only. Entry requires (1) a Minervini-style MA alignment --
close > ma50 > ma150 > ma200, ma200 rising over `ma_up_lookback` bars and
ma50 not falling; (2) a volatility squeeze -- 3-bar range percentile (150-bar
lookback) <= 40 OR the 3-bar close range <= 4% of its high; and (3) volume
expansion above 20-bar average x1.2 (toggleable). The order is a stop-buy at
the prior 3-bar high. Exits are tiered: 25% at +10%, 50% at +20%, remainder
at +30%, all with an 8% stop below the average entry price. Author default
date window is 2018-01-01 onward.

Port notes (approximations, recorded per pre-registration section 6)
--------------------------------------------------------------------
1. Stop-buy -> daily cross. Pine fills intrabar when price pierces the prior
   3-bar high. The port signals when close crosses that prior 3-bar high; the
   engine then buys at the close of t+1. Intraday fill vs close-fill timing
   differs.
2. Scale-outs collapsed. TP1/TP2/TP3 at 25/50/100% cannot be modeled by a
   full-position engine, so the port exits at the FIRST target (+10%) or the
   stop (-8%), whichever is touched first (high/low touch, engine next-close).
3. Entry price is the engine's next-close fill (close[t+1]), used as the
   anchor for the stop/TP levels (position_avg_price in Pine). Verified
   engine-consistent by tests/test_tv_ports.py.
"""

from __future__ import annotations

import pandas as pd

from analytics.technical import _sma
from strategies.ports import base
from strategies.ports.base import percentrank, simulate_positions
from strategies.ports import _register, PortInfo

SLUG = "hybrid_breakout_vcp"

DEFAULT_PARAMS = dict(
    len50=50, len150=150, len200=200, ma_up_lookback=20,
    atr_len=3, lookback=150, percentile_max=40.0, close_range_max=4.0,
    use_vol_filter=True, vol_ma_len=20, vol_mult=1.2,
    breakout_lookback=3, stop_loss_pct=0.08, take1_pct=0.10,
    start_date="2018-01-01", end_date="2099-01-01",
)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    close, high, low = df["close"], df["high"], df["low"]

    ma50 = _sma(close, p["len50"])
    ma150 = _sma(close, p["len150"])
    ma200 = _sma(close, p["len200"])
    bullish = (
        (close > ma50) & (ma50 > ma150) & (ma150 > ma200)
        & (ma200 > ma200.shift(1))
        & (ma200 > ma200.shift(p["ma_up_lookback"]))
        & (ma50 >= ma50.shift(1))
    )

    h_range = high.rolling(p["atr_len"]).max()
    l_range = low.rolling(p["atr_len"]).min()
    range_pct = (h_range - l_range) / h_range * 100
    h_close = close.rolling(p["atr_len"]).max()
    l_close = close.rolling(p["atr_len"]).min()
    close_range_pct = (h_close - l_close) / h_close * 100

    range_percentile = percentrank(range_pct, p["lookback"])
    squeeze = (range_percentile <= p["percentile_max"]) | \
        (close_range_pct <= p["close_range_max"])

    vol_ma = _sma(df["volume"], p["vol_ma_len"]) if "volume" in df.columns \
        else pd.Series(0.0, index=df.index)
    volume_ok = (not p["use_vol_filter"]) | (df["volume"] > vol_ma * p["vol_mult"])

    recent_high = high.rolling(p["breakout_lookback"]).max()
    stop_crossed = close > recent_high.shift(1)

    idx = pd.to_datetime(df.index)
    in_date = (idx >= pd.Timestamp(p["start_date"])) & \
        (idx <= pd.Timestamp(p["end_date"]))

    long_signal = bullish & squeeze & volume_ok & in_date
    entries = long_signal & stop_crossed

    walk = simulate_positions(
        entries, close,
        lambda j, price, frame: (
            frame["low"].iloc[j] <= price * (1 - p["stop_loss_pct"])
            or frame["high"].iloc[j] >= price * (1 + p["take1_pct"])
        ),
        df,
    )
    return {"entries": entries, "exits": walk.exits,
            "entry_price": walk.entry_price}


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
        tv_url="https://www.tradingview.com/script/hezSShJr-Hybrid-Breakout-VCP-Inspired-Trend/",
        tv_author="blitz_locked",
        tv_script_name="Hybrid Breakout | VCP-Inspired Trend",
        mechanism_family="breakout",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "stop-buy approximated as close crossing the prior 3-bar high",
            "tiered TP1/TP2/TP3 scale-outs collapsed to first target (+10%) or 8% stop",
            "stop/TP fills use high/low touch with engine next-close execution",
            "author date window 2018-01-01+ applied unchanged",
        ],
    ),
    build_rule,
)
