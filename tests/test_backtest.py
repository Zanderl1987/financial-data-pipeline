"""
test_backtest.py — portfolio construction, metrics, and look-ahead safety.

The headline correctness property: a signal that knows next-period returns must
score strongly positive, and its negation must mirror it. This proves the sign
convention and the one-day weight lag are wired correctly.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import backtest as bt


class TestPureHelpers:
    def test_max_drawdown(self):
        eq = pd.Series([1.0, 1.2, 0.9, 1.1])  # peak 1.2 -> trough 0.9
        assert bt._max_drawdown(eq) == pytest.approx(0.9 / 1.2 - 1.0)

    def test_rebalance_dates_monthly(self):
        idx = pd.bdate_range("2024-01-01", "2024-03-31")
        reb = bt._rebalance_dates(idx, "M")
        assert len(reb) == 3  # Jan, Feb, Mar month-ends
        assert all(d in idx for d in reb)

    def test_target_weights_long_short_sum(self):
        dates = pd.to_datetime(["2024-01-31"])
        scores = pd.DataFrame({"A": [3.0], "B": [2.0], "C": [1.0], "D": [0.0]}, index=dates)
        w = bt._target_weights(scores, dates, quantiles=2, long_short=True)
        assert w.loc[dates[0]].sum() == pytest.approx(0.0)   # dollar-neutral
        assert w.loc[dates[0], "A"] > 0 and w.loc[dates[0], "D"] < 0

    def test_target_weights_long_only_sum(self):
        dates = pd.to_datetime(["2024-01-31"])
        scores = pd.DataFrame({"A": [3.0], "B": [2.0], "C": [1.0], "D": [0.0]}, index=dates)
        w = bt._target_weights(scores, dates, quantiles=2, long_short=False)
        assert w.loc[dates[0]].sum() == pytest.approx(1.0)
        assert (w.loc[dates[0]] >= 0).all()


def _price_backed_signal(monkeypatch, returns: pd.DataFrame, signal_long: pd.DataFrame):
    """Patch the price-return matrix so backtest() runs on synthetic prices."""
    monkeypatch.setattr(bt, "_pick_price_table", lambda *a, **k: "synthetic")
    monkeypatch.setattr(bt, "_returns_matrix", lambda *a, **k: returns)
    return signal_long


class TestLookAheadSafety:
    def setup_method(self):
        np.random.seed(0)
        dates = pd.bdate_range("2024-01-01", periods=60)
        syms = ["A", "B", "C", "D", "E", "F"]
        self.R = pd.DataFrame(np.random.normal(0, 0.02, (len(dates), len(syms))),
                              index=dates, columns=syms)

    def _to_long(self, wide):
        return (wide.reset_index().melt(id_vars="index", var_name="symbol",
                                        value_name="composite")
                    .rename(columns={"index": "date"}).dropna())

    def test_perfect_foresight_beats_anti_foresight(self, monkeypatch):
        oracle = self._to_long(self.R.shift(-1))   # tomorrow's return, known today
        anti = oracle.copy(); anti["composite"] *= -1

        monkeypatch.setattr(bt, "_pick_price_table", lambda *a, **k: "synthetic")
        monkeypatch.setattr(bt, "_returns_matrix", lambda *a, **k: self.R)
        r_pos = bt.backtest(oracle, rebalance="D", quantiles=3, long_short=True)

        monkeypatch.setattr(bt, "_returns_matrix", lambda *a, **k: self.R)
        r_neg = bt.backtest(anti, rebalance="D", quantiles=3, long_short=True)

        assert r_pos.metrics["sharpe"] > 5          # foresight is hugely profitable
        assert r_neg.metrics["sharpe"] < -5         # its negation is mirror-bad
        assert r_pos.metrics["sharpe"] == pytest.approx(-r_neg.metrics["sharpe"], rel=0.05)

    def test_metrics_keys_present(self, monkeypatch):
        sig = self._to_long(self.R.shift(-1))
        monkeypatch.setattr(bt, "_pick_price_table", lambda *a, **k: "synthetic")
        monkeypatch.setattr(bt, "_returns_matrix", lambda *a, **k: self.R)
        res = bt.backtest(sig, rebalance="W")
        for key in ("cagr_pct", "sharpe", "max_drawdown_pct", "hit_rate_pct",
                    "benchmark_cagr_pct", "n_long"):
            assert key in res.metrics


class TestValidation:
    def test_missing_score_column_raises(self):
        df = pd.DataFrame({"symbol": ["A"], "date": ["2024-01-01"]})
        with pytest.raises(ValueError):
            bt.backtest(df, score="composite")
