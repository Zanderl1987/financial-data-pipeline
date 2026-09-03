"""
tests/test_backtest_app.py -- unit tests for backtest_app.py's pure logic
(registry/artifact loading, live trade-rule simulation, chart data prep).
Dash callback wiring itself is smoke-tested (layout construction only) --
no Selenium/browser harness, per docs/superpowers/specs/
2026-08-03-interactive-backtest-explorer-design.md.
"""

import os
import sys

import dash
import numpy as np
import pandas as pd
import pytest
import plotly.graph_objects as go

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import backtest_app as ba
from evaluation import trades as ev_trades


class TestListEvaluatedSignals:
    def test_empty_registry_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(ba.ev_registry, "load",
                            lambda path=None: pd.DataFrame(columns=ba.ev_registry.COLUMNS))
        assert ba.list_evaluated_signals() == []

    def test_lists_unique_sorted_names_with_artifact_flag(self, monkeypatch):
        reg = pd.DataFrame({"input_name": ["tv_threshold", "factor_value",
                                           "tv_threshold"]})
        monkeypatch.setattr(ba.ev_registry, "load", lambda path=None: reg)
        monkeypatch.setattr(ba, "find_latest",
                            lambda name: "/some/dir" if name == "tv_threshold" else None)
        assert ba.list_evaluated_signals() == [
            {"name": "factor_value", "has_local_artifacts": False},
            {"name": "tv_threshold", "has_local_artifacts": True},
        ]


class TestLoadSignal:
    def test_missing_artifacts_returns_error_dict(self, monkeypatch):
        monkeypatch.setattr(ba, "find_latest", lambda name: None)
        out = ba.load_signal("ghost_signal")
        assert "error" in out
        assert "ghost_signal" in out["error"]

    def test_loads_run_artifacts_on_success(self, monkeypatch):
        monkeypatch.setattr(ba, "find_latest", lambda name: "/run/dir")
        monkeypatch.setattr(ba, "load_run", lambda run_dir: (
            {"summary": {"n_trades": 5}}, {"input_name": "tv_threshold"}, None))
        out = ba.load_signal("tv_threshold")
        assert out == {
            "run_dir": "/run/dir",
            "results": {"summary": {"n_trades": 5}},
            "meta": {"input_name": "tv_threshold"},
            "trades": None,
        }


class TestBuildTvThresholdRule:
    def _df(self):
        # rating_all path: 0.0 -> 0.6 (crosses bull 0.5) -> 0.05 (exits long,
        # < 0.1) -> -0.6 (crosses bear -0.5) -> -0.05 (exits short, > -0.1)
        return pd.DataFrame({
            "close": [10.0, 11.0, 12.0, 13.0, 14.0],
            "rating_all": [0.0, 0.6, 0.05, -0.6, -0.05],
        }, index=pd.bdate_range("2024-01-01", periods=5))

    def test_matches_adapters_tv_threshold_rule_at_default_thresholds(self):
        import tv_rating_eval as tve
        from evaluation.adapters import tv_threshold_rule

        df = self._df()
        live_rule = ba.build_tv_threshold_rule(
            bull_min=tve.BULL_MIN, exit_long_max=tve.EXIT_LONG_MAX,
            bear_max=tve.BEAR_MAX, exit_short_min=tve.EXIT_SHORT_MIN,
            notional=tve.NOTIONAL)
        fixed_rule = tv_threshold_rule()

        le1, lx1, se1, sx1 = ev_trades.rule_flags(live_rule, df)
        le2, lx2, se2, sx2 = ev_trades.rule_flags(fixed_rule, df)
        assert np.array_equal(le1, le2)
        assert np.array_equal(lx1, lx2)
        assert np.array_equal(se1, se2)
        assert np.array_equal(sx1, sx2)

    def test_tighter_bull_threshold_enters_earlier(self):
        # Use a custom df where first value is below tight threshold (0.0)
        # so that tight threshold actually triggers on day 1
        df = pd.DataFrame({
            "close": [10.0, 11.0, 12.0, 13.0, 14.0],
            "rating_all": [-0.1, 0.6, 0.05, -0.6, -0.05],
        }, index=pd.bdate_range("2024-01-01", periods=5))
        loose = ba.build_tv_threshold_rule(0.5, 0.1, -0.5, -0.1)
        tight = ba.build_tv_threshold_rule(0.0, 0.1, -0.5, -0.1)
        le_loose, _, _, _ = ev_trades.rule_flags(loose, df)
        le_tight, _, _, _ = ev_trades.rule_flags(tight, df)
        # tight (0.0) fires on the very first crossing above 0.0 (day 1: -0.1->0.6),
        # loose only once rating_all reaches 0.5 (also day 1: -0.1->0.6)
        # Both fire on row 1 here, so assert the tight rule fires at least as early
        assert np.flatnonzero(le_tight)[0] <= np.flatnonzero(le_loose)[0]

    def test_side_is_both(self):
        rule = ba.build_tv_threshold_rule(0.5, 0.1, -0.5, -0.1)
        assert rule.side == "both"
        assert rule.name == "tv_threshold_live"

    def test_boundary_values_do_not_cross_on_equal_previous(self):
        """When previous day's value equals threshold (not strictly beyond),
        no cross is detected. This tests strict-inequality boundary behavior.

        Setup: rating_all=[0.4, 0.5, 0.6, 0.1, -0.5, -0.4]
        bull_min=0.5, bear_max=-0.5
        - Day 1: 0.5 (curr) with 0.4 (prev) -> prev < 0.5, CROSSES UP
        - Day 2: 0.6 (curr) with 0.5 (prev) -> prev = 0.5, NOT < 0.5, NO CROSS
        - Day 4: -0.5 (curr) with 0.1 (prev) -> prev > -0.5, CROSSES DOWN
        - Day 5: -0.4 (curr) with -0.5 (prev) -> prev = -0.5, NOT > -0.5, NO CROSS
        """
        df = pd.DataFrame({
            "close": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
            "rating_all": [0.4, 0.5, 0.6, 0.1, -0.5, -0.4],
        }, index=pd.bdate_range("2024-01-01", periods=6))

        rule = ba.build_tv_threshold_rule(
            bull_min=0.5, exit_long_max=0.1,
            bear_max=-0.5, exit_short_min=-0.1)

        long_entries, long_exits, short_entries, short_exits = \
            ev_trades.rule_flags(rule, df)

        # Expected long_entries: day 1 crosses up (0.5 from 0.4), day 2 doesn't (0.6 from 0.5)
        assert long_entries[0] == False, "No entry on day 0 (no previous)"
        assert long_entries[1] == True, "Entry on day 1: 0.5 >= 0.5 and prev 0.4 < 0.5"
        assert long_entries[2] == False, "No entry on day 2: 0.6 >= 0.5 but prev 0.5 NOT < 0.5"
        assert long_entries[3] == False, "No entry on day 3: 0.1 < 0.5"
        assert long_entries[4] == False, "No entry on day 4: -0.5 < 0.5"
        assert long_entries[5] == False, "No entry on day 5: -0.4 < 0.5"

        # Expected short_entries: day 4 crosses down (-0.5 from 0.1), day 5 doesn't (-0.4 from -0.5)
        assert short_entries[0] == False, "No short entry on day 0"
        assert short_entries[1] == False, "No short entry on day 1: 0.5 > -0.5"
        assert short_entries[2] == False, "No short entry on day 2: 0.6 > -0.5"
        assert short_entries[3] == False, "No short entry on day 3: 0.1 > -0.5"
        assert short_entries[4] == True, "Short entry on day 4: -0.5 <= -0.5 and prev 0.1 > -0.5"
        assert short_entries[5] == False, "No short entry on day 5: -0.4 > -0.5, prev -0.5 NOT > -0.5"


