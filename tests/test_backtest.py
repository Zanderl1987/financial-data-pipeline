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

    def test_ann_metrics_constant_returns_no_noise_sharpe(self):
        """A constant-return series has sd ~6e-19 in float64 -- the old bare
        `vol > 0` gate reported a Sharpe near 2.4e16 instead of NaN."""
        ret = pd.Series([0.001] * 300)
        equity = (1.0 + ret).cumprod()
        m = bt._ann_metrics(ret, equity)
        assert not (isinstance(m["sharpe"], float) and abs(m["sharpe"]) > 1e12)

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


class TestExecutionCostsAndRiskControls:
    """Covers the added spread/borrow-fee/slippage cost models and the
    vol-target/max-weight/max-drawdown-stop risk controls -- including
    regression tests for two look-ahead bugs found and fixed in this module:
    the drawdown circuit breaker was zeroing the breach day's own
    already-realized return (foresight), and the vol-target scale for day t
    was computed from a rolling window that included day t's own return."""

    def _single_symbol_signal(self, dates):
        """Two symbols, A always outranks B (composite 1.0 vs 0.0) and
        quantiles=1/long_short=False picks exactly the top-1 bucket -> A's
        weight is always 1.0, B's is always 0.0, once the daily
        rebalance/shift settles. (_target_weights requires n>=2 symbols to
        form any bucket at all, so a true single-symbol universe always
        yields all-zero weights -- not usable for these tests.)"""
        n = len(dates)
        return pd.DataFrame({
            "symbol": ["A", "B"] * n,
            "date": np.repeat(dates, 2),
            "composite": [1.0, 0.0] * n,
        })

    def test_borrow_fee_bps_does_not_crash_and_increases_cost(self, monkeypatch):
        """Regression: DataFrame.applymap was removed in pandas 3.0; the
        original implementation crashed with AttributeError whenever
        borrow_fee_bps > 0 and any weight was negative (i.e. any short)."""
        dates = pd.bdate_range("2024-01-01", periods=40)
        syms = ["A", "B"]
        R = pd.DataFrame(0.001, index=dates, columns=syms)
        sig = pd.DataFrame({
            "symbol": syms * len(dates),
            "date": np.repeat(dates, 2),
            "composite": [1.0, -1.0] * len(dates),
        })
        monkeypatch.setattr(bt, "_pick_price_table", lambda *a, **k: "synthetic")
        monkeypatch.setattr(bt, "_returns_matrix", lambda *a, **k: R)

        baseline = bt.backtest(sig, rebalance="D", quantiles=2, long_short=True)
        with_fee = bt.backtest(sig, rebalance="D", quantiles=2, long_short=True,
                               borrow_fee_bps=500.0)
        assert with_fee.metrics["total_return_pct"] < baseline.metrics["total_return_pct"]

    def test_max_weight_caps_target_before_shift(self, monkeypatch):
        dates = pd.bdate_range("2024-01-01", periods=10)
        R = pd.DataFrame(0.0, index=dates, columns=["A", "B"])
        sig = self._single_symbol_signal(dates)
        monkeypatch.setattr(bt, "_pick_price_table", lambda *a, **k: "synthetic")
        monkeypatch.setattr(bt, "_returns_matrix", lambda *a, **k: R)
        res = bt.backtest(sig, rebalance="D", quantiles=2, long_short=False,
                          max_weight=0.3)
        assert (res.weights["A"].abs() <= 0.3 + 1e-9).all()

    def test_drawdown_circuit_breaker_preserves_breach_day_return(self, monkeypatch):
        dates = pd.bdate_range("2024-01-01", periods=20)
        R = pd.DataFrame(0.0, index=dates, columns=["A", "B"])
        R.iloc[5, 0] = -0.30   # single sharp drop well past a 20% stop
        sig = self._single_symbol_signal(dates)
        monkeypatch.setattr(bt, "_pick_price_table", lambda *a, **k: "synthetic")
        monkeypatch.setattr(bt, "_returns_matrix", lambda *a, **k: R)

        res = bt.backtest(sig, rebalance="D", quantiles=2, long_short=False,
                          max_drawdown_stop=0.20)

        # The breach day's own loss must stand -- it was already realized by
        # the close of that day, before the breach could be detected.
        assert res.returns.iloc[5] == pytest.approx(-0.30, abs=1e-6)
        # Every day AFTER the breach is flattened.
        assert (res.returns.iloc[6:] == 0.0).all()
        assert (res.weights.iloc[6:]["A"] == 0.0).all()

    def test_vol_target_scale_excludes_same_day_return(self, monkeypatch):
        """A single huge one-day move must not change that same day's own
        position size -- vol targeting can only react to it starting the
        next day, once the rolling estimate has actually seen it."""
        dates = pd.bdate_range("2024-01-01", periods=40)
        R = pd.DataFrame(0.001, index=dates, columns=["A", "B"])
        R.iloc[10, 0] = 0.50   # one huge outlier day
        sig = self._single_symbol_signal(dates)
        monkeypatch.setattr(bt, "_pick_price_table", lambda *a, **k: "synthetic")
        monkeypatch.setattr(bt, "_returns_matrix", lambda *a, **k: R)

        res = bt.backtest(sig, rebalance="D", quantiles=2, long_short=False,
                          vol_target=0.10)
        w = res.weights["A"]

        # The scale on the outlier day itself is computed from the flat,
        # low-vol pre-outlier window (same shape as the day before) -- it
        # must not already reflect the outlier it's coincident with.
        assert w.iloc[10] == pytest.approx(w.iloc[9], rel=0.05)
        # The day AFTER, the rolling window has now seen the outlier and the
        # vol estimate jumps, sharply scaling the position down.
        assert w.iloc[11] < w.iloc[10] * 0.5


