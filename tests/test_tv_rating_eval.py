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