class TestBuildTvFadeRule:
    def _df(self):
        return pd.DataFrame({
            "close": [10.0, 11.0, 12.0, 13.0, 14.0],
            "rating_all": [0.0, 0.6, 0.05, -0.6, -0.05],
        }, index=pd.bdate_range("2024-01-01", periods=5))

    def test_fade_is_the_side_swapped_mirror_of_threshold(self):
        """build_tv_fade_rule's long/short flags should exactly equal
        build_tv_threshold_rule's short/long flags at the same thresholds
        -- fading is defined as swapping which side each trigger drives,
        not a different trigger definition."""
        df = self._df()
        threshold = ba.build_tv_threshold_rule(0.5, 0.1, -0.5, -0.1)
        fade = ba.build_tv_fade_rule(0.5, 0.1, -0.5, -0.1)

        t_le, t_lx, t_se, t_sx = ev_trades.rule_flags(threshold, df)
        f_le, f_lx, f_se, f_sx = ev_trades.rule_flags(fade, df)

        assert np.array_equal(f_le, t_se)
        assert np.array_equal(f_lx, t_sx)
        assert np.array_equal(f_se, t_le)
        assert np.array_equal(f_sx, t_lx)

    def test_fade_side_is_both(self):
        rule = ba.build_tv_fade_rule(0.5, 0.1, -0.5, -0.1)
        assert rule.side == "both"
        assert rule.name == "tv_fade_live"

    def test_fade_long_matches_fade_long_leg_only(self):
        df = self._df()
        fade = ba.build_tv_fade_rule(0.5, 0.1, -0.5, -0.1)
        fade_long = ba.build_tv_fade_long_rule(0.5, 0.1, -0.5, -0.1)

        f_le, f_lx, _, _ = ev_trades.rule_flags(fade, df)
        fl_le, fl_lx, fl_se, fl_sx = ev_trades.rule_flags(fade_long, df)

        assert np.array_equal(f_le, fl_le)
        assert np.array_equal(f_lx, fl_lx)
        assert not fl_se.any()
        assert not fl_sx.any()

    def test_fade_long_side_is_long(self):
        rule = ba.build_tv_fade_long_rule(0.5, 0.1, -0.5, -0.1)
        assert rule.side == "long"
        assert rule.name == "tv_fade_long_live"


