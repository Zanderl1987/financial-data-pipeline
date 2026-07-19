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
