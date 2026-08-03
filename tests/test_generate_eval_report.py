"""
tests/test_generate_eval_report.py -- coverage backfill for the functions
backtest_app.py depends on directly: find_latest, load_run,
classify_significance. generate_eval_report.py itself is unchanged and
already working in production (it produces the static HTML report); these
tests exist because nothing exercised these functions before, not because
anything was broken.
"""

import json
import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import generate_eval_report as ger


class TestFindLatest:
    def test_picks_newest_by_sorted_suffix(self, tmp_path):
        root = tmp_path / "eval"
        root.mkdir()
        (root / "sig_20260101_000000").mkdir()
        (root / "sig_20260301_000000").mkdir()
        (root / "sig_20260215_000000").mkdir()
        assert ger.find_latest("sig", root=str(root)) == str(
            root / "sig_20260301_000000")

    def test_returns_none_when_no_match(self, tmp_path):
        root = tmp_path / "eval"
        root.mkdir()
        assert ger.find_latest("missing_signal", root=str(root)) is None

    def test_ignores_files_only_matches_dirs(self, tmp_path):
        root = tmp_path / "eval"
        root.mkdir()
        (root / "sig_20260101_000000").write_text("not a dir")
        assert ger.find_latest("sig", root=str(root)) is None


class TestLoadRun:
    def _make_run_dir(self, tmp_path, with_trades=True):
        run_dir = tmp_path / "sig_20260101_000000"
        run_dir.mkdir()
        with open(run_dir / "results.json", "w", encoding="utf-8") as fh:
            json.dump({"summary": {"n_trades": 2}}, fh)
        with open(run_dir / "run_meta.json", "w", encoding="utf-8") as fh:
            json.dump({"input_name": "sig", "input_type": "trade_rule"}, fh)
        if with_trades:
            pd.DataFrame({"symbol": ["AAPL"], "pnl_dollars": [10.0]}).to_parquet(
                run_dir / "trades.parquet", index=False)
        return str(run_dir)

    def test_loads_results_and_meta(self, tmp_path):
        run_dir = self._make_run_dir(tmp_path)
        results, meta, trades = ger.load_run(run_dir)
        assert results == {"summary": {"n_trades": 2}}
        assert meta == {"input_name": "sig", "input_type": "trade_rule"}
        assert list(trades["symbol"]) == ["AAPL"]

    def test_trades_none_when_no_trades_parquet(self, tmp_path):
        run_dir = self._make_run_dir(tmp_path, with_trades=False)
        _, _, trades = ger.load_run(run_dir)
        assert trades is None


class TestClassifySignificance:
    def test_none_inputs_are_noise(self):
        assert ger.classify_significance(None, None) == "noise"

    def test_low_ic_or_low_t_is_noise(self):
        assert ger.classify_significance(0.01, 5.0) == "noise"
        assert ger.classify_significance(0.03, 1.0) == "noise"

    def test_mid_ic_is_weak(self):
        assert ger.classify_significance(0.03, 3.0) == "weak"

    def test_high_ic_is_significant(self):
        assert ger.classify_significance(0.08, 4.0) == "significant"