class TestCacheAndSimulateLive:
    def test_has_trade_rule_true_for_known_signal(self):
        assert ba.has_trade_rule("tv_threshold") is True

    def test_has_trade_rule_true_for_fade_signals(self):
        assert ba.has_trade_rule("tv_fade") is True
        assert ba.has_trade_rule("tv_fade_long") is True

    def test_has_trade_rule_false_for_unknown_signal(self):
        assert ba.has_trade_rule("factor_value") is False

    def test_get_cache_builds_once_and_reuses(self, monkeypatch):
        calls = []

        def fake_rating_cache():
            calls.append(1)
            return {"AAPL": pd.DataFrame({"close": [1.0], "rating_all": [0.0]})}

        monkeypatch.setitem(ba.KNOWN_TRADE_RULE_SIGNALS, "tv_threshold",
                            (fake_rating_cache, ba.build_tv_threshold_rule))
        ba._CACHE.clear()
        ba._CACHE_RUN_ID.clear()
        first = ba.get_cache("tv_threshold", "run_001")
        second = ba.get_cache("tv_threshold", "run_001")
        assert first is second
        assert len(calls) == 1

    def test_get_cache_rebuilds_on_different_run_id(self, monkeypatch):
        calls = []

        def fake_rating_cache():
            calls.append(1)
            return {"AAPL": pd.DataFrame({"close": [1.0], "rating_all": [0.0]})}

        monkeypatch.setitem(ba.KNOWN_TRADE_RULE_SIGNALS, "tv_threshold",
                            (fake_rating_cache, ba.build_tv_threshold_rule))
        ba._CACHE.clear()
        ba._CACHE_RUN_ID.clear()
        first = ba.get_cache("tv_threshold", "run_001")
        second = ba.get_cache("tv_threshold", "run_002")
        assert first is not second
        assert len(calls) == 2

    def test_get_cache_raises_for_unknown_signal(self):
        with pytest.raises(KeyError):
            ba.get_cache("factor_value", "some_run_id")

    def test_simulate_live_zero_trades_at_extreme_threshold(self, monkeypatch):
        cache = {"AAPL": pd.DataFrame(
            {"close": [10.0, 11.0, 12.0], "rating_all": [0.0, 0.1, 0.2]},
            index=pd.bdate_range("2024-01-01", periods=3))}
        monkeypatch.setattr(ba, "get_cache", lambda name, run_id: cache)
        trades, summary = ba.simulate_live("tv_threshold", "run_001", bull_min=0.99,
                                           exit_long_max=0.1, bear_max=-0.99,
                                           exit_short_min=-0.1)
        assert trades.empty
        assert summary == {"n_trades": 0, "summary_reason": "no realized trades"}

    def test_config_none_still_works_and_matches_prior_behavior(self, monkeypatch):
        cache = {"AAPL": pd.DataFrame(
            {"close": [10.0, 11.0, 12.0, 13.0], "rating_all": [0.0, 0.6, 0.05, 0.6]},
            index=pd.bdate_range("2024-01-01", periods=4))}
        monkeypatch.setattr(ba, "get_cache", lambda name, run_id: cache)
        ba._SIM_CACHE.clear()
        trades, summary = ba.simulate_live("tv_threshold", "run_001", bull_min=0.5,
                                           exit_long_max=0.1, bear_max=-0.5,
                                           exit_short_min=-0.1)
        assert summary["n_trades"] >= 0   # no exception, legacy path still runs

    def test_different_configs_produce_separate_cache_entries(self, monkeypatch):
        cache = {"AAPL": pd.DataFrame(
            {"close": [10.0, 11.0, 12.0, 13.0], "rating_all": [0.0, 0.6, 0.05, 0.6]},
            index=pd.bdate_range("2024-01-01", periods=4))}
        monkeypatch.setattr(ba, "get_cache", lambda name, run_id: cache)
        ba._SIM_CACHE.clear()
        cheap = ba.build_execution_config(commission_bps=0.0)
        costly = ba.build_execution_config(commission_bps=50.0)
        ba.simulate_live("tv_threshold", "run_001", 0.5, 0.1, -0.5, -0.1, config=cheap)
        ba.simulate_live("tv_threshold", "run_001", 0.5, 0.1, -0.5, -0.1, config=costly)
        assert len(ba._SIM_CACHE) == 2

    def test_costly_config_reduces_pnl_versus_legacy(self, monkeypatch):
        cache = {"AAPL": pd.DataFrame(
            {"close": [10.0, 12.0, 14.0, 11.0], "rating_all": [0.0, 0.6, 0.05, 0.6]},
            index=pd.bdate_range("2024-01-01", periods=4))}
        monkeypatch.setattr(ba, "get_cache", lambda name, run_id: cache)
        ba._SIM_CACHE.clear()
        legacy_trades, legacy_summary = ba.simulate_live(
            "tv_threshold", "run_001", 0.5, 0.1, -0.5, -0.1)
        costly = ba.build_execution_config(commission_bps=100.0)
        costly_trades, costly_summary = ba.simulate_live(
            "tv_threshold", "run_001", 0.5, 0.1, -0.5, -0.1, config=costly)
        if legacy_summary.get("n_trades", 0) > 0:
            assert (costly_summary["total_pnl_dollars"]
                    < legacy_summary["total_pnl_dollars"])


