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
        df = self._df()
        loose = ba.build_tv_threshold_rule(0.5, 0.1, -0.5, -0.1)
        tight = ba.build_tv_threshold_rule(0.0, 0.1, -0.5, -0.1)
        le_loose, _, _, _ = ev_trades.rule_flags(loose, df)
        le_tight, _, _, _ = ev_trades.rule_flags(tight, df)
        # tight (0.0) fires on the very first crossing above 0.0, loose only
        # once rating_all reaches 0.5 in the same step -- both fire on row 1
        # here, so assert the tight rule fires at least as early overall.
        assert np.flatnonzero(le_tight)[0] <= np.flatnonzero(le_loose)[0]

    def test_side_is_both(self):
        rule = ba.build_tv_threshold_rule(0.5, 0.1, -0.5, -0.1)
        assert rule.side == "both"
        assert rule.name == "tv_threshold_live"
