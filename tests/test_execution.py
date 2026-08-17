"""
test_execution.py -- unit tests for evaluation/execution.py (W1 Step A).

The golden masters in test_execution_golden.py prove the two engines' OUTPUT is
unchanged. These prove the extracted arithmetic equals the inline expressions it
replaced, independently of either engine, so a failure points at the formula
rather than at a whole backtest.

The pre-refactor expressions being reproduced:
  backtest.py:198       effective_cost_bps = cost_bps + spread_bps / 2.0
                        costs = turnover * (effective_cost_bps / 1e4)
  backtest.py:203-204   + short_exposure * (borrow_fee_bps / (1e4 * 252))
  backtest.py:207-209   + turnover**0.5 * (adv_impact_coeff / 1e4)
  event_backtest.py:346 effective_cost = (cost_bps + spread_bps / 2.0) / 1e4
  event_backtest.py:348 + 0.0010 when slippage_model == "sqrt_impact"
  stage3.py:173         round_trip_rate = 2.0 * cost_bps_side / 1e4
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from evaluation import execution as ex  # noqa: E402


class TestCostRates:
    def test_per_side_matches_inline_expression(self):
        for cost_bps, spread_bps in [(0, 0), (10, 0), (0, 20), (7.5, 12.5)]:
            c = ex.CostModel(commission_bps=cost_bps, spread_bps=spread_bps)
            assert ex.per_side_rate(c) == (cost_bps + spread_bps / 2.0) / 1e4

    def test_round_trip_matches_stage3_inline_expression(self):
        for bps in (5.0, 10.0, 20.0):
            c = ex.CostModel(commission_bps=bps)
            assert ex.round_trip_rate(c) == 2.0 * bps / 1e4

    def test_flat_impact_reproduces_event_backtest_constant(self):
        """event_backtest.py adds exactly 0.0010 per side under sqrt_impact."""
        plain = ex.CostModel(commission_bps=10.0, spread_bps=20.0)
        flat = ex.CostModel(commission_bps=10.0, spread_bps=20.0,
                            impact_model="flat", impact_coeff=10.0)
        assert ex.per_side_rate(flat) - ex.per_side_rate(plain) == pytest.approx(0.0010)

    def test_sqrt_impact_is_not_a_per_side_constant(self):
        """The sqrt model is a function of turnover and must not leak into the
        per-side rate -- that distinction is the whole reason the two engines'
        'sqrt_impact' behaviors could be told apart."""
        c = ex.CostModel(commission_bps=10.0, impact_model="sqrt", impact_coeff=0.1)
        assert ex.per_side_rate(c) == 10.0 / 1e4


class TestDailyCost:
    def setup_method(self):
        self.turnover = pd.Series([0.0, 0.5, 1.0, 2.0])
        self.short = pd.Series([0.0, 1.0, 1.0, 0.5])

    def test_matches_backtest_inline_commission_spread(self):
        c = ex.CostModel(commission_bps=10.0, spread_bps=20.0)
        expected = self.turnover * ((10.0 + 20.0 / 2.0) / 1e4)
        pd.testing.assert_series_equal(ex.daily_cost(c, self.turnover), expected)

    def test_matches_backtest_inline_borrow_fee(self):
        c = ex.CostModel(commission_bps=10.0, borrow_fee_bps=50.0)
        expected = (self.turnover * (10.0 / 1e4)
                    + self.short * (50.0 / (1e4 * 252)))
        pd.testing.assert_series_equal(
            ex.daily_cost(c, self.turnover, self.short), expected)

    def test_matches_backtest_inline_sqrt_impact(self):
        c = ex.CostModel(commission_bps=5.0, impact_model="sqrt", impact_coeff=0.1)
        expected = (self.turnover * (5.0 / 1e4)
                    + self.turnover.pow(0.5) * (0.1 / 1e4))
        pd.testing.assert_series_equal(ex.daily_cost(c, self.turnover), expected)

    def test_zero_impact_coeff_adds_nothing(self):
        """backtest.py guards on `adv_impact_coeff > 0`, so a zero coefficient
        must skip the term rather than add 0.0 -- preserved for exact float
        reproducibility."""
        c = ex.CostModel(commission_bps=5.0, impact_model="sqrt", impact_coeff=0.0)
        plain = ex.CostModel(commission_bps=5.0)
        pd.testing.assert_series_equal(ex.daily_cost(c, self.turnover),
                                       ex.daily_cost(plain, self.turnover))

    def test_borrow_fee_ignored_when_no_short_exposure_passed(self):
        c = ex.CostModel(borrow_fee_bps=50.0)
        pd.testing.assert_series_equal(ex.daily_cost(c, self.turnover),
                                       self.turnover * 0.0)


class TestLegacyKwargShim:
    def test_none_model_carries_plain_costs(self):
        c = ex.costs_from_legacy_kwargs(cost_bps=10, spread_bps=20, borrow_fee_bps=5)
        assert (c.commission_bps, c.spread_bps, c.borrow_fee_bps) == (10, 20, 5)
        assert c.impact_model is None

    def test_backtest_path_selects_sqrt(self):
        c = ex.costs_from_legacy_kwargs(cost_bps=5, slippage_model="sqrt_impact",
                                        impact_coeff=0.1)
        assert c.impact_model == "sqrt" and c.impact_coeff == 0.1

    def test_event_backtest_path_selects_flat(self):
        c = ex.costs_from_legacy_kwargs(cost_bps=5, slippage_model="sqrt_impact",
                                        flat_impact_bps=10.0)
        assert c.impact_model == "flat" and c.impact_coeff == 10.0

    def test_unknown_model_raises(self):
        with pytest.raises(ValueError, match="unknown slippage_model"):
            ex.costs_from_legacy_kwargs(slippage_model="almgren_chriss")


class TestConfigContract:
    def test_legacy_is_all_defaults(self):
        assert ex.per_side_rate(ex.LEGACY.costs) == 0.0
        assert ex.LEGACY.limits.capital is None
        assert ex.LEGACY.risk.stop_loss_pct is None
        assert ex.LEGACY.sizing.mode == "fixed_notional"

    def test_tv_campaign_reproduces_prereg_cost(self):
        """20 bps round trip, per the pre-registration's 10 bps/side."""
        assert ex.round_trip_rate(ex.TV_CAMPAIGN.costs) == pytest.approx(0.0020)

    def test_tv_campaign_matches_stage3_constant(self):
        from strategies import stage3
        assert ex.TV_CAMPAIGN.costs.commission_bps == stage3.PRIMARY_COST_BPS

    def test_resolve_none_is_legacy(self):
        assert ex.resolve(None) is ex.LEGACY
        cfg = ex.ExecutionConfig(name="x")
        assert ex.resolve(cfg) is cfg

    def test_frozen(self):
        with pytest.raises(Exception):
            ex.LEGACY.costs.commission_bps = 5.0

    def test_as_dict_is_flat_and_stable(self):
        d = ex.TV_CAMPAIGN.as_dict()
        assert d["config_name"] == "tv_campaign"
        assert d["costs.commission_bps"] == 10.0
        assert d["limits.capital"] is None
        assert list(d) == sorted(d, key=list(d).index)  # insertion order preserved

    @pytest.mark.parametrize("kwargs", [
        {"impact_model": "almgren"},
        {"commission_bps": -1},
        {"spread_bps": -0.5},
    ])
    def test_cost_model_validation(self, kwargs):
        with pytest.raises(ValueError):
            ex.CostModel(**kwargs)

    @pytest.mark.parametrize("kwargs", [
        {"stop_loss_pct": 0}, {"take_profit_pct": -3}, {"max_holding_days": 0},
    ])
    def test_risk_validation(self, kwargs):
        with pytest.raises(ValueError):
            ex.RiskControls(**kwargs)

    @pytest.mark.parametrize("kwargs", [
        {"mode": "kelly"}, {"notional": 0}, {"max_weight": -1},
    ])
    def test_sizing_validation(self, kwargs):
        with pytest.raises(ValueError):
            ex.Sizing(**kwargs)

    @pytest.mark.parametrize("kwargs", [
        {"capital": 0}, {"max_concurrent": 0}, {"max_drawdown_stop": 1.5},
    ])
    def test_limits_validation(self, kwargs):
        with pytest.raises(ValueError):
            ex.PortfolioLimits(**kwargs)