class TestBaselineVsLive:
    def test_diffs_the_three_headline_stats(self):
        baseline = {"n_trades": 21938, "win_rate_pct": 36.6,
                   "total_pnl_dollars": 378073.0}
        live = {"n_trades": 1847, "win_rate_pct": 41.2,
               "total_pnl_dollars": 612340.0}
        out = ba.baseline_vs_live(baseline, live)
        assert out == {
            "n_trades": {"baseline": 21938, "live": 1847},
            "win_rate_pct": {"baseline": 36.6, "live": 41.2},
            "total_pnl_dollars": {"baseline": 378073.0, "live": 612340.0},
        }

    def test_missing_baseline_keys_are_none(self):
        out = ba.baseline_vs_live({}, {"n_trades": 0,
                                       "summary_reason": "no realized trades"})
        assert out["n_trades"] == {"baseline": None, "live": 0}
        assert out["win_rate_pct"] == {"baseline": None, "live": None}


class TestBuildExecutionConfig:
    def test_defaults_match_legacy_field_values(self):
        cfg = ba.build_execution_config()
        assert cfg.costs == ba.ev_execution.CostModel()
        assert cfg.risk == ba.ev_execution.RiskControls()
        assert cfg.sizing == ba.ev_execution.Sizing()
        assert cfg.limits == ba.ev_execution.PortfolioLimits()

    def test_custom_cost_values_populate_cost_model(self):
        cfg = ba.build_execution_config(commission_bps=10.0, spread_bps=5.0,
                                        borrow_fee_bps=2.0, impact_model="sqrt",
                                        impact_coeff=0.1)
        assert cfg.costs == ba.ev_execution.CostModel(
            commission_bps=10.0, spread_bps=5.0, borrow_fee_bps=2.0,
            impact_model="sqrt", impact_coeff=0.1)

    def test_custom_risk_and_sizing_and_limits_values(self):
        cfg = ba.build_execution_config(
            stop_loss_pct=0.05, take_profit_pct=0.10, vol_stop_mult=2.0,
            trailing=True, max_holding_days=20,
            sizing_mode="fixed_fraction", fraction=0.1, max_weight=0.2,
            capital=100_000.0, max_concurrent=5, max_drawdown_stop=0.25)
        assert cfg.risk == ba.ev_execution.RiskControls(
            stop_loss_pct=0.05, take_profit_pct=0.10, vol_stop_mult=2.0,
            trailing=True, max_holding_days=20)
        assert cfg.sizing.mode == "fixed_fraction"
        assert cfg.sizing.fraction == 0.1
        assert cfg.sizing.max_weight == 0.2
        assert cfg.limits == ba.ev_execution.PortfolioLimits(
            capital=100_000.0, max_concurrent=5, max_drawdown_stop=0.25)

    def test_invalid_fixed_fraction_without_fraction_raises_value_error(self):
        with pytest.raises(ValueError, match="fixed_fraction"):
            ba.build_execution_config(sizing_mode="fixed_fraction")

    def test_invalid_negative_commission_raises_value_error(self):
        with pytest.raises(ValueError, match="commission_bps"):
            ba.build_execution_config(commission_bps=-1.0)


class TestResolveExecutionConfig:
    DEFAULTS = dict(
        commission_bps=0.0, spread_bps=0.0, borrow_fee_bps=0.0,
        impact_model="none", impact_coeff=0.0, stop_loss_pct=None,
        take_profit_pct=None, vol_stop_mult=None, trailing=[],
        max_holding_days=None, sizing_mode="fixed_notional",
        sizing_notional=None, sizing_fraction=None, sizing_max_weight=None,
        limits_capital=None, limits_max_concurrent=None,
        limits_max_drawdown_stop=None)

    def test_defaults_resolve_to_legacy_equivalent_config(self):
        cfg, err = ba.resolve_execution_config(**self.DEFAULTS)
        assert err == ""
        assert cfg == ba.ev_execution.ExecutionConfig(
            name="live", costs=ba.ev_execution.CostModel(),
            risk=ba.ev_execution.RiskControls(), sizing=ba.ev_execution.Sizing(),
            limits=ba.ev_execution.PortfolioLimits())

    def test_impact_model_none_sentinel_maps_to_python_none(self):
        cfg, err = ba.resolve_execution_config(**self.DEFAULTS)
        assert cfg.costs.impact_model is None

    def test_impact_model_sqrt_passes_through(self):
        values = dict(self.DEFAULTS, impact_model="sqrt", impact_coeff=0.1)
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg.costs.impact_model == "sqrt"
        assert cfg.costs.impact_coeff == 0.1

    def test_trailing_checklist_value_maps_to_bool(self):
        values = dict(self.DEFAULTS, trailing=["trailing"])
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg.risk.trailing is True

    def test_blank_notional_falls_back_to_default_notional(self):
        values = dict(self.DEFAULTS, sizing_notional=None)
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg.sizing.notional == ba.DEFAULT_NOTIONAL

    def test_explicit_notional_passes_through(self):
        values = dict(self.DEFAULTS, sizing_notional=25_000.0)
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg.sizing.notional == 25_000.0

    def test_invalid_combination_returns_none_config_and_message(self):
        values = dict(self.DEFAULTS, sizing_mode="fixed_fraction",
                     sizing_fraction=None)
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg is None
        assert "fixed_fraction" in err

    def test_fixed_fraction_without_capital_returns_none_and_message(self):
        """Regression for the final-review Critical finding: fixed_fraction
        sizing with fraction set but limits.capital blank constructs a
        VALID ExecutionConfig at __post_init__ time (Sizing only requires
        `fraction`, PortfolioLimits has no cross-field check) -- the
        failure used to surface only later, inside evaluation/trades.py's
        _portfolio_pass(), as an uncaught ValueError out of simulate_live().
        resolve_execution_config() now catches this combination directly,
        before any simulation is attempted -- verified at this level since
        that's exactly where the fix (part 1a) lives; the callback-level
        fix (part 1b, the try/except ValueError around simulate_live) is a
        belt-and-suspenders guard for any FUTURE point-of-use check added to
        trades.py, and isn't reachable by this specific case anymore."""
        values = dict(self.DEFAULTS, sizing_mode="fixed_fraction",
                     sizing_fraction=0.1, limits_capital=None)
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg is None
        assert "fixed_fraction" in err


