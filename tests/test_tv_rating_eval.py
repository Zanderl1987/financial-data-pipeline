"""
test_tv_rating_eval.py — TV rating backtest: signal cache, return panel,
level-IC stats, transition study, trade simulation. No API keys or stored
data required; analytics.technical.rating_history / event_backtest are
monkeypatched with synthetic frames.
"""

import json
import math
import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import tv_rating_eval as tve
import event_backtest as eb


class TestUniverseAndCache:
    def test_universe_calls_query_symbols(self, monkeypatch):
        monkeypatch.setattr(tve.q, "symbols", lambda table: ["AAPL", "MSFT"])
        assert tve.universe() == ["AAPL", "MSFT"]

    def test_cache_skips_empty_symbols(self, monkeypatch):
        good = pd.DataFrame({"close": [1.0, 2.0]},
                            index=pd.bdate_range("2024-01-01", periods=2))

        def fake_rating_history(sym, **kw):
            return good if sym == "GOOD" else pd.DataFrame()

        monkeypatch.setattr("analytics.technical.rating_history", fake_rating_history)
        cache = tve.build_signal_cache(["GOOD", "BAD"])
        assert list(cache.keys()) == ["GOOD"]


class TestReturnPanel:
    def _cache(self):
        dates = pd.bdate_range("2024-01-01", periods=7)
        x = pd.DataFrame({
            "close": [100, 101, 102, 103, 104, 105, 106],
            "rating_all": [0.6] * 7, "rating_ma": [0.5] * 7,
            "rating_osc": [0.7] * 7, "rating_label": ["strong_buy"] * 7,
        }, index=dates)
        bench = pd.DataFrame({
            "close": [200.0] * 7, "rating_all": [0.0] * 7, "rating_ma": [0.0] * 7,
            "rating_osc": [0.0] * 7, "rating_label": ["neutral"] * 7,
        }, index=dates)
        return {"X": x, "SPY": bench}

    def test_next_close_entry_and_benchmark_excluded(self):
        panel = tve.build_return_panel(self._cache(), horizons=(1, 2), benchmark="SPY")
        assert list(panel["symbol"].unique()) == ["X"]
        row0 = panel.iloc[0]
        assert row0["fwd_1d"] == pytest.approx(102 / 101 - 1.0)
        assert row0["fwd_2d"] == pytest.approx(103 / 101 - 1.0)

    def test_insufficient_future_data_is_nan(self):
        panel = tve.build_return_panel(self._cache(), horizons=(2,), benchmark="SPY")
        assert pd.isna(panel.iloc[-1]["fwd_2d"])

    def test_no_benchmark_gives_raw_return(self):
        panel = tve.build_return_panel(self._cache(), horizons=(1,), benchmark=None)
        assert panel.iloc[0]["fwd_1d"] == pytest.approx(102 / 101 - 1.0)

    def test_excess_vs_moving_benchmark(self):
        dates = pd.bdate_range("2024-01-01", periods=5)
        x = pd.DataFrame({"close": [100, 102, 104, 106, 108],
                          "rating_all": [0.6] * 5, "rating_ma": [0.5] * 5,
                          "rating_osc": [0.7] * 5, "rating_label": ["strong_buy"] * 5},
                         index=dates)
        bench = pd.DataFrame({"close": [200, 200, 202, 204, 206],
                              "rating_all": [0.0] * 5, "rating_ma": [0.0] * 5,
                              "rating_osc": [0.0] * 5, "rating_label": ["neutral"] * 5},
                             index=dates)
        panel = tve.build_return_panel({"X": x, "SPY": bench}, horizons=(1,),
                                       benchmark="SPY")
        raw = 104 / 102 - 1.0
        bench_ret = 202 / 200 - 1.0
        assert panel.iloc[0]["fwd_1d"] == pytest.approx(raw - bench_ret)


