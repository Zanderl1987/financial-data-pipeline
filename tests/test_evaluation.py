"""
test_evaluation.py -- unified evaluation framework (evaluation/ package).
All synthetic data; no stored data or API keys. Repo modules
(event_backtest, backtest, query, analytics.*) are monkeypatched where the
code under test imports them locally.
"""

import json
import math
import os
import sys
import warnings

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from evaluation.contracts import Signal, EventSet, TradeRule


def _sig_frame(n_dates=300, symbols=("AAA", "BBB", "CCC")):
    dates = pd.bdate_range("2023-01-02", periods=n_dates)
    rows = [{"symbol": s, "date": d, "value": float(i + j)}
            for j, s in enumerate(symbols) for i, d in enumerate(dates)]
    return pd.DataFrame(rows)


class TestSignalContract:
    def test_valid_signal_constructs_and_sorts(self):
        f = _sig_frame().sample(frac=1.0, random_state=0)   # shuffled input
        s = Signal(name="toy", frame=f)
        assert list(s.frame.columns) == ["symbol", "date", "value"]
        assert s.frame["date"].is_monotonic_increasing
        assert str(s.frame["date"].dtype) == "datetime64[ns]"

    def test_missing_column_raises(self):
        with pytest.raises(ValueError, match="missing columns"):
            Signal(name="toy", frame=pd.DataFrame({"symbol": ["A"], "date": ["2024-01-02"]}))

    def test_duplicate_symbol_date_raises(self):
        f = pd.concat([_sig_frame(), _sig_frame().head(1)], ignore_index=True)
        with pytest.raises(ValueError, match="duplicate"):
            Signal(name="toy", frame=f)

    def test_nan_value_raises(self):
        f = _sig_frame()
        f.loc[0, "value"] = np.nan
        with pytest.raises(ValueError, match="NaN"):
            Signal(name="toy", frame=f)

    def test_tz_aware_dates_raise(self):
        f = _sig_frame()
        f["date"] = pd.to_datetime(f["date"]).dt.tz_localize("UTC")
        with pytest.raises(ValueError, match="tz-naive"):
            Signal(name="toy", frame=f)

    def test_bad_direction_raises(self):
        with pytest.raises(ValueError, match="direction"):
            Signal(name="toy", frame=_sig_frame(), direction=2)

    def test_short_history_warns_not_fails(self):
        with pytest.warns(UserWarning, match="distinct dates"):
            Signal(name="toy", frame=_sig_frame(n_dates=50))

    def test_long_history_no_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Signal(name="toy", frame=_sig_frame(n_dates=300))


class TestEventSetContract:
    def test_valid_event_set(self):
        f = pd.DataFrame({"symbol": ["AAA", "BBB"],
                          "date": ["2024-01-03", "2024-02-05"],
                          "label": ["up", "down"]})
        e = EventSet(name="toy_events", frame=f)
        assert e.min_events == 5
        assert str(e.frame["date"].dtype) == "datetime64[ns]"

    def test_missing_label_raises(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": ["2024-01-03"]})
        with pytest.raises(ValueError, match="missing columns"):
            EventSet(name="toy_events", frame=f)

    def test_nan_label_raises(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": ["2024-01-03"], "label": [None]})
        with pytest.raises(ValueError, match="NaN"):
            EventSet(name="toy_events", frame=f)


class TestTradeRuleContract:
    def test_valid_long_rule(self):
        r = TradeRule(name="toy_rule",
                      entries=lambda d: d["x"] > 0, exits=lambda d: d["x"] < 0)
        assert r.side == "long" and r.notional == 10_000.0

    def test_bad_side_raises(self):
        with pytest.raises(ValueError, match="side"):
            TradeRule(name="r", entries=lambda d: d, exits=lambda d: d, side="sideways")

    def test_both_requires_short_pair(self):
        with pytest.raises(ValueError, match="short_entries"):
            TradeRule(name="r", entries=lambda d: d, exits=lambda d: d, side="both")

    def test_non_callable_raises(self):
        with pytest.raises(ValueError, match="callable"):
            TradeRule(name="r", entries="not a function", exits=lambda d: d)


from evaluation.data import HORIZONS, apply_lag, build_return_panel


def _close_matrix(n=40):
    idx = pd.bdate_range("2024-01-02", periods=n)
    return pd.DataFrame({"AAA": 100.0 * (1.01 ** np.arange(n)),
                         "SPY": np.full(n, 100.0)}, index=idx)


class TestApplyLag:
    def test_zero_lag_returns_copy_unchanged(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": [pd.Timestamp("2024-01-05")],
                          "value": [1.0]})
        out = apply_lag(f, 0)
        assert out is not f
        assert out["date"].iloc[0] == pd.Timestamp("2024-01-05")

    def test_lag_moves_business_days(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": [pd.Timestamp("2024-01-05")],
                          "value": [1.0]})           # a Friday
        out = apply_lag(f, 2)
        assert out["date"].iloc[0] == pd.Timestamp("2024-01-09")   # Fri + 2bd = Tue