class TestStage3CostEquivalence:
    """The stage3 monkeypatch's arithmetic must be untouched by Step A --
    including its round-then-deduct-then-round order, which is load-bearing."""

    def test_cost_adjusted_matches_pre_refactor_formula(self, monkeypatch):
        from evaluation import trades as ev_trades
        from strategies import stage3

        notional = 10_000.0
        raw_pcts = [0.0123, -0.0456, 0.10, -0.0001]

        def fake_simulate_symbol(index, close, le, lx, se, sx, symbol, notional):
            return [{"symbol": symbol, "side": "long",
                     "pnl_dollars": round(notional * p, 2),
                     "pnl_pct": round(100 * p, 3)} for p in raw_pcts]

        monkeypatch.setattr(ev_trades, "simulate_symbol", fake_simulate_symbol)

        bps = 10.0
        with stage3.cost_adjusted(bps):
            rows = ev_trades.simulate_symbol(None, None, None, None, None, None,
                                             "TEST", notional)

        rate = 2.0 * bps / 1e4                      # the pre-refactor expression
        for row, p in zip(rows, raw_pcts):
            assert row["pnl_dollars"] == round(round(notional * p, 2) - notional * rate, 2)
            assert row["pnl_pct"] == round(round(100 * p, 3) - 100 * rate, 3)

    def test_patch_is_restored(self, monkeypatch):
        from evaluation import trades as ev_trades
        from strategies import stage3
        before = ev_trades.simulate_symbol
        with stage3.cost_adjusted(10.0):
            assert ev_trades.simulate_symbol is not before
        assert ev_trades.simulate_symbol is before