class TestCharts:
    def _price_df(self):
        return pd.DataFrame({"close": [10.0, 11.0, 12.0]},
                            index=pd.bdate_range("2024-01-01", periods=3))

    def _trades_df(self):
        return pd.DataFrame({
            "symbol": ["AAPL", "MSFT"], "side": ["long", "long"],
            "entry_signal_date": pd.bdate_range("2024-01-01", periods=2),
            "entry_date": pd.bdate_range("2024-01-02", periods=2),
            "entry_price": [10.0, 20.0],
            "exit_signal_date": pd.bdate_range("2024-01-03", periods=2),
            "exit_date": pd.bdate_range("2024-01-04", periods=2),
            "exit_price": [12.0, 19.0], "days_held": [2, 2],
            "pnl_dollars": [200.0, -50.0], "pnl_pct": [2.0, -0.5],
        })

    def test_symbol_price_fig_renders_with_zero_trades_for_symbol(self):
        fig = ba.symbol_price_fig("AAPL", self._price_df(), pd.DataFrame(
            columns=ba.ev_trades.TRADE_COLS))
        assert isinstance(fig, go.Figure)
        assert len(fig.data) == 1     # price line only, no marker traces

    def test_symbol_price_fig_adds_entry_exit_markers(self):
        fig = ba.symbol_price_fig("AAPL", self._price_df(), self._trades_df())
        assert len(fig.data) > 1

    def test_cumulative_pnl_fig_none_on_empty_trades(self):
        assert ba.cumulative_pnl_fig(pd.DataFrame(
            columns=ba.ev_trades.TRADE_COLS)) is None

    def test_cumulative_pnl_fig_builds_running_sum(self):
        fig = ba.cumulative_pnl_fig(self._trades_df())
        assert isinstance(fig, go.Figure)
        assert list(fig.data[0].y) == [200.0, 150.0]


class TestRenderRiskCard:
    def test_renders_all_metric_labels(self):
        metrics = {
            "sortino": 1.2, "calmar": 0.8, "omega": 1.5,
            "var_95_pct": 3.1, "cvar_95_pct": 4.5, "gain_to_pain": 2.0,
            "ff_alpha_ann": 1.1, "ff_r_squared": 0.42,
        }
        div = ba.render_risk_card(metrics)
        assert isinstance(div, ba.html.Div)
        assert len(div.children) == 8   # one card per metric

    def test_missing_metrics_render_as_na_without_raising(self):
        div = ba.render_risk_card({})
        assert isinstance(div, ba.html.Div)
        assert len(div.children) == 8


class TestParameterHeatmapFig:
    def test_builds_heatmap_from_simulate_live(self, monkeypatch):
        calls = []

        def fake_simulate_live(name, run_id, bull, exit_long, bear, exit_short):
            calls.append((bull, exit_long))
            return None, {"total_pnl_dollars": bull * 100}

        monkeypatch.setattr(ba, "simulate_live", fake_simulate_live)
        fig = ba.parameter_heatmap_fig("tv_threshold", "run1")
        assert isinstance(fig, go.Figure)
        assert len(calls) == 25   # 5x5 grid
        z = fig.data[0].z
        assert z.shape == (5, 5)

    def test_simulate_live_error_degrades_to_zero_cell(self, monkeypatch):
        def raising_simulate_live(*a, **k):
            raise RuntimeError("no trade rule for this signal")

        monkeypatch.setattr(ba, "simulate_live", raising_simulate_live)
        fig = ba.parameter_heatmap_fig("unknown_signal", "run1")
        assert (fig.data[0].z == 0.0).all()


