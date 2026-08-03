"""
tests/test_backtest_app.py -- unit tests for backtest_app.py's pure logic
(registry/artifact loading, live trade-rule simulation, chart data prep).
Dash callback wiring itself is smoke-tested (layout construction only) --
no Selenium/browser harness, per docs/superpowers/specs/
2026-08-03-interactive-backtest-explorer-design.md.
"""

import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import backtest_app as ba


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
