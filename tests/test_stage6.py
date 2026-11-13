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
        # holdout P&L held profitable here so only the p-value bar varies
        assert cat.label_verdict(stage, pnl, 1000.0) == expected

    @pytest.mark.parametrize("holdout_pnl_net", [-91177.0, 0.0, None, float("nan")])
    def test_significant_but_not_profitable_is_not_promising(self, holdout_pnl_net):
        """Amendment 2026-10-28 item 3: beating the null while losing money (or
        with no P&L recorded) is not promising."""
        assert cat.label_verdict("stage5", 0.0099, holdout_pnl_net) == "undetermined"

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

class TestVoidedRowsIgnored:
    """Amendment 2026-10-28 item 4: the catalog derives nothing from rows
    written under the pre-amendment evaluation names."""

    @staticmethod
    def _row(evaluation, slug, statistic, value):
        return {"run_id": "r", "input_name": f"pine_{slug}", "input_type": "trade_rule",
                "evaluation": evaluation, "horizon": -1, "statistic": statistic,
                "value": value, "n": 1, "universe_hash": "u", "date_range": None,
                "created_at": "2026-10-28T00:00:00Z"}

    def test_only_v2_rows_are_read(self, monkeypatch):
        rows = [
            self._row("tv_strategy_catalog_stage3", "old", "pnl_p", 0.005),
            self._row("tv_strategy_catalog_stage3_v2", "new", "pnl_p", 0.30),
            self._row("tv_strategy_catalog_stage4", "old", "bh_q", 0.01),
            self._row("tv_strategy_catalog_stage4_v2", "new", "bh_q", 0.40),
            self._row("tv_strategy_catalog_stage5", "old", "holdout_pnl_p", 0.0099),
        ]
        monkeypatch.setattr(cat.ev_registry, "load", lambda: pd.DataFrame(rows))
        assert cat._stage3_wide()["strategy_id"].tolist() == ["new"]
        assert cat._stage4_wide()["strategy_id"].tolist() == ["new"]
        assert cat._stage5_wide().empty