class TestBuildReturnPanel:
    def test_entry_is_strictly_next_close(self):
        closes = _close_matrix()
        idx = closes.index
        f = pd.DataFrame({"symbol": ["AAA"], "date": [idx[5]], "value": [1.0]})
        panel, dropped = build_return_panel(f, closes, benchmark=None)
        assert dropped == {}
        assert panel["entry_date"].iloc[0] == idx[6]
        expected = closes["AAA"].iloc[7] / closes["AAA"].iloc[6] - 1.0
        assert panel["fwd_1d"].iloc[0] == pytest.approx(expected)

    def test_flat_benchmark_equals_raw_return(self):
        closes = _close_matrix()          # SPY constant 100 -> excess == raw
        f = pd.DataFrame({"symbol": ["AAA"], "date": [closes.index[5]], "value": [1.0]})
        raw, _ = build_return_panel(f, closes, benchmark=None)
        exc, _ = build_return_panel(f, closes, benchmark="SPY")
        assert exc["fwd_5d"].iloc[0] == pytest.approx(raw["fwd_5d"].iloc[0])

    def test_excess_vs_identical_benchmark_is_zero(self):
        closes = _close_matrix()
        closes["SPY"] = closes["AAA"]
        f = pd.DataFrame({"symbol": ["AAA"], "date": [closes.index[5]], "value": [1.0]})
        panel, _ = build_return_panel(f, closes, benchmark="SPY")
        assert panel["fwd_5d"].iloc[0] == pytest.approx(0.0, abs=1e-12)

    def test_lag_pushes_entry_and_kills_tail(self):
        closes = _close_matrix()
        idx = closes.index
        f = pd.DataFrame({"symbol": ["AAA", "AAA"],
                          "date": [idx[5], idx[38]], "value": [1.0, 2.0]})
        panel, _ = build_return_panel(apply_lag(f, 3), closes, benchmark=None)
        # idx[5] + 3bd = idx[8] -> entry strictly after = idx[9]
        assert panel["entry_date"].iloc[0] == idx[9]
        # idx[38] + 3bd is past the data end -> no entry, all horizons NaN
        assert pd.isna(panel["entry_date"].iloc[1])
        assert panel[[f"fwd_{h}d" for h in HORIZONS]].iloc[1].isna().all()

    def test_benchmark_symbol_excluded(self):
        closes = _close_matrix()
        f = pd.DataFrame({"symbol": ["SPY"], "date": [closes.index[5]], "value": [1.0]})
        panel, dropped = build_return_panel(f, closes, benchmark="SPY")
        assert panel.empty
        assert "SPY" in dropped and "benchmark" in dropped["SPY"]

    def test_unknown_symbol_and_short_history_dropped(self):
        closes = _close_matrix()
        closes["SHT"] = np.nan
        closes.iloc[:10, closes.columns.get_loc("SHT")] = 50.0
        f = pd.DataFrame({"symbol": ["ZZZ", "SHT"],
                          "date": [closes.index[2]] * 2, "value": [1.0, 1.0]})
        panel, dropped = build_return_panel(f, closes, benchmark=None)
        assert dropped["ZZZ"] == "no price data"
        assert "history too short" in dropped["SHT"]

    def test_nonpositive_prices_masked(self):
        closes = _close_matrix()
        closes.iloc[9, closes.columns.get_loc("AAA")] = -1.0   # WTI-Apr-2020 class
        f = pd.DataFrame({"symbol": ["AAA"], "date": [closes.index[5]], "value": [1.0]})
        panel, _ = build_return_panel(f, closes, benchmark=None)
        # entry = idx[6]; h=3 exits at idx[9] (the bad close) -> masked to NaN
        assert pd.isna(panel["fwd_3d"].iloc[0])
        # h=1 exits at idx[7], untouched -> still a real return
        assert np.isfinite(panel["fwd_1d"].iloc[0])