def _synthetic_panel(n_days=30, n_syms=10, noise=0.0, seed=7):
    """Panel where fwd_1d is a monotone function of rating_all (+ noise)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-02", periods=n_days)
    rows = []
    for d in dates:
        for i in range(n_syms):
            score = rng.uniform(-1, 1)
            rows.append({
                "symbol": f"S{i}", "date": d, "rating_all": score,
                "fwd_1d": 0.01 * score + noise * rng.normal(),
            })
    return pd.DataFrame(rows)


class TestEvaluateSignal:
    def test_recovers_positive_signal(self):
        panel = _synthetic_panel()
        res = tve.evaluate_signal(panel, "rating_all", horizons=(1,))
        assert 1 in res
        r = res[1]
        assert r["pooled_ic"] > 0.9
        assert r["mean_daily_ic"] > 0.9
        assert r["ic_days"] == 30
        assert r["spread_pct"] > 0
        # noise=0.0 makes fwd_1d an exact positive-scalar multiple of rating_all,
        # so every single day's Spearman rho is exactly 1.0 -- zero cross-day
        # variance, so ic_se/ic_t_stat are correctly None (same sd>0 guard
        # sentiment_eval.evaluate() already uses for its own t-stat).
        assert r["ic_se"] is None

    def test_ic_se_positive_with_noisy_signal(self):
        # noise=0.05 breaks the exact-rho-1.0-every-day degeneracy above, so
        # ic_se's sd>0 branch is actually exercised and produces a real value.
        panel = _synthetic_panel(noise=0.05)
        res = tve.evaluate_signal(panel, "rating_all", horizons=(1,))
        r = res[1]
        assert r["ic_se"] is not None
        assert r["ic_se"] > 0

    def test_insufficient_rows_skipped(self):
        panel = _synthetic_panel(n_days=1, n_syms=5)
        res = tve.evaluate_signal(panel, "rating_all", horizons=(1,))
        assert res == {} or "mean_daily_ic" not in res.get(1, {})

    def test_daily_ic_withheld_below_min_names(self):
        panel = _synthetic_panel(n_days=30, n_syms=3)
        res = tve.evaluate_signal(panel, "rating_all", horizons=(1,), min_names=5)
        assert "mean_daily_ic" not in res[1]

    def test_missing_horizon_column_ignored(self):
        panel = _synthetic_panel()
        res = tve.evaluate_signal(panel, "rating_all", horizons=(1, 21))
        assert 1 in res and 21 not in res

    def test_works_on_any_signal_column_name(self):
        panel = _synthetic_panel().rename(columns={"rating_all": "rating_osc"})
        res = tve.evaluate_signal(panel, "rating_osc", horizons=(1,))
        assert res[1]["pooled_ic"] > 0.9


class TestTransitionStudy:
    def test_skips_groups_below_min_events(self, monkeypatch):
        changes = pd.DataFrame({
            "symbol": ["A", "B", "C"],
            "date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
            "from_label": ["neutral", "neutral", "buy"],
            "to_label": ["buy", "buy", "strong_buy"],
            "from_score": [0.0, 0.0, 0.3], "to_score": [0.3, 0.3, 0.6],
            "step": [1, 1, 1], "direction": ["upgrade"] * 3,
        })
        monkeypatch.setattr(tve.eb, "rating_changes", lambda *a, **k: changes)

        called = {}

        def fake_event_study(events, **kw):
            called["n"] = len(events)
            return eb.EventStudyResult(
                car=pd.DataFrame(), mean_car=pd.Series({0: 0.0, 21: 0.01}),
                horizons=pd.DataFrame({"n": [len(events)], "mean_pct": [1.0],
                                      "median_pct": [1.0], "hit_rate_pct": [60.0],
                                      "t_stat": [2.5], "baseline_pct": [0.5],
                                      "edge_pct": [0.5]}, index=[21]),
                events=events, baseline=pd.Series(dtype=float), params={})

        monkeypatch.setattr(tve.eb, "event_study", fake_event_study)

        paths, summary = tve.run_transition_study(["A", "B", "C"], min_events=2)
        assert called["n"] == 2                       # only neutral->buy qualifies
        assert "neutral->buy" in summary
        assert "buy->strong_buy" not in summary        # only 1 event, below min_events
        assert set(paths["from_label"]) == {"neutral"}

    def test_empty_changes_returns_empty(self, monkeypatch):
        monkeypatch.setattr(tve.eb, "rating_changes",
                            lambda *a, **k: pd.DataFrame(columns=eb._CHANGE_COLS))
        paths, summary = tve.run_transition_study(["A"])
        assert paths.empty
        assert summary == {}

    def test_always_passes_explicit_start(self, monkeypatch):
        captured = {}

        def fake_rating_changes(symbols, start=None, end=None, price_table=None):
            captured["start"] = start
            return pd.DataFrame(columns=eb._CHANGE_COLS)

        monkeypatch.setattr(tve.eb, "rating_changes", fake_rating_changes)
        tve.run_transition_study(["A"])
        assert captured["start"] is not None
