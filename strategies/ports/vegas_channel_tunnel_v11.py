"""
strategies/ports/vegas_channel_tunnel_v11.py -- port of "Vegas Channel Tunnel
Strategy v1.1" (yubinzhang802, tv_url https://www.tradingview.com/script/
J6JxBgkr-Vegas-Channel-Tunnel-Strategy-v1-1/), source in
storage/tv_scripts/vegas_channel_tunnel_v11.pine.

Author design (from source, verbatim)
-------------------------------------
Vegas tunnel. Tunnel = max/min of a fast/slow EMA pair (tuned 55/89), macro
band = a 4x-slower pair (576/676), trigger = a short EMA (8, "12 classic"). A
long ARMS when price AND the trigger close above the tunnel upper band while
price is above the macro band (optional filter); it disarms if close falls
back inside the tunnel or the macro filter breaks. The entry fires on the
RETRACE: while armed, a bar whose low wicks into the tunnel upper boundary
(+retraceTol*ATR) yet CLOSES back above it. Mirror for shorts. Entries only
flat-or-opposite; each entry gets a hard stop (ATR*atrSlMult or the opposite
tunnel band + buffer), risk measured off it, and a 3.5R/3R RR ladder with an
optional 50% scale-out at TP1 (1.5R), stop-to-breakeven and/or an ATR trail
that both engage only AFTER TP1 is hit. A regime filter (ADX >= 25 AND macro
slope sign) makes with-trend "ride" trades trail-only with no fixed target and
stands aside from counter-trend setups.

Port notes (approximations, recorded per pre-registration section 6)
-------------------------------------------------------------------
1. The arm/disarm/retrace state machine and the per-bar rStop ratchet replay
   the Pine var ordering exactly (retrace consumes the armed state before the
   disarm and re-arm blocks run; TP1 hit flips before breakeven/trail updates
   on the same bar).
2. The partial scale-out (scaleOutPct% at TP1) and the breakeven/trail
   ratchets are collapsed together: the single-position proxy exits when the
   RUN order would fill (stop rStop breached or the rrRatio target touched, or
   trail-only rStop for ride trades). The TP1 half-close offsets the effective
   average fill slightly; documented rather than modelled.
3. Entries allowed while flat-or-opposite in the source; the same-bar
   opposite entry cannot play out in the engine's single position (dropped by
   the next_free gate) -- position-management stays exit-order driven.
4. barsCooldown is a no-op at its default 0 and is unwired at non-zero values
   (would require joint entry/exit replay); slMethod is honoured as a string
   param. The tuned EMA lengths assume the author's 1H Gold intent (the
   mechanism itself is timeframe-portable -- no session/time logic).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.technical import _adx, _ema
from strategies.ports import base
from strategies.ports.base import atr_wilder, simulate_positions_both_indexed
from strategies.ports import _register, PortInfo

SLUG = "vegas_channel_tunnel_v11"

DEFAULT_PARAMS = dict(
    len12=8, len144=55, len169=89, len576=576, len676=676,
    use_macro_filter=True, retrace_tol=0.0,
    rr_ratio=3.0, sl_method="ATR", atr_len=14,
    atr_sl_mult=2.0, tunnel_buf=0.25,
    use_partial=True, scale_out_pct=50.0, tp1_r=1.5,
    use_breakeven=True, use_trail=True, trail_atr_mult=3.0,
    bars_cooldown=0,
    regime_filter=True, adx_len=14, adx_th=25.0, slope_len=20,
)


def _state_signals(j_lo, upper, lower, close, trend_up, trend_dn, broke_up,
                   broke_dn, tol):
    """Arm/disarm/retrace machine replicating the Pine block order."""
    n = len(close)
    long_state = 0
    short_state = 0
    long_retrace = np.zeros(n, dtype=bool)
    short_rebound = np.zeros(n, dtype=bool)
    for j in range(n):
        lon_ret = long_state == 1 and j_lo[j] <= upper[j] + tol[j] \
            and close[j] > upper[j] and trend_up[j]
        sho_ret = short_state == 1 and j_lo[j] >= lower[j] - tol[j] \
            and close[j] < lower[j] and trend_dn[j]
        if lon_ret:
            long_state = 0
        if sho_ret:
            short_state = 0
        if long_state == 1 and (not trend_up[j] or close[j] < upper[j]):
            long_state = 0
        if short_state == 1 and (not trend_dn[j] or close[j] > lower[j]):
            short_state = 0
        if trend_up[j] and broke_up[j]:
            long_state = 1
        if trend_dn[j] and broke_dn[j]:
            short_state = 1
        long_retrace[j] = lon_ret
        short_rebound[j] = sho_ret
    return long_retrace, short_rebound


def compute(df: pd.DataFrame, params: dict = None) -> dict:
    """Pure per-frame signal computation (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    high, low, close = df["high"], df["low"], df["close"]

    ema12 = _ema(close, p["len12"])
    e144 = _ema(close, p["len144"])
    e169 = _ema(close, p["len169"])
    e576 = _ema(close, p["len576"])
    e676 = _ema(close, p["len676"])

    tunnel_upper = np.maximum(e144, e169)
    tunnel_lower = np.minimum(e144, e169)
    macro_upper = np.maximum(e576, e676)
    macro_lower = np.minimum(e576, e676)

    atr = atr_wilder(high, low, close, p["atr_len"])
    tol = p["retrace_tol"] * atr

    adx_val = _adx(high, low, close, p["adx_len"])[0]
    macro_rising = e576 > e576.shift(p["slope_len"])
    strong_trend = adx_val >= p["adx_th"]
    strong_up = strong_trend & macro_rising
    strong_dn = strong_trend & ~macro_rising
    allow_long = (~p["regime_filter"]) | (~strong_dn)
    allow_short = (~p["regime_filter"]) | (~strong_up)

    trend_up = (~p["use_macro_filter"]) | (close > macro_upper)
    trend_dn = (~p["use_macro_filter"]) | (close < macro_lower)

    broke_up = (close > tunnel_upper) & (ema12 > tunnel_upper)
    broke_dn = (close < tunnel_lower) & (ema12 < tunnel_lower)

    long_retrace, short_rebound = _state_signals(
        low.to_numpy(dtype=float),
        tunnel_upper.to_numpy(dtype=float),
        tunnel_lower.to_numpy(dtype=float),
        close.to_numpy(dtype=float),
        trend_up.fillna(False).to_numpy(dtype=bool),
        trend_dn.fillna(False).to_numpy(dtype=bool),
        broke_up.fillna(False).to_numpy(dtype=bool),
        broke_dn.fillna(False).to_numpy(dtype=bool),
        tol.to_numpy(dtype=float))

    long_entries = pd.Series(long_retrace & allow_long.fillna(False),
                             index=df.index)
    short_entries = pd.Series(short_rebound & allow_short.fillna(False),
                              index=df.index)

    close_arr = close.to_numpy(dtype=float)
    atr_arr = atr.to_numpy(dtype=float)
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)
    up_arr = tunnel_upper.to_numpy(dtype=float)
    dn_arr = tunnel_lower.to_numpy(dtype=float)
    strong_up_arr = strong_up.fillna(False).to_numpy(dtype=bool)
    strong_dn_arr = strong_dn.fillna(False).to_numpy(dtype=bool)

    use_be = p["use_breakeven"]
    use_trail = p["use_trail"]
    trail_mult = p["trail_atr_mult"]
    tp1_r = p["tp1_r"]
    rr = p["rr_ratio"]
    sl_atr = p["atr_sl_mult"]
    tun_buf = p["tunnel_buf"]
    sl_method = p["sl_method"]
    use_partial = p["use_partial"]

    def _replay(j, sig_i, long_side):
        e_ref = close_arr[sig_i]
        if sl_method == "ATR":
            slp = (e_ref - atr_arr[sig_i] * sl_atr if long_side
                   else e_ref + atr_arr[sig_i] * sl_atr)
        else:
            slp = (dn_arr[sig_i] - atr_arr[sig_i] * tun_buf if long_side
                   else up_arr[sig_i] + atr_arr[sig_i] * tun_buf)
        risk = e_ref - slp if long_side else slp - e_ref
        if not (risk > 0):
            return False
        r_stop = slp
        tp1_hit = False
        ride = p["regime_filter"] and (
            strong_up_arr[sig_i] if long_side else strong_dn_arr[sig_i])
        trail_eff = use_trail or ride
        for jj in range(sig_i + 1, j + 1):
            tp1_l = e_ref + tp1_r * risk if long_side else e_ref - tp1_r * risk
            final = e_ref + rr * risk if long_side else e_ref - rr * risk
            if (high_arr[jj] >= tp1_l) if long_side else (low_arr[jj] <= tp1_l):
                tp1_hit = True
            if use_be and tp1_hit:
                r_stop = max(r_stop, e_ref) if long_side else min(r_stop, e_ref)
            if trail_eff and tp1_hit:
                trail = close_arr[jj] - atr_arr[jj] * trail_mult if long_side \
                    else close_arr[jj] + atr_arr[jj] * trail_mult
                r_stop = max(r_stop, trail) if long_side else min(r_stop, trail)
            if long_side:
                if low_arr[jj] <= r_stop:
                    return True
                if not ride and high_arr[jj] >= final:
                    return True
            else:
                if high_arr[jj] >= r_stop:
                    return True
                if not ride and low_arr[jj] <= final:
                    return True
        return False

    def long_exit_trigger(j, sig_i, price, frame):
        return short_entries.iloc[j] or _replay(j, sig_i, True)

    def short_exit_trigger(j, sig_i, price, frame):
        return long_entries.iloc[j] or _replay(j, sig_i, False)

    walk = simulate_positions_both_indexed(
        long_entries, short_entries, close, long_exit_trigger, short_exit_trigger,
        df)

    return {
        "entries": long_entries, "exits": walk.exits,
        "short_entries": short_entries, "short_exits": walk.short_exits,
    }


