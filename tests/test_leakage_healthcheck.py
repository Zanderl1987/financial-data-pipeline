"""
tests/test_leakage_healthcheck.py -- leakage_healthcheck.py's own logic
(roster iteration, flagging, exit code), independent of any real event
study. entry_lag_leakage() itself is covered by tests/test_leakage_probe.py;
this file only checks the script wraps it correctly.
"""

import pandas as pd
import pytest

import leakage_healthcheck as lh


class TestRunOne:
    def test_empty_events_gives_reason_not_error(self):
        out = lh.run_one("empty", lambda: pd.DataFrame(columns=["symbol", "date"]), {})
        assert out["n_events"] == 0
        assert out["reason"] == "no events"

    def test_passes_scenario_kwargs_through(self, monkeypatch):
        captured = {}

        def fake_entry_lag_leakage(events, **kwargs):
            captured.update(kwargs)
            return {"switch": "entry_lag", "safe_value": 1, "leaky_value": 0,
                   "safe_metric": 0.1, "leaky_metric": 0.2, "inflation": 0.1}

        monkeypatch.setattr(lh.lp, "entry_lag_leakage", fake_entry_lag_leakage)
        events = pd.DataFrame({"symbol": ["AAPL"], "date": [pd.Timestamp("2024-01-01")]})
        out = lh.run_one("t", lambda: events, dict(holding_days=5, price_table="prices"))
        assert captured == {"holding_days": 5, "price_table": "prices"}
        assert out["n_events"] == 1
        assert out["inflation"] == 0.1


class TestMain:
    def _patch_roster(self, monkeypatch, roster):
        monkeypatch.setattr(lh, "ROSTER", roster)

    def test_no_flags_returns_zero(self, monkeypatch, capsys):
        events = pd.DataFrame({"symbol": ["AAPL"], "date": [pd.Timestamp("2024-01-01")]})
        self._patch_roster(monkeypatch, {
            "clean_study": (lambda: events, {}),
        })
        monkeypatch.setattr(lh.lp, "entry_lag_leakage",
                            lambda events, **kw: {"safe_metric": 1.0,
                                                  "leaky_metric": 1.02,
                                                  "inflation": 0.02})
        rc = lh.main(["--threshold", "0.10"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[ok]" in out
        assert "no entry-lag leakage flagged" in out

    def test_inflation_above_threshold_flags_and_returns_one(self, monkeypatch, capsys):
        events = pd.DataFrame({"symbol": ["AAPL"], "date": [pd.Timestamp("2024-01-01")]})
        self._patch_roster(monkeypatch, {
            "leaky_study": (lambda: events, {}),
        })
        monkeypatch.setattr(lh.lp, "entry_lag_leakage",
                            lambda events, **kw: {"safe_metric": 0.1,
                                                  "leaky_metric": 5.0,
                                                  "inflation": 4.9})
        rc = lh.main(["--threshold", "0.10"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "[FLAG]" in out

    def test_empty_events_is_skipped_not_flagged(self, monkeypatch, capsys):
        self._patch_roster(monkeypatch, {
            "empty_study": (lambda: pd.DataFrame(columns=["symbol", "date"]), {}),
        })
        rc = lh.main([])
        out = capsys.readouterr().out
        assert rc == 0
        assert "SKIP (no events)" in out

    def test_probe_error_flags_without_crashing(self, monkeypatch, capsys):
        def boom():
            raise RuntimeError("no data source")
        self._patch_roster(monkeypatch, {"broken_study": (boom, {})})
        rc = lh.main([])
        out = capsys.readouterr().out
        assert rc == 1
        assert "ERROR RuntimeError: no data source" in out

    def test_none_inflation_is_not_flagged(self, monkeypatch, capsys):
        events = pd.DataFrame({"symbol": ["AAPL"], "date": [pd.Timestamp("2024-01-01")]})
        self._patch_roster(monkeypatch, {
            "flat_study": (lambda: events, {}),
        })
        monkeypatch.setattr(lh.lp, "entry_lag_leakage",
                            lambda events, **kw: {"safe_metric": None,
                                                  "leaky_metric": None,
                                                  "inflation": None})
        rc = lh.main([])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[ok]" in out