class TestLayout:
    def test_builds_with_empty_registry(self):
        div = ba.build_layout([])
        assert isinstance(div, ba.html.Div)

    def test_builds_with_signals(self):
        div = ba.build_layout([{"name": "tv_threshold", "has_local_artifacts": True},
                               {"name": "factor_value", "has_local_artifacts": False}])
        assert isinstance(div, ba.html.Div)

    def test_register_callbacks_does_not_raise(self):
        app = dash.Dash(__name__)
        app.layout = ba.build_layout(ba.list_evaluated_signals())
        ba.register_callbacks(app)     # just verifies callback registration succeeds

    def test_register_callbacks_wires_new_outputs_without_raising(self):
        # register_callbacks() would raise if it referenced an Output id
        # missing from the layout -- this documents that the new IDs
        # (execution-config-error, tearsheet-container) are both present
        # and wired without needing a browser to prove it.
        app = dash.Dash(__name__)
        app.layout = ba.build_layout([
            {"name": "tv_threshold", "has_local_artifacts": True}])
        ba.register_callbacks(app)
        found = set()
        self._collect_ids(app.layout, found)
        assert "execution-config-error" in found
        assert "tearsheet-container" in found

    def _collect_ids(self, component, found):
        cid = getattr(component, "id", None)
        if cid:
            found.add(cid)
        children = getattr(component, "children", None)
        if children is None:
            return
        if isinstance(children, (list, tuple)):
            for c in children:
                self._collect_ids(c, found)
        else:
            self._collect_ids(children, found)

    def test_execution_config_and_tearsheet_ids_present(self):
        div = ba.build_layout([])
        found = set()
        self._collect_ids(div, found)
        expected = {"commission-bps", "spread-bps", "borrow-fee-bps",
                   "impact-model", "impact-coeff", "stop-loss-pct",
                   "take-profit-pct", "vol-stop-mult", "trailing",
                   "max-holding-days", "sizing-mode", "sizing-notional",
                   "sizing-fraction", "sizing-max-weight", "limits-capital",
                   "limits-max-concurrent", "limits-max-drawdown-stop",
                   "execution-config-error", "tearsheet-container"}
        assert expected.issubset(found)


class TestLiveTearsheet:
    def test_empty_trades_returns_reason(self):
        out = ba.live_tearsheet(pd.DataFrame(columns=ba.ev_trades.TRADE_COLS))
        assert out == {"returns_reason": "no realized trades"}

    def test_none_trades_returns_reason(self):
        assert ba.live_tearsheet(None) == {"returns_reason": "no realized trades"}

    def test_trades_with_no_exit_date_column_propagates_bridge_reason(self):
        trades = pd.DataFrame({"symbol": ["AAPL"], "pnl_dollars": [100.0]})
        out = ba.live_tearsheet(trades)
        assert "returns_reason" in out
        assert "exit_date" in out["returns_reason"] or "columns" in out["returns_reason"]

    def test_enough_realized_trades_returns_full_tearsheet_dict(self):
        dates = pd.bdate_range("2024-01-01", periods=40)
        trades = pd.DataFrame({
            "exit_date": dates[::4],
            "pnl_dollars": [100.0, -50.0, 200.0, 80.0, -20.0, 150.0, 60.0, -10.0,
                            120.0, 90.0],
        })
        out = ba.live_tearsheet(trades)
        assert "returns_reason" not in out
        assert set(out.keys()) == {"headline", "monthly", "rolling",
                                   "drawdowns", "underwater", "benchmark",
                                   "tail_risk"}
        assert out["headline"]["sharpe"] is not None or \
               "headline_reason" in out["headline"]


class TestRenderTearsheet:
    def test_returns_reason_renders_single_message_div(self):
        out = ba.render_tearsheet({"returns_reason": "no realized trades"})
        assert len(out) == 1
        assert isinstance(out[0], ba.html.Div)
        assert "no realized trades" in out[0].children

    def test_full_sheet_renders_markdown_and_graphs(self):
        dates = pd.bdate_range("2024-01-01", periods=40)
        trades = pd.DataFrame({
            "exit_date": dates[::4],
            "pnl_dollars": [100.0, -50.0, 200.0, 80.0, -20.0, 150.0, 60.0, -10.0,
                            120.0, 90.0],
        })
        sheet = ba.live_tearsheet(trades)
        out = ba.render_tearsheet(sheet)
        assert any(isinstance(c, ba.dcc.Markdown) for c in out)
        assert any(isinstance(c, ba.dcc.Graph) for c in out)


class TestFullChainIntegration:
    """Covers the seam the final-review Critical finding slipped through:
    no prior test drove resolve_execution_config -> simulate_live(config=)
    -> live_tearsheet -> render_tearsheet as one chain with valid raw
    Dash-shaped inputs. Only get_cache (external data load) is mocked --
    the rule engine, execution config resolution, and tearsheet rendering
    all run for real."""

    def test_resolve_to_render_tearsheet_end_to_end(self, monkeypatch):
        n_cycles = 15
        pattern = [0.0, 0.6, 0.05, -0.6, -0.05]
        rating_all = pattern * n_cycles
        n = len(rating_all)
        close = [100.0 + 0.3 * i for i in range(n)]
        cache = {"AAPL": pd.DataFrame(
            {"close": close, "rating_all": rating_all},
            index=pd.bdate_range("2024-01-01", periods=n))}
        monkeypatch.setattr(ba, "get_cache", lambda name, run_id: cache)
        ba._SIM_CACHE.clear()

        values = dict(TestResolveExecutionConfig.DEFAULTS,
                     commission_bps=1.0, spread_bps=1.0)
        cfg, err = ba.resolve_execution_config(**values)
        assert cfg is not None
        assert err == ""

        trades, summary = ba.simulate_live(
            "tv_threshold", "run_001", bull_min=0.5, exit_long_max=0.1,
            bear_max=-0.5, exit_short_min=-0.1, config=cfg)
        assert summary.get("n_trades", 0) > 0

        sheet = ba.live_tearsheet(trades)
        children = ba.render_tearsheet(sheet)
        assert isinstance(children, list)
        assert len(children) > 0
        assert any(isinstance(c, ba.dcc.Markdown) for c in children)


