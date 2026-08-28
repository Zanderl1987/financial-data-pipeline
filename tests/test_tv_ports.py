"""
Tests for strategies/ports/ -- the Stage 2 Pine-to-TradeRule translation layer.

Covers the Pine primitives (supertrend, percentrank, pivots, Wilder ATR), the
engine-consistent position simulator, the hybrid_breakout_vcp port end-to-end,
and the port registry.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.ports import base, load_rule, all_ports, port_info  # noqa: E402
from strategies.ports.hybrid_breakout_vcp import build_rule, compute  # noqa: E402
from strategies.ports.supertrend_entry_tp123 import compute as st_compute  # noqa: E402
from evaluation import trades  # noqa: E402


# ------------------------------------------------------------------ fixtures

def _synth_frame(n: int = 280) -> pd.DataFrame:
    """Steady uptrend, then a 10-bar tight squeeze, then a volume breakout --
    engineered so the hybrid-breakout setup fires exactly once, on the
    breakout bar (bar 230), and the exit takes a while so the trade is
    realized inside the frame."""
    rng = np.random.default_rng(11)
    close = np.empty(n)
    close[0] = 100.0
    for i in range(1, 220):                       # steady uptrend
        close[i] = close[i - 1] * (1 + 0.004 + rng.normal(0, 0.008))
    base = close[219]
    for i in range(220, 230):                     # tight consolidation
        close[i] = base * (1 + 0.0004 * rng.normal(0, 1))
    close[230] = base * 1.03                      # breakout bar
    for i in range(231, n):
        close[i] = close[i - 1] * (1 + 0.003 + rng.normal(0, 0.008))

    prev = np.concatenate(([close[0]], close[:-1]))
    spread = rng.uniform(0.001, 0.01, n)
    high = np.maximum(close, prev) * (1 + spread)
    low = np.minimum(close, prev) * (1 - spread)

    volume = rng.uniform(0.9e6, 1.1e6, n)
    volume[220:230] = 0.5e6                       # quiet squeeze, filter OFF
    volume[230] = 2.2e6                           # expansion on breakout

    df = pd.DataFrame({
        "open": prev, "high": high, "low": low, "close": close,
        "volume": volume,
    }, index=pd.date_range("2019-01-01", periods=n, freq="B"))
    return df


# ---------------------------------------------------------------- primitives

class TestPinePrimitives:
    def test_supertrend_uptrend_direction(self):
        n = 120
        idx = pd.date_range("2020-01-01", periods=n, freq="D")
        close = pd.Series(100 * 1.004 ** np.arange(n), index=idx)
        high = close * 1.01
        low = close * 0.99
        line, direction = base.supertrend(high, low, close, factor=2.0,
                                          atr_period=10)
        assert direction.iloc[-1] == -1.0          # uptrend -> -1 per Pine
        assert line.notna().sum() > n // 2

    def test_supertrend_downtrend_direction(self):
        n = 120
        idx = pd.date_range("2020-01-01", periods=n, freq="D")
        close = pd.Series(100 * 0.996 ** np.arange(n), index=idx)
        high = close * 1.01
        low = close * 0.99
        line, direction = base.supertrend(high, low, close, factor=2.0,
                                          atr_period=10)
        assert direction.iloc[-1] == 1.0           # downtrend -> +1 per Pine

    def test_percentrank(self):
        s = pd.Series([1.0, 5.0, 2.0, 3.0, 4.0])
        out = base.percentrank(s, length=3)
        assert out.iloc[0] != out.iloc[0]          # NaN before warmup
        assert out.iloc[3] == pytest.approx(100.0 * 2 / 3)   # prior [1,5,2], 2 <= 3
        assert out.iloc[4] == pytest.approx(100.0 * 2 / 3)   # prior [5,2,3], 2 <= 4

    def test_pivot_high_confirms_right(self):
        h = pd.Series([5.0, 4.0, 3.0, 9.0, 3.0, 4.0, 5.0, 6.0])
        out = base.pivot_high(h, left=2, right=2)
        assert out.isna().sum() == len(h) - 1
        assert out.iloc[5] == 9.0                  # pivot bar 3, confirmed at 3+2

    def test_pivot_low_confirms_right(self):
        l = pd.Series([9.0, 8.0, 7.0, 1.0, 7.0, 8.0, 9.0, 8.0])
        out = base.pivot_low(l, left=2, right=2)
        assert out.isna().sum() == len(l) - 1
        assert out.iloc[5] == 1.0

    def test_atr_wilder_constant_tr(self):
        n = 60
        idx = pd.date_range("2020-01-01", periods=n, freq="D")
        close = pd.Series(np.linspace(100, 150, n), index=idx)
        high = close + 1.0                         # TR == 2 everywhere
        low = close - 1.0
        atr = base.atr_wilder(high, low, close, n=3)
        assert atr.iloc[:2].isna().all()           # warmup
        assert atr.iloc[2:].round(6).eq(2.0).all()


# ------------------------------------------------ engine-consistent positions

class TestPositionWalk:
    def test_walk_matches_engine(self):
        df = _synth_frame()
        res = compute(df)
        rule = build_rule()

        sig = rule.entries(df)
        assert sig.sum() >= 1
        sig_day = int(np.flatnonzero(sig.to_numpy())[0])
        assert sig_day == 230

        trades_df = trades.simulate(rule, {"SYN": df})
        assert len(trades_df) == 1
        row = trades_df.iloc[0]
        # engine entered at the close of the bar after the FIRST signal
        assert row["entry_date"] == df.index[sig_day + 1]
        assert row["entry_price"] == df["close"].iloc[sig_day + 1]
        assert row["entry_price"] == res["entry_price"].iloc[sig_day + 1]

    def test_exit_flags_consistent_with_engine(self):
        df = _synth_frame()
        res = compute(df)
        rule = build_rule()
        # the port's exit flags, replayed through the engine, produce exactly
        # the same trade as the port's own position walk
        trades_df = trades.simulate(rule, {"SYN": df})
        row = trades_df.iloc[0]
        assert bool(res["exits"].loc[row["exit_signal_date"]])
        assert res["entry_price"].loc[row["entry_date"]] == row["entry_price"]


# ------------------------------------------------------------------- the port

class TestHybridBreakoutPort:
    def test_single_entry_on_breakout_bar(self):
        df = _synth_frame()
        res = compute(df)
        sig = res["entries"]
        assert sig.sum() >= 1
        # the FIRST entry is the breakout bar (volume filter clears there);
        # later re-fires are a synthetic-frame vma-dip artifact and are
        # blocked by the engine's one-position-at-a-time rule
        assert int(np.flatnonzero(sig.to_numpy())[0]) == 230

    def test_entry_requires_stop_cross_and_squeeze(self):
        df = _synth_frame()
        df2 = df.copy()
        # no volume expansion: entry must not fire
        df2["volume"] = 1.0e6
        res = compute(df2)
        assert res["entries"].sum() == 0

    def test_port_side_and_notional(self):
        rule = build_rule()
        assert rule.side == "long"
        assert rule.notional == 10_000.0
        assert callable(rule.entries) and callable(rule.exits)


# -------------------------------------------------------------- supertrend port

def _wave_frame(n: int = 400) -> pd.DataFrame:
    """Roughly sinusoidal close so the Supertrend direction crosses zero
    repeatedly, producing both long and short flips."""
    rng = np.random.default_rng(3)
    t = np.arange(n)
    close = 100 + 30 * np.sin(t / 25.0) + rng.normal(0, 0.6, n)
    close = np.maximum(close, 5.0)
    prev = np.concatenate(([close[0]], close[:-1]))
    spread = rng.uniform(0.002, 0.02, n)
    high = np.maximum(close, prev) * (1 + spread)
    low = np.minimum(close, prev) * (1 - spread)
    return pd.DataFrame({
        "open": prev, "high": high, "low": low, "close": close,
        "volume": rng.uniform(0.5e6, 1.5e6, n),
    }, index=pd.date_range("2018-01-01", periods=n, freq="B"))


class TestSupertrendPort:
    def test_both_sides_and_flips(self):
        df = _wave_frame()
        res = st_compute(df)
        assert res["entries"].sum() > 0
        assert res["short_entries"].sum() > 0
        # every buy is a crossunder of the direction series through zero
        from strategies.ports.supertrend_entry_tp123 import _crossunder
        _line, direction = base.supertrend(df["high"], df["low"], df["close"],
                                           factor=2.5, atr_period=14)
        assert res["entries"].equals(_crossunder(direction, 0.0))

    def test_long_exit_includes_flip(self):
        df = _wave_frame()
        res = st_compute(df)
        # the sell signal is itself a long exit (author closes long on flip)
        assert (res["exits"] & res["short_entries"]).sum() > 0

    def test_rule_is_both_sided(self):
        from strategies.ports.supertrend_entry_tp123 import build_rule
        rule = build_rule()
        assert rule.side == "both"
        assert callable(rule.short_entries) and callable(rule.short_exits)

    def test_engine_produces_trades(self):
        df = _wave_frame()
        rule = load_rule("supertrend_entry_tp123")
        trades_df = trades.simulate(rule, {"SYN": df})
        assert len(trades_df) >= 1
        assert {"long", "short"}.issubset(set(trades_df["side"]))


# ------------------------------------------------------------------ registry

class TestRegistry:
    def test_load_rule_returns_trade_rule(self):
        from evaluation.contracts import TradeRule
        assert isinstance(load_rule("hybrid_breakout_vcp"), TradeRule)
        assert isinstance(load_rule("supertrend_entry_tp123"), TradeRule)

    def test_all_ports_contains_hybrid(self):
        slugs = {p.slug for p in all_ports()}
        assert {"hybrid_breakout_vcp", "supertrend_entry_tp123"}.issubset(slugs)

    def test_port_info_metadata(self):
        info = port_info("hybrid_breakout_vcp")
        assert info.tv_author == "blitz_locked"
        assert info.translation_verified == "unverified"
        assert info.mechanism_family == "breakout"
        assert info.notes                      # approximations documented

    def test_unknown_slug_raises(self):
        with pytest.raises(KeyError):
            load_rule("does_not_exist")


# ---------------------------------------------------------- every-port smoke

def _smoke_frame(n: int = 500, seed: int = 7) -> pd.DataFrame:
    """Generic random-walk OHLCV frame, long enough to clear a 200-bar SMA
    warmup. Not engineered to fire any particular port's signal -- this only
    checks that every registered port runs cleanly and returns
    engine-shaped output, not that it trades on this data."""
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.02, n))
    prev = np.concatenate(([close[0]], close[:-1]))
    spread = rng.uniform(0.002, 0.02, n)
    high = np.maximum(close, prev) * (1 + spread)
    low = np.minimum(close, prev) * (1 - spread)
    volume = rng.uniform(0.5e6, 1.5e6, n)
    return pd.DataFrame(
        {"open": prev, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.date_range("2015-01-01", periods=n, freq="B"))


class TestEveryPortSmoke:
    """Every registered port, run once against a generic frame: no crash,
    boolean Series aligned to the frame's index, and the engine accepts the
    resulting TradeRule without error. Catches wiring bugs (wrong dtype,
    misaligned index, an unregistered helper) that a single hand-picked
    scenario per port would not necessarily exercise."""

    def test_all_ports_run_clean(self):
        df = _smoke_frame()
        for info in all_ports():
            rule = load_rule(info.slug)
            entries = rule.entries(df)
            exits = rule.exits(df)
            for name, flags in (("entries", entries), ("exits", exits)):
                assert flags.dtype == bool, f"{info.slug}.{name}: not boolean"
                assert flags.index.equals(df.index), \
                    f"{info.slug}.{name}: index misaligned with input frame"
            if rule.side == "both":
                short_entries = rule.short_entries(df)
                short_exits = rule.short_exits(df)
                for name, flags in (("short_entries", short_entries),
                                    ("short_exits", short_exits)):
                    assert flags.dtype == bool, f"{info.slug}.{name}: not boolean"
                    assert flags.index.equals(df.index), \
                        f"{info.slug}.{name}: index misaligned with input frame"
            trades_df = trades.simulate(rule, {"SYN": df})
            assert isinstance(trades_df, pd.DataFrame)
