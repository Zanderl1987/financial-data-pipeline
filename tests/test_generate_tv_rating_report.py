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

    def test_no_qualifying_symbols_returns_empty_not_crash(self):
        # Panel too thin for any symbol to reach the len(sub) >= 10 floor --
        # must return an empty table with the expected columns, not raise
        # KeyError from sort_values("best_ic") on a rows-less DataFrame.
        dates = pd.bdate_range("2024-01-01", periods=5)
        panel = pd.DataFrame({
            "symbol": "X", "date": dates,
            "rating_all": np.linspace(-1, 1, 5),
            "fwd_1d": np.linspace(-0.01, 0.01, 5),
        })
        out = gr.build_symbol_table(panel, signal="rating_all", horizons=(1,))
        assert out.empty
        assert list(out.columns) == ["symbol", "n_signals", "best_horizon",
                                     "best_ic", "worst_horizon", "worst_ic"]


class TestICBarChart:
    def test_three_signal_traces(self):
        ic_stats = {"level_ic": {sig: {"1": {"mean_daily_ic": 0.03, "ic_t_stat": 2.0,
                    "ic_se": 0.015, "ic_days": 100}} for sig in gr.COLOR_SERIES}}
        fig = gr.build_ic_bar_chart(ic_stats)
        assert len(fig.data) == 3
        assert {tr.name for tr in fig.data} == set(gr.COLOR_SERIES)


class TestSpreadChart:
    def test_three_subplots_no_legend(self):
        ic_stats = {"level_ic": {sig: {"1": {"spread_pct": 0.5}}
                    for sig in gr.COLOR_SERIES}}
        fig = gr.build_spread_chart(ic_stats)
        assert len(fig.data) == 3
        assert all(tr.showlegend is False for tr in fig.data)


class TestScatterSection:
    def test_dropdown_has_one_button_per_combo(self):
        dates = pd.bdate_range("2024-01-01", periods=20)
        panel = pd.DataFrame({
            "symbol": "X", "date": dates, "rating_all": np.linspace(-1, 1, 20),
            "rating_ma": np.linspace(-1, 1, 20), "rating_osc": np.linspace(-1, 1, 20),
            **{f"fwd_{h}d": np.linspace(-0.05, 0.05, 20) for h in gr.HORIZONS},
        })
        fig = gr.build_scatter_section(panel)
        assert len(fig.data) == len(gr.SIGNALS) * len(gr.HORIZONS)
        assert len(fig.layout.updatemenus[0].buttons) == len(gr.SIGNALS) * len(gr.HORIZONS)
        assert fig.data[0].visible is True
        assert fig.data[1].visible is False


class TestTransitionChart:
    def test_one_trace_per_transition_type(self):
        df = pd.DataFrame({
            "from_label": ["neutral", "neutral", "buy"],
            "to_label": ["buy", "buy", "strong_buy"],
            "rel_day": [0, 21, 0], "mean_car_pct": [0.0, 1.5, 0.0],
            "n": [2, 2, 1],
        })
        fig = gr.build_transition_chart(df)
        assert len(fig.data) == 2   # 2 distinct (from_label, to_label) groups

    def test_empty_input_no_crash(self):
        fig = gr.build_transition_chart(pd.DataFrame(
            columns=["from_label", "to_label", "rel_day", "mean_car_pct", "n"]))
        assert len(fig.data) == 0


class TestPriceTradesChart:
    def test_visibility_toggles_per_symbol(self):
        dates = pd.bdate_range("2024-01-01", periods=5)
        panel = pd.DataFrame({"symbol": ["A"] * 5 + ["B"] * 5,
                              "date": list(dates) * 2,
                              "close": [10, 11, 12, 13, 14, 20, 21, 22, 23, 24]})
        trades = pd.DataFrame(columns=["symbol", "side", "entry_date", "entry_price",
                                       "exit_date", "exit_price", "pnl_dollars", "pnl_pct"])
        fig = gr.build_price_trades_chart(panel, trades, symbols=["A", "B"])
        assert len(fig.data) == 10          # 5 traces x 2 symbols
        assert len(fig.layout.updatemenus[0].buttons) == 2
        vis0 = fig.layout.updatemenus[0].buttons[0].args[0]["visible"]
        assert vis0 == [True] * 5 + [False] * 5


class TestCumulativePnlChart:
    def test_cumulative_sum_matches_manual(self):
        trades = pd.DataFrame({
            "symbol": ["A", "B"], "side": ["long", "short"],
            "exit_date": pd.to_datetime(["2024-01-05", "2024-01-03"]),
            "pnl_dollars": [200.0, -50.0], "pnl_pct": [2.0, -0.5],
        })
        fig = gr.build_cumulative_pnl_chart(trades)
        y = list(fig.data[0].y)
        assert y == [-50.0, 150.0]     # sorted by exit_date: B(-50) then A(+200)


class TestAssembleReport:
    def test_writes_html_file_with_expected_sections(self, tmp_path):
        out_dir = tmp_path / "artifacts"
        out_dir.mkdir()
        ic_stats = {"level_ic": {sig: {"1": {
            "n": 50, "pooled_ic": 0.03, "pooled_p": 0.02, "mean_daily_ic": 0.025,
            "ic_t_stat": 2.2, "ic_se": 0.011, "ic_days": 40,
            "spread_pct": 0.4, "spread_t": 1.8}} for sig in gr.COLOR_SERIES},
            "transition_stats": {}}
        with open(out_dir / "ic_stats.json", "w") as f:
            json.dump(ic_stats, f)

        dates = pd.bdate_range("2024-01-01", periods=10)
        panel = pd.DataFrame({
            "symbol": "AAPL", "date": dates, "close": np.linspace(100, 110, 10),
            "rating_all": np.linspace(-1, 1, 10), "rating_ma": np.linspace(-1, 1, 10),
            "rating_osc": np.linspace(-1, 1, 10),
            **{f"fwd_{h}d": np.linspace(-0.02, 0.02, 10) for h in gr.HORIZONS},
        })
        panel.to_parquet(out_dir / "panel.parquet", index=False)
        pd.DataFrame(columns=["from_label", "to_label", "rel_day", "mean_car_pct", "n"]
                    ).to_parquet(out_dir / "transitions.parquet", index=False)
        pd.DataFrame(columns=["symbol", "side", "entry_date", "entry_price",
                              "exit_date", "exit_price", "pnl_dollars", "pnl_pct"]
                    ).to_parquet(out_dir / "trades.parquet", index=False)

        report_path = tmp_path / "report.html"
        path = gr.assemble_report(str(out_dir), str(report_path))
        content = report_path.read_text(encoding="utf-8")

        assert path == str(report_path)
        assert "TradingView Rating Backtest" in content
        assert "How to read this report" in content
        assert content.lower().count("plotly") > 0
