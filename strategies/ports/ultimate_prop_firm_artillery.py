"""
strategies/ports/ultimate_prop_firm_artillery.py -- port of "Ultimate Prop Firm
[Artillery]" (ArtilleryTrades, tv_url https://www.tradingview.com/script/
PkUPYbw7-Ultimate-Prop-Firm-Artillery/), source in
storage/tv_scripts/ultimate_prop_firm_artillery.pine.

Author design (from source, verbatim)
-------------------------------------
Pivot-reversal supply/demand strategy. The most recent confirmed swing pivot
high/low brackets a supply/demand zone one sd_mult*ATR wide on each side.
Entry long on the bar that CONFIRMS a pivot low (3/2 window) when price trades
into the demand zone (low <= demand_hi), RSI(14) >= rsi_bull 40, the bar is
bullish (close > open), and volume >= sma20*0.8; short mirrors on the
confirmation bar of a pivot high into the supply zone with RSI <= 60 and a
bearish bar. Entries only while flat. Per entry the script anchors a hard stop
at sl_mult*ATR and a 3-leg take-profit ladder (40% at +1R, 50% at +2R, the
remainder at +3.5R) off the entry close. Prop-firm-style guards wrap the
mechanism: an intraday session window (HHMM ET), a max trades/day cap, a daily
drawdown % cap (recomputed from strategy equity at each day change), and an
end-of-session flatten.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. The prop-firm risk guards are equity/intraday constructs and are NOT
   ported: the daily-DD cap (needs a strategy equity series) and the
   max-trades/day counter (order counting is engine policy); the session
   window and EOD flatten are time-of-day logic on an intraday clock. The
   session window is carried as a DEFAULT-OFF mask (use_session_filter=False)
   -- on daily bars the hourly clock is degenerate, so enabling it is only
   meaningful on intraday frames (documented like the Keltner session cut).
2. Pivot levels carry forward by ffill as in the source (last_ph/last_pl
   vars); the zone is recomputed every bar from the LATEST carried pivot.
3. The 3-leg TP ladder (40/50/10% at +1R/+2R/+3.5R, shared stop) is collapsed
   to a single-position exit at the first bar whose range touches ANY exit
   level (stop or the +1R target), anchored to the SIGNAL bar's close and ATR
   (the source freezes e_sl/e_tp* at the entry bar). Residual legs' futures
   are dropped.
4. The RSI/volume filters are evaluated at the signal bar; the flat-only entry
   gate maps onto the engine's single-position replay.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _rsi, _sma
from strategies.ports import base
from strategies.ports.base import atr_wilder, pivot_high, pivot_low
from strategies.ports.base import simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "ultimate_prop_firm_artillery"

DEFAULT_PARAMS = dict(
    piv_left=3, piv_right=2, atr_len=14, sd_mult=1.0,
    rsi_len=14, rsi_bull=40.0, rsi_bear=60.0,
    vol_len=20, vol_mult=0.8,
    sl_mult=1.5, tp1_mult=1.0, tp2_mult=2.0, tp3_mult=3.5,
    use_session_filter=False, sess_start=930, sess_end=1555,
)


def _session_mask(df, start_hhmm, end_hhmm):
    """DEFAULT-OFF ET session mask from the index (degenerate on daily bars)."""
    mask = pd.Series(True, index=df.index)
    if not isinstance(df.index, pd.DatetimeIndex):
        return mask
    try:
        t = df.index.tz_convert("America/New_York")
    except Exception:
        return mask
    hhmm = t.hour * 100 + t.minute
    return (hhmm >= start_hhmm) & (hhmm <= end_hhmm)


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    open_, high, low, close, volume = (
        df["open"], df["high"], df["low"], df["close"], df["volume"])

    atr = atr_wilder(high, low, close, p["atr_len"])
    piv_h = pivot_high(high, p["piv_left"], p["piv_right"])
    piv_l = pivot_low(low, p["piv_left"], p["piv_right"])
    last_ph = piv_h.ffill()
    last_pl = piv_l.ffill()

    demand_hi = last_pl + atr * p["sd_mult"]
    supply_lo = last_ph - atr * p["sd_mult"]

    rsi = _rsi(close, p["rsi_len"])
    vma = _sma(volume, p["vol_len"])
    vol_ok = volume >= vma * p["vol_mult"]

    in_sess = (pd.Series(True, index=df.index) if not p["use_session_filter"]
               else _session_mask(df, p["sess_start"], p["sess_end"]))

    long_go = (piv_l.notna() & (low <= demand_hi) & (rsi >= p["rsi_bull"])
               & (close > open_) & vol_ok & in_sess)
    short_go = (piv_h.notna() & (high >= supply_lo) & (rsi <= p["rsi_bear"])
                & (close < open_) & vol_ok & in_sess)

    long_entries = long_go.fillna(False)
    short_entries = short_go.fillna(False)

    close_arr = close.to_numpy(dtype=float)
    atr_arr = atr.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)

    def long_exit_trigger(j, sig_i, price, frame):
        e_sl = close_arr[sig_i] - atr_arr[sig_i] * p["sl_mult"]
        e_tp1 = close_arr[sig_i] + atr_arr[sig_i] * p["tp1_mult"]
        return low_arr[j] <= e_sl or high_arr[j] >= e_tp1

    def short_exit_trigger(j, sig_i, price, frame):
        e_sl = close_arr[sig_i] + atr_arr[sig_i] * p["sl_mult"]
        e_tp1 = close_arr[sig_i] - atr_arr[sig_i] * p["tp1_mult"]
        return high_arr[j] >= e_sl or low_arr[j] <= e_tp1

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger,
        df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, pivot S/D reversal)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/PkUPYbw7-Ultimate-Prop-Firm-Artillery/",
        tv_author="ArtilleryTrades",
        tv_script_name="Ultimate Prop Firm [Artillery]",
        mechanism_family="reversal",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "daily-DD cap and max-trades/day guards NOT ported (equity/"
            "intraday constructs); session window default-OFF so daily-bar "
            "runs aren't silently gated; EOD flatten dropped",
            "pivot levels carry forward (last_ph/last_pl ffill), zone "
            "recomputed every bar from the latest carried pivot",
            "3-leg TP ladder collapsed to exit at first touch of any level "
            "(stop or +1R), anchored to the signal bar close & ATR",
            "entries flat-only via engine; RSI/volume filters at signal bar",
        ],
    ),
    build_rule,
)