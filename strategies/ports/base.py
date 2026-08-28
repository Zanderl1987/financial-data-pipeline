"""
strategies/ports/base.py -- shared machinery for porting Pine strategy scripts
to evaluation.contracts.TradeRule.

Why the position simulator exists
---------------------------------
TradeRule entries/exits are stateless callables (df) -> boolean Series. The
engine (evaluation/trades.py) executes signals at the close of t+1 and holds
one position per symbol at a time. Several of the collected Pine strategies
size their exits off the *entry price* (fixed-percent stops / take-profits
anchored to `strategy.position_avg_price`). A rule that only sees the OHLCV
frame cannot know its own entry price, so the port recomputes it internally:
`simulate_positions()` walks the same next-close, one-position-at-a-time
semantics the engine uses, so the exit flags it emits are exactly the flags
the engine will consume for the same position progression.

The rule closures are pure functions of the frame (no shared mutable state):
`entries(df)` and `exits(df)` each recompute everything, which is O(n) per
symbol and deterministic -- a position's exit flags never depend on whether
the engine happened to call entries before exits.

All ports here are `translation_verified = "unverified"` per the campaign
pre-registration (experiments/2026-08-11_tv-strategy-catalog-preregistration.md
section 6): ported from source, no external ground-truth check. Variant
choices below (Supertrend, percentrank, pivots) are documented, not assumed
to match TradingView's engine bit-for-bit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional

import numpy as np
import pandas as pd

from analytics.technical import _rma, _sma  # in-repo primitives
from evaluation.contracts import TradeRule


# ------------------------------------------------------------- Pine primitives

def atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series,
               n: int) -> pd.Series:
    """ta.atr -- Wilder-smoothed true range (RMA of TR), like
    analytics.technical's atr14."""
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    return _rma(tr, n)


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               factor: float = 3.0, atr_period: int = 10):
    """
    Pine `ta.supertrend(factor, atr_period)`.

    Returns (line, direction) with direction == -1 in an uptrend, +1 in a
    downtrend, matching Pine's sign convention (`isUpTrend = direction < 0`).
    Implements the canonical community variant: upper/lower bands from
    hl2 +/- factor*ATR, ratcheted so the band only ever widens while the
    prior close stays on its side of the previous band, trend = close vs the
    ratcheted bands with persistence.
    """
    hl2 = (high + low) / 2.0
    atr = atr_wilder(high, low, close, atr_period)
    up = hl2 - factor * atr
    dn = hl2 + factor * atr
    c = close.to_numpy(dtype=float)
    up_a = up.to_numpy(dtype=float)
    dn_a = dn.to_numpy(dtype=float)
    n = len(c)

    line = np.full(n, np.nan)
    direction = np.zeros(n)
    if n == 0:
        return (pd.Series(line, index=close.index),
                pd.Series(direction, index=close.index))

    up_t, dn_t = up_a[0], dn_a[0]
    trend = 1
    line[0] = up_t
    direction[0] = -1.0
    for i in range(1, n):
        # Canonical ratchet: while the prior close stays on its side of the
        # RAW band, the band only ever widens (max for support / min for
        # resistance); a close through the band resets it to the raw value.
        up_t = max(up_a[i], up_t) if c[i - 1] > up_a[i - 1] else up_a[i]
        dn_t = min(dn_a[i], dn_t) if c[i - 1] < dn_a[i - 1] else dn_a[i]
        if c[i] > dn_t:
            trend = 1
        elif c[i] < up_t:
            trend = -1
        line[i] = up_t if trend == 1 else dn_t
        direction[i] = -1.0 if trend == 1 else 1.0
    return (pd.Series(line, index=close.index),
            pd.Series(direction, index=close.index))


def percentrank(source: pd.Series, length: int) -> pd.Series:
    """
    Pine `ta.percentrank(source, length)`: percentile rank of the current
    value against the `length` prior values, in [0, 100] (TV counts prior
    values <= current, so a max-rolling value ranks 100).
    """
    s = source.to_numpy(dtype=float)
    n = len(s)
    out = np.full(n, np.nan)
    for i in range(length, n):
        out[i] = 100.0 * np.count_nonzero(s[i - length:i] <= s[i]) / length
    return pd.Series(out, index=source.index)


def _pivot(source: pd.Series, left: int, right: int,
           want_high: bool) -> pd.Series:
    """Shared pivot detection. Returns the pivot value on the bar it is
    *confirmed* (pivot bar + right), NaN elsewhere, matching Pine's
    ta.pivothigh/pivotlow series shape."""
    s = source.to_numpy(dtype=float)
    n = len(s)
    out = np.full(n, np.nan)
    for i in range(left, n - right):
        window = s[i - left:i + right + 1]
        if np.isnan(s[i]):
            continue
        is_max = want_high and s[i] == np.max(window) and \
            np.count_nonzero(window == s[i]) == 1
        is_min = not want_high and s[i] == np.min(window) and \
            np.count_nonzero(window == s[i]) == 1
        if is_max or is_min:
            out[i + right] = s[i]
    return pd.Series(out, index=source.index)


def pivot_high(high: pd.Series, left: int, right: int) -> pd.Series:
    """Pine `ta.pivothigh(high, left, right)`."""
    return _pivot(high, left, right, want_high=True)


def pivot_low(low: pd.Series, left: int, right: int) -> pd.Series:
    """Pine `ta.pivotlow(low, left, right)`."""
    return _pivot(low, left, right, want_high=False)


# ---------------------------------------------- engine-consistent positions

@dataclass
class PositionWalk:
    """Result of simulating the engine's position progression."""
    entry_price: pd.Series     # NaN until an engine entry executes (close t+1)
    exits: pd.Series           # engine-consistent exit flags (bool)


def simulate_positions(entry_flags: pd.Series, close: pd.Series,
                       exit_trigger: Callable[[int, float, pd.DataFrame],
                                              bool],
                       df: pd.DataFrame) -> PositionWalk:
    """
    Replay evaluation/trades.simulate_symbol's position progression on one
    frame so exit flags that depend on the entry price are exactly what the
    engine will consume.

    Semantics mirrored from the engine: a signal on day t enters at close[t+1]
    (dropped if t is the last bar), one position at a time, an exit signal on
    day j exits at close[j+1], and re-entry is blocked until the exit bar has
    passed (the engine sets next_free = exit_signal_day + 2).
    """
    n = len(close)
    c = close.to_numpy(dtype=float)
    flags = entry_flags.fillna(False).to_numpy(dtype=bool)
    ep = np.full(n, np.nan)
    out = np.zeros(n, dtype=bool)
    next_free = 0
    for i in range(n):
        if i < next_free or not flags[i]:
            continue
        entry_i = i + 1
        if entry_i >= n:
            next_free = n
            continue
        price = c[entry_i]
        if not np.isfinite(price) or price <= 0:
            continue
        ep[entry_i] = price
        hit_day = None
        for j in range(entry_i + 1, n):
            if exit_trigger(j, price, df):
                hit_day = j
                break
        if hit_day is None:
            next_free = n
            continue
        out[hit_day] = True
        next_free = hit_day + 2
    return PositionWalk(entry_price=pd.Series(ep, index=close.index),
                        exits=pd.Series(out, index=close.index))


@dataclass
class PositionWalkBoth:
    """Result of simulating a side='both' rule's MERGED position progression
    -- one shared timeline, long and short mutually exclusive."""
    entry_price_long: pd.Series
    exits: pd.Series            # long exit flags
    entry_price_short: pd.Series
    short_exits: pd.Series      # short exit flags


def simulate_positions_both(long_entries: pd.Series, short_entries: pd.Series,
                            close: pd.Series,
                            long_trigger: Callable[[int, float, pd.DataFrame], bool],
                            short_trigger: Callable[[int, float, pd.DataFrame], bool],
                            df: pd.DataFrame) -> PositionWalkBoth:
    """
    Like `simulate_positions`, but for side='both' rules whose exit levels
    are anchored to the entry price on EITHER side.

    evaluation.trades.simulate_symbol merges long and short entry signals
    into one chronologically-sorted, mutually-exclusive timeline (one
    `next_free` gate shared across both sides) -- a short signal is dropped
    while a long position is open, and vice versa. Running `simulate_positions`
    independently per side would miss that shared gate and could compute an
    entry price/timing the real engine would never produce. This replays the
    SAME merged-timeline logic as simulate_symbol.
    """
    n = len(close)
    c = close.to_numpy(dtype=float)
    long_flags = long_entries.fillna(False).to_numpy(dtype=bool)
    short_flags = short_entries.fillna(False).to_numpy(dtype=bool)
    ep_long = np.full(n, np.nan)
    ep_short = np.full(n, np.nan)
    out_long = np.zeros(n, dtype=bool)
    out_short = np.zeros(n, dtype=bool)

    entry_positions = sorted(
        [(i, "long") for i in np.flatnonzero(long_flags)]
        + [(i, "short") for i in np.flatnonzero(short_flags)]
    )
    next_free = 0
    for sig_i, side in entry_positions:
        if sig_i < next_free:
            continue
        entry_i = sig_i + 1
        if entry_i >= n:
            next_free = n
            continue
        price = c[entry_i]
        if not np.isfinite(price) or price <= 0:
            continue
        trigger = long_trigger if side == "long" else short_trigger
        (ep_long if side == "long" else ep_short)[entry_i] = price
        hit_day = None
        for j in range(entry_i + 1, n):
            if trigger(j, price, df):
                hit_day = j
                break
        if hit_day is None:
            next_free = n
            continue
        (out_long if side == "long" else out_short)[hit_day] = True
        next_free = hit_day + 2
    return PositionWalkBoth(
        entry_price_long=pd.Series(ep_long, index=close.index),
        exits=pd.Series(out_long, index=close.index),
        entry_price_short=pd.Series(ep_short, index=close.index),
        short_exits=pd.Series(out_short, index=close.index),
    )


def level_exits(entry_price: pd.Series, high: pd.Series, low: pd.Series,
                stop_pct: float, target_pcts: "list[float]",
                side: str = "long") -> pd.Series:
    """
    Exit flags for stop / take-profit levels anchored to the entry price
    (strategy.position_avg_price in Pine). Collapses multi-target scale-outs
    to the FIRST target, because the engine models full-position exits only.

    Long: low <= entry*(1-stop) or high >= entry*(1+target).
    Short: high >= entry*(1+stop) or low <= entry*(1-target).
    Using high/low (not close) matches limit/stop fills intrabar.
    """
    target = entry_price * (1 + target_pcts[0]) if side == "long" else \
        entry_price * (1 - target_pcts[0])
    stop = entry_price * (1 - stop_pct) if side == "long" else \
        entry_price * (1 + stop_pct)
    if side == "long":
        flags = (low <= stop) | (high >= target)
    else:
        flags = (high >= stop) | (low <= target)
    return flags.fillna(False)


# ------------------------------------------------------------ rule factories

def stateful_rule(name: str, compute: Callable[[pd.DataFrame], Dict],
                  side: str = "long", notional: float = 10_000.0) -> TradeRule:
    """
    Wrap a per-frame `compute(df) -> dict` (keys entries/exits, plus
    short_entries/short_exits for side="both") into a TradeRule. Closures are
    pure functions of the frame, so call order never matters.
    """
    if side not in ("long", "short", "both"):
        raise ValueError(f"stateful_rule: side must be long/short/both, got {side!r}")

    def entries(df):  # pragma: no cover - thin indirection
        return compute(df)["entries"]

    def exits(df):  # pragma: no cover - thin indirection
        return compute(df)["exits"]

    if side == "both":
        def short_entries(df):  # pragma: no cover - thin indirection
            return compute(df)["short_entries"]

        def short_exits(df):  # pragma: no cover - thin indirection
            return compute(df)["short_exits"]

        return TradeRule(name=name, entries=entries, exits=exits, side="both",
                         short_entries=short_entries, short_exits=short_exits,
                         notional=notional)
    return TradeRule(name=name, entries=entries, exits=exits, side=side,
                     notional=notional)