class TestAdvParticipationCost:
    """adv_participation_coeff -- the same per-symbol ADV market-impact model
    event_backtest.scenario() already uses, reused here (not reinvented) for
    the weight-matrix engine. Deliberately a DIFFERENT parameter from the
    existing portfolio-turnover-based adv_impact_coeff (see
    _adv_participation_cost's docstring)."""

    def _oscillating_signal(self, dates):
        """A and B swap the single top-quantile slot every day -> 100%
        turnover on every rebalance, a large and easy-to-detect ADV cost."""
        rows = []
        for i, d in enumerate(dates):
            a = 1.0 if i % 2 == 0 else 0.0
            rows.append({"symbol": "A", "date": d, "composite": a})
            rows.append({"symbol": "B", "date": d, "composite": 1.0 - a})
        return pd.DataFrame(rows)

    def _setup(self, monkeypatch, n=40):
        dates = pd.bdate_range("2024-01-01", periods=n)
        R = pd.DataFrame(0.001, index=dates, columns=["A", "B"])
        sig = self._oscillating_signal(dates)
        monkeypatch.setattr(bt, "_pick_price_table", lambda *a, **k: "synthetic")
        monkeypatch.setattr(bt, "_returns_matrix", lambda *a, **k: R)
        return dates, sig

    def test_off_by_default(self, monkeypatch):
        dates, sig = self._setup(monkeypatch)
        baseline = bt.backtest(sig, rebalance="D", quantiles=2, long_short=False)
        explicit_none = bt.backtest(sig, rebalance="D", quantiles=2,
                                    long_short=False, adv_participation_coeff=None)
        assert list(baseline.returns) == list(explicit_none.returns)
        assert baseline.params["aum"] is None
        assert baseline.params["adv_window"] is None

    def test_increases_cost_and_degrades_return(self, monkeypatch):
        dates, sig = self._setup(monkeypatch)
        import event_backtest as eb
        thin = pd.DataFrame({"A": 1000.0, "B": 1000.0}, index=dates)
        monkeypatch.setattr(eb, "load_dollar_volume_matrix",
                            lambda syms, start=None, end=None, price_table=None: thin)

        baseline = bt.backtest(sig, rebalance="D", quantiles=2, long_short=False)
        with_adv = bt.backtest(sig, rebalance="D", quantiles=2, long_short=False,
                               adv_participation_coeff=50.0, aum=1_000_000.0,
                               adv_window=5)
        assert with_adv.metrics["total_return_pct"] < baseline.metrics["total_return_pct"]

    def test_missing_volume_data_degrades_to_no_extra_cost(self, monkeypatch):
        dates, sig = self._setup(monkeypatch)
        import event_backtest as eb
        monkeypatch.setattr(eb, "load_dollar_volume_matrix",
                            lambda syms, start=None, end=None, price_table=None:
                            pd.DataFrame())

        baseline = bt.backtest(sig, rebalance="D", quantiles=2, long_short=False)
        with_adv = bt.backtest(sig, rebalance="D", quantiles=2, long_short=False,
                               adv_participation_coeff=50.0)
        assert list(baseline.returns) == list(with_adv.returns)

    def test_higher_coefficient_costs_more(self, monkeypatch):
        dates, sig = self._setup(monkeypatch)
        import event_backtest as eb
        thin = pd.DataFrame({"A": 1000.0, "B": 1000.0}, index=dates)
        monkeypatch.setattr(eb, "load_dollar_volume_matrix",
                            lambda syms, start=None, end=None, price_table=None: thin)

        low = bt.backtest(sig, rebalance="D", quantiles=2, long_short=False,
                          adv_participation_coeff=10.0, adv_window=5)
        high = bt.backtest(sig, rebalance="D", quantiles=2, long_short=False,
                           adv_participation_coeff=100.0, adv_window=5)
        assert high.metrics["total_return_pct"] < low.metrics["total_return_pct"]

    def test_params_recorded_only_when_enabled(self, monkeypatch):
        dates, sig = self._setup(monkeypatch)
        import event_backtest as eb
        thin = pd.DataFrame({"A": 1000.0, "B": 1000.0}, index=dates)
        monkeypatch.setattr(eb, "load_dollar_volume_matrix",
                            lambda syms, start=None, end=None, price_table=None: thin)

        res = bt.backtest(sig, rebalance="D", quantiles=2, long_short=False,
                          adv_participation_coeff=25.0, aum=500_000.0, adv_window=10)
        assert res.params["adv_participation_coeff"] == 25.0
        assert res.params["aum"] == 500_000.0
        assert res.params["adv_window"] == 10