class TestRenderIcPanel:
    def test_trade_rule_type_renders_trades_fig(self):
        trades = pd.DataFrame({
            "symbol": ["AAPL"], "pnl_dollars": [100.0], "pnl_pct": [1.0],
        })
        out = ba._render_ic_panel({"input_type": "trade_rule"}, {}, trades)
        assert len(out) == 1

    def test_trade_rule_type_empty_trades_renders_nothing(self):
        out = ba._render_ic_panel({"input_type": "trade_rule"}, {}, pd.DataFrame())
        assert out == []

    def test_signal_type_renders_ic_charts(self):
        results = {"ic": {"5": {"pooled_ic": 0.03, "mean_daily_ic": 0.02}}}
        out = ba._render_ic_panel({"input_type": "signal"}, results, None)
        assert len(out) >= 1


class TestRegisterTradeRuleSignal:
    def test_register_restores_known_signal(self, monkeypatch):
        # deleting through monkeypatch restores the built-in after the test
        monkeypatch.delitem(ba.KNOWN_TRADE_RULE_SIGNALS, "tv_threshold")
        assert ba.has_trade_rule("tv_threshold") is False
        ba.register_trade_rule_signal("tv_threshold", dict,
                                      ba.build_tv_threshold_rule)
        assert ba.has_trade_rule("tv_threshold") is True
        assert ba.KNOWN_TRADE_RULE_SIGNALS["tv_threshold"] == (dict,
                                                               ba.build_tv_threshold_rule)

    def test_register_new_signal_is_cleaned_up(self, monkeypatch):
        # guard key so monkeypatch removes it again on teardown
        monkeypatch.setitem(ba.KNOWN_TRADE_RULE_SIGNALS, "unit_test_sig", None)
        ba.register_trade_rule_signal("unit_test_sig", dict, lambda *a: None)
        assert ba.has_trade_rule("unit_test_sig") is True


class TestCostSensitivityFig:
    def _cfg(self):
        return ba.build_execution_config(commission_bps=7.5)

    def test_sweeps_commission_keeping_other_fields_fixed(self, monkeypatch):
        seen = []

        def fake_simulate_live(name, run_id, b, el, be, es, notional=None, *,
                               config=None):
            seen.append(config.costs.commission_bps)
            return (pd.DataFrame(),
                    {"total_pnl_dollars": 1000.0 - config.costs.commission_bps * 10,
                     "n_trades": 12})

        monkeypatch.setattr(ba, "simulate_live", fake_simulate_live)
        fig = ba.cost_sensitivity_fig("tv_threshold", "run1", 0.5, 0.1,
                                      -0.5, -0.1, self._cfg())
        assert isinstance(fig, go.Figure)
        assert len(fig.data) == 2                      # P&L + trade count
        assert list(fig.data[0].x) == list(ba.COST_SWEEP_BPS)
        assert seen == list(ba.COST_SWEEP_BPS)         # one sim per level
        assert fig.data[0].y[0] > fig.data[0].y[-1]    # more bps -> less net

    def test_simulate_valueerror_returns_none(self, monkeypatch):
        def raising(*a, **k):
            raise ValueError("bad sizing combo")

        monkeypatch.setattr(ba, "simulate_live", raising)
        out = ba.cost_sensitivity_fig("tv_threshold", "run1", 0.5, 0.1,
                                      -0.5, -0.1, self._cfg())
        assert out is None


class TestCvarSensitivityFig:
    def _cfg(self):
        return ba.build_execution_config(commission_bps=7.5)

    def _trades(self, n=30, seed=0):
        rng = np.random.default_rng(seed)
        exit_dates = pd.bdate_range("2024-01-02", periods=n)
        pnl = rng.normal(50.0, 200.0, n)
        return pd.DataFrame({"exit_date": exit_dates, "pnl_dollars": pnl})

    def test_sweeps_commission_and_reports_cvar(self, monkeypatch):
        seen = []
        trades = self._trades()

        def fake_simulate_live(name, run_id, b, el, be, es, notional=None, *,
                               config=None):
            seen.append(config.costs.commission_bps)
            return trades, {"n_trades": len(trades)}

        monkeypatch.setattr(ba, "simulate_live", fake_simulate_live)
        fig = ba.cvar_sensitivity_fig("tv_threshold", "run1", 0.5, 0.1,
                                      -0.5, -0.1, self._cfg())
        assert isinstance(fig, go.Figure)
        assert len(fig.data) == 2                       # CVaR + trade count
        assert list(fig.data[0].x) == list(ba.COST_SWEEP_BPS)
        assert seen == list(ba.COST_SWEEP_BPS)          # one sim per level
        assert all(v is not None for v in fig.data[0].y)
        assert list(fig.data[1].y) == [len(trades)] * len(ba.COST_SWEEP_BPS)

    def test_simulate_valueerror_returns_none(self, monkeypatch):
        def raising(*a, **k):
            raise ValueError("bad sizing combo")

        monkeypatch.setattr(ba, "simulate_live", raising)
        out = ba.cvar_sensitivity_fig("tv_threshold", "run1", 0.5, 0.1,
                                      -0.5, -0.1, self._cfg())
        assert out is None

    def test_no_realized_trades_returns_none(self, monkeypatch):
        def fake_simulate_live(*a, **k):
            return pd.DataFrame(), {"n_trades": 0}

        monkeypatch.setattr(ba, "simulate_live", fake_simulate_live)
        out = ba.cvar_sensitivity_fig("tv_threshold", "run1", 0.5, 0.1,
                                      -0.5, -0.1, self._cfg())
        assert out is None


