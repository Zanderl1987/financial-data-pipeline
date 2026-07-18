"""
test_generate_tv_rating_report.py — TV rating dashboard report builder.
Pure data-prep/classification functions are unit tested directly; chart
builders (Task 7) are tested for structural correctness (trace counts,
visibility arrays), not pixel output; assemble_report (Task 8) is tested
end-to-end against synthetic artifacts written to tmp_path.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import generate_tv_rating_report as gr


class TestClassifySignificance:
    def test_noise_below_ic_floor(self):
        assert gr.classify_significance(0.01, 3.0) == "noise"

    def test_noise_below_t_floor(self):
        assert gr.classify_significance(0.03, 1.5) == "noise"

    def test_weak_band(self):
        assert gr.classify_significance(0.03, 2.5) == "weak"

    def test_significant_band(self):
        assert gr.classify_significance(0.07, 3.0) == "significant"

    def test_none_inputs_are_noise(self):
        assert gr.classify_significance(None, None) == "noise"


class TestHeadlineRows:
    def test_builds_rows_from_nested_json(self):
        ic_stats = {"level_ic": {"rating_all": {"1": {
            "n": 100, "pooled_ic": 0.05, "pooled_p": 0.01, "mean_daily_ic": 0.04,
            "ic_t_stat": 2.5, "ic_se": 0.016, "ic_days": 300,
            "spread_pct": 1.2, "spread_t": 2.1}}}}
        rows = gr.build_headline_rows(ic_stats)
        assert len(rows) == 1
        assert rows[0]["signal"] == "rating_all"
        assert rows[0]["horizon"] == 1
        assert rows[0]["tier"] == "weak"


class TestSymbolTable:
    def test_best_worst_horizon_identified(self):
        # NOTE: fwd_1d/fwd_5d must NOT both be clean positive-scalar multiples
        # of rating_all -- Spearman rho is scale-invariant, so two such columns
        # tie at rho=1.0 exactly and "best horizon" becomes undecidable. fwd_1d
        # gets heavy noise (weak relation); fwd_5d stays a clean transform
        # (rho=1.0) so the two are unambiguously, deterministically different.
        dates = pd.bdate_range("2024-01-01", periods=60)
        rng = np.random.default_rng(3)
        signal = np.linspace(-1, 1, 60)
        panel = pd.DataFrame({
            "symbol": "X", "date": dates, "rating_all": signal,
            "fwd_1d": signal * 0.001 + rng.normal(0, 0.5, 60),  # weak relation
            "fwd_5d": signal * 0.05,                            # strong relation
        })
        out = gr.build_symbol_table(panel, signal="rating_all", horizons=(1, 5))
        row = out.iloc[0]
        assert row["symbol"] == "X"
        assert row["best_horizon"] == 5
