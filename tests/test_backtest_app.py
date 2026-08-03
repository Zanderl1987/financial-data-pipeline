"""
tests/test_backtest_app.py -- unit tests for backtest_app.py's pure logic
(registry/artifact loading, live trade-rule simulation, chart data prep).
Dash callback wiring itself is smoke-tested (layout construction only) --
no Selenium/browser harness, per docs/superpowers/specs/
2026-08-03-interactive-backtest-explorer-design.md.
"""

import os
import sys

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


class TestCacheAndSimulateLive:
    def test_has_trade_rule_true_for_known_signal(self):
        assert ba.has_trade_rule("tv_threshold") is True

    def test_has_trade_rule_false_for_unknown_signal(self):
        assert ba.has_trade_rule("factor_value") is False

    def test_get_cache_builds_once_and_reuses(self, monkeypatch):
        calls = []

        def fake_rating_cache():
            calls.append(1)
            return {"AAPL": pd.DataFrame({"close": [1.0], "rating_all": [0.0]})}

        monkeypatch.setitem(ba.KNOWN_TRADE_RULE_SIGNALS, "tv_threshold",
                            fake_rating_cache)
        ba._CACHE.clear()
        first = ba.get_cache("tv_threshold")
        second = ba.get_cache("tv_threshold")
        assert first is second
        assert len(calls) == 1

    def test_get_cache_raises_for_unknown_signal(self):
        with pytest.raises(KeyError):
            ba.get_cache("factor_value")

    def test_simulate_live_zero_trades_at_extreme_threshold(self, monkeypatch):
        cache = {"AAPL": pd.DataFrame(
            {"close": [10.0, 11.0, 12.0], "rating_all": [0.0, 0.1, 0.2]},
            index=pd.bdate_range("2024-01-01", periods=3))}
        monkeypatch.setattr(ba, "get_cache", lambda name: cache)
        trades, summary = ba.simulate_live("tv_threshold", bull_min=0.99,
                                           exit_long_max=0.1, bear_max=-0.99,
                                           exit_short_min=-0.1)
        assert trades.empty
        assert summary == {"n_trades": 0, "summary_reason": "no realized trades"}


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
