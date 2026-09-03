"""
tests/test_leakage_probe.py -- one-switch decision-timing leakage diagnostic.

one_switch_ablation()'s single-key-changed discipline gets its own tests
(zero changes and two changes must both raise, not just "more than one").
entry_lag_leakage() gets a hand-built price path where the correct answer
is known: a sharp jump lands exactly on the event date and never reverts,
so same-bar execution (the leaky switch) bakes that jump into the trade's
own measured return while next-bar execution (the safe default) enters
after the jump has already happened and captures none of it. The two
entry_lag settings must therefore produce measurably different average
trade returns on this fixture -- not just "some numeric difference", the
specific direction the leak predicts.
"""

import numpy as np
import pandas as pd
import pytest

import event_backtest as eb
from evaluation import leakage_probe as lp


class TestOneSwitchAblation:
    def test_two_changed_keys_raises(self):
        def fn(a, b):
            return a + b
        with pytest.raises(ValueError, match="exactly one key"):
            lp.one_switch_ablation(fn, {"a": 1, "b": 1}, {"a": 2, "b": 2}, lambda r: r)

    def test_zero_changed_keys_raises(self):
        def fn(a):
            return a
        with pytest.raises(ValueError, match="exactly one key"):
            lp.one_switch_ablation(fn, {"a": 1}, {"a": 1}, lambda r: r)

    def test_reports_inflation(self):
        def fn(x):
            return x * 2
        out = lp.one_switch_ablation(fn, {"x": 1}, {"x": 5}, lambda r: r)
        assert out["safe_metric"] == 2
        assert out["leaky_metric"] == 10
        assert out["inflation"] == 8

    def test_none_metric_propagates_as_none_inflation(self):
        def fn(x):
            return None
        out = lp.one_switch_ablation(fn, {"x": 1}, {"x": 2}, lambda r: r)
        assert out["inflation"] is None


class TestEntryLagLeakage:
    @pytest.fixture
    def jump_prices(self, monkeypatch):
        idx = pd.bdate_range("2020-01-01", periods=300)
        # small daily noise throughout -- realistic, and gives the SAFE
        # (post-jump) trades nonzero return variance too, so this isn't
        # comparing a real number against a degenerate-guard None
        rng = np.random.default_rng(11)
        close = 100 * np.cumprod(1 + rng.normal(0.0, 0.001, 300))
        event_locs = [50, 100, 150, 200, 250]
        # varying jump sizes (not identical) so trade returns have nonzero
        # variance across events -- an identical jump every time gives every
        # trade the same return_pct and a degenerate (zero) std either way
        jumps = [1.14, 1.18, 1.20, 1.23, 1.27]
        for loc, jump in zip(event_locs, jumps):
            close[loc:] *= jump      # a permanent step, no reversion
        px = pd.Series(close, index=idx, name="TEST")

        def fake_load_close(symbol, start=None, end=None, price_table=None):
            return px.copy() if symbol == "TEST" else pd.Series(dtype=float)

        def fake_matrix(symbols, start=None, end=None, price_table=None):
            return pd.DataFrame({"TEST": px}) if "TEST" in symbols else pd.DataFrame()

        monkeypatch.setattr(eb, "load_close", fake_load_close)
        monkeypatch.setattr(eb, "load_close_matrix", fake_matrix)
        return px, event_locs

    def test_same_bar_execution_inflates_return(self, jump_prices):
        px, event_locs = jump_prices
        ev = pd.DataFrame({"date": px.index[event_locs]})

        leaky_sc = eb.scenario(ev, symbols="TEST", holding_days=5, entry_lag=0)
        safe_sc = eb.scenario(ev, symbols="TEST", holding_days=5, entry_lag=1)
        # ground truth the probe's claim is checked against: same-bar entry
        # bakes in the +20% jump, next-bar entry enters after it happened
        assert leaky_sc.metrics["avg_return_pct"] > 15.0
        assert abs(safe_sc.metrics["avg_return_pct"]) < 1.0

        out = lp.entry_lag_leakage(ev, symbols="TEST", holding_days=5)
        assert out["switch"] == "entry_lag"
        assert out["safe_value"] == 1 and out["leaky_value"] == 0
        assert out["leaky_metric"] > out["safe_metric"]
        assert out["inflation"] > 0

    def test_flat_series_shows_no_inflation(self, monkeypatch):
        idx = pd.bdate_range("2020-01-01", periods=200)
        px = pd.Series(100.0, index=idx, name="TEST")

        monkeypatch.setattr(eb, "load_close",
                            lambda symbol, start=None, end=None, price_table=None:
                            px.copy() if symbol == "TEST" else pd.Series(dtype=float))
        monkeypatch.setattr(eb, "load_close_matrix",
                            lambda symbols, start=None, end=None, price_table=None:
                            pd.DataFrame({"TEST": px}) if "TEST" in symbols else pd.DataFrame())

        ev = pd.DataFrame({"date": px.index[[50, 100, 150]]})
        out = lp.entry_lag_leakage(ev, symbols="TEST", holding_days=5)
        # every trade's return_pct is exactly 0 on a flat series -> zero
        # variance -> the degenerate-sd guard returns None on both sides,
        # so inflation is None, not a spurious nonzero number
        assert out["inflation"] is None