def build_rule(params: dict = None):
    """Author-default TradeRule (both sides, tunnel retrace)."""
    return base.stateful_rule(
        name=SLUG,
        compute=lambda df: compute(df, params),
        side="both",
    )


_register(
    PortInfo(
        slug=SLUG,
        tv_url="https://www.tradingview.com/script/J6JxBgkr-Vegas-Channel-Tunnel-Strategy-v1-1/",
        tv_author="yubinzhang802",
        tv_script_name="Vegas Channel Tunnel Strategy v1.1",
        mechanism_family="trend",
        param_count=len(DEFAULT_PARAMS),
        translation_verified="unverified",
        notes=[
            "arm/disarm/retrace state and rStop ratchet replay Pine var "
            "ordering (retrace consumes armed state first; TP1 flips before "
            "breakeven/trail on the same bar)",
            "partial scale-out collapsed: full-position proxy exits when the "
            "RUN order would fill (rStop breach or rrRatio target; trail-only "
            "rStop for ride trades)",
            "flat-or-opposite entries reduce to flat in the engine's "
            "single-position replay",
            "barsCooldown unwired at non-zero (no-op at default 0); tuned EMA "
            "lengths assume 1H Gold intent, mechanism is timeframe-portable",
        ],
    ),
    build_rule,
)