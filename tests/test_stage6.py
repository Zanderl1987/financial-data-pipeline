"""
Tests for strategies/catalog.py's Stage 6 verdict labeling and
strategies/stage6.py's preview/apply runners. Uses monkeypatched
build_catalog_rows so these never touch real data.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies import catalog as cat  # noqa: E402
import strategies.stage6 as stage6  # noqa: E402


class TestLabelVerdict:
    @pytest.mark.parametrize("stage,pnl,p,expected", [
        ("stage5", 0.0499, 0.05, "promising"),      # strictly below the bar
        ("stage5", 0.0099, 0.05, "promising"),
        ("stage5", 0.05,   0.05, "undetermined"),    # exactly at bar -> NOT promising
        ("stage5", 0.12,   0.05, "undetermined"),
        ("stage5", None,   0.05, "undetermined"),    # no holdout value
        ("stage3", 0.0099, 0.05, "undetermined"),    # Stage 5 survivors ONLY
        ("stage2", None,   0.05, "undetermined"),
        ("stage4", 0.0099, 0.05, "undetermined"),
    ])
    def test_boundary(self, stage, pnl, p, expected):
        assert cat.label_verdict(stage, pnl) == expected

    def test_nan_holdout_not_promising(self):
        assert cat.label_verdict("stage5", float("nan")) == "undetermined"

    def test_verdict_column_in_schema(self):
        assert "verdict" in cat.SCHEMA_COLUMNS


class TestStage6Runners:
    def test_preview_shows_promising_only_for_stage5_survivors(self, monkeypatch):
        rows = pd.DataFrame([
            {"strategy_id": "survivor", "stage": "stage5",
             "verdict": "promising", "pnl_p": 0.0099,
             "holdout_pnl_p": 0.0099, "provisional": True},
            {"strategy_id": "untested", "stage": "stage2",
             "verdict": "undetermined", "pnl_p": None,
             "holdout_pnl_p": None, "provisional": True},
            {"strategy_id": "not_significant", "stage": "stage3",
             "verdict": "undetermined", "pnl_p": 0.6,
             "holdout_pnl_p": None, "provisional": True},
        ])
        monkeypatch.setattr(stage6, "build_catalog_rows", lambda: rows)
        out = stage6.preview()
        assert len(out) == 3
        assert set(out.loc[out["verdict"] == "promising", "strategy_id"]) == {"survivor"}
        assert set(out.loc[out["strategy_id"] == "untested", "verdict"]) == {"undetermined"}
        assert set(out.loc[out["strategy_id"] == "not_significant", "verdict"]) == {"undetermined"}

    def test_apply_persists_catalog(self, monkeypatch):
        rows = pd.DataFrame([
            {"strategy_id": "survivor", "stage": "stage5", "verdict": "promising",
             "pnl_p": 0.0099, "holdout_pnl_p": 0.0099, "provisional": True},
        ])
        monkeypatch.setattr(stage6, "build_catalog_rows", lambda: rows)
        written = {}
        monkeypatch.setattr(stage6, "write_catalog_table",
                            lambda df: written.setdefault("df", df))
        out = stage6.apply()
        assert written["df"].equals(rows)
        assert not out.empty