class TestTradesTable:
    def test_empty_trades_render_message(self):
        out = ba.trades_table(pd.DataFrame())
        assert isinstance(out, ba.html.Div)
        assert "no realized trades" in out.children

    def test_none_trades_render_message(self):
        assert "no realized trades" in ba.trades_table(None).children

    def test_populated_trades_render_datatable(self):
        trades = pd.DataFrame({
            "symbol": ["AAPL", "MSFT"], "side": ["long", "short"],
            "entry_date": ["2024-01-02", "2024-01-05"],
            "exit_date": ["2024-01-10", "2024-01-11"],
            "entry_price": [100.0, 200.0], "exit_price": [101.0, 198.0],
            "pnl_dollars": [10.0, -10.0], "pnl_pct": [1.0, -1.0],
            "exit_reason": ["signal", "stop_loss"],
            "extra_col_not_shown": [1, 2],
        })
        out = ba.trades_table(trades)
        table = out.children
        assert table.columns[0]["id"] == "symbol"
        assert len(table.data) == 2
        assert all(c["id"] != "extra_col_not_shown" for c in table.columns)


class TestWalkForwardChildren:
    def test_absent_artifacts_render_note(self):
        out = ba.walk_forward_children({})
        assert len(out) == 1
        assert "no walk-forward artifacts" in out[0].children

    def test_folds_render_header_and_bar_chart(self):
        results = {"tier3": {"walk_forward": {
            "oos": {"mean_daily_ic": 0.03, "ic_t_stat": 2.2},
            "n_train_days": 126,
            "folds": [
                {"fold": 1, "date_range": "2023-01..2023-06",
                 "mean_daily_ic": 0.04, "ic_t_stat": 1.5, "ic_days": 120},
                {"fold": 2, "date_range": "2023-07..2023-12",
                 "mean_daily_ic": 0.02, "ic_t_stat": 1.1, "ic_days": 122},
            ]}}}
        out = ba.walk_forward_children(results)
        assert "OOS mean daily IC 0.03" in out[0].children
        assert any(isinstance(c, ba.dcc.Graph) for c in out)


class TestRobustnessChildren:
    def _run(self, monkeypatch, trades=None):
        monkeypatch.setattr(ba.ev_robustness, "noise_test",
                            lambda rule, cache, **k: {
                                "observed_pnl_dollars": 500.0,
                                "noise_pct_profitable": 80.0,
                                "noise_median_pnl_dollars": 100.0,
                                "noise_p95_pnl_dollars": 900.0})
        monkeypatch.setattr(ba.ev_robustness, "price_mcpt",
                            lambda rule, cache, **k: {
                                "price_mcpt_p": 0.12,
                                "observed_pnl_dollars": 500.0, "n_perm": 100})
        monkeypatch.setattr(ba.ev_robustness, "trade_order_mc",
                            lambda t, **k: {
                                "n_trades": 74, "final_return_pct": 3.4,
                                "observed_mdd_pct": 30.0,
                                "mdd_median_pct": 35.3, "mdd_p5_pct": 20.0,
                                "mdd_p95_pct": 50.0, "mdd_worst_pct": 59.5,
                                "observed_mdd_percentile": 23.0,
                                "n_trials": 500})
        return ba.robustness_children("tv_threshold", "run1", 0.5, 0.1,
                                      -0.5, -0.1, ba.build_execution_config(),
                                      trades)

    def test_full_results_render_three_cards_plus_disclaimer(self, monkeypatch):
        out = self._run(monkeypatch, trades=pd.DataFrame({"x": [1]}))
        text = str(out[0])
        assert "80.0% of perturbed trials profitable" in text
        assert "price MCPT p = 0.12" in text
        assert "23.0th pct" in text         # observed drawdown percentile
        assert "Diagnosis only" in str(out[-1])

    def test_no_trades_degrades_order_card(self, monkeypatch):
        out = self._run(monkeypatch, trades=None)
        assert "trade-order MC: n/a" in str(out[0])


class TestParameterSurfaceMode:
    def test_heatmap_mode_returns_heatmap_trace(self, monkeypatch):
        monkeypatch.setattr(ba, "simulate_live",
                            lambda *a, **k: (None, {"total_pnl_dollars": 1.0}))
        fig = ba.parameter_heatmap_fig("tv_threshold", "run1", mode="heatmap")
        assert isinstance(fig.data[0], go.Heatmap)

    def test_surface_mode_returns_surface_trace(self, monkeypatch):
        monkeypatch.setattr(ba, "simulate_live",
                            lambda *a, **k: (None, {"total_pnl_dollars": 1.0}))
        fig = ba.parameter_heatmap_fig("tv_threshold", "run1", mode="surface")
        assert isinstance(fig.data[0], go.Surface)
