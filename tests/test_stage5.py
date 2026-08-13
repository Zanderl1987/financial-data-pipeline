"""
Tests for strategies/stage5.py -- the one-shot holdout runner. Uses
monkeypatched registry/catalog throughout so these never touch real data or
risk burning the actual one-shot holdout on a live strategy.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategies.stage5 as stage5  # noqa: E402


def _reg_df(rows):
    cols = ["run_id", "input_name", "input_type", "evaluation", "horizon",
            "statistic", "value", "n", "universe_hash", "date_range", "created_at"]
    return pd.DataFrame(rows, columns=cols)


def _catalog(fdr_pass_map):
    """{strategy_id: fdr_pass_bool_or_None}"""
    rows = []
    for sid, fdr_pass in fdr_pass_map.items():
        rows.append({"strategy_id": sid, "fdr_pass": fdr_pass,
                     "holdout_pnl_p": None, "holdout_run_ts": None, "stage": "stage4"})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ already_run

def test_already_run_true_when_registry_has_stage5_row(monkeypatch):
    reg = _reg_df([{"run_id": "r1", "input_name": "pine_foo", "input_type": "trade_rule",
                    "evaluation": "tv_strategy_catalog_stage5", "horizon": -1,
                    "statistic": "holdout_pnl_p", "value": 0.01, "n": 10,
                    "universe_hash": "h", "date_range": "2018..", "created_at": "t"}])
    monkeypatch.setattr(stage5.ev_registry, "load", lambda: reg)
    assert stage5.already_run("foo") is True
    assert stage5.already_run("bar") is False


def test_already_run_false_on_empty_registry(monkeypatch):
    monkeypatch.setattr(stage5.ev_registry, "load", lambda: _reg_df([]))
    assert stage5.already_run("foo") is False


# ------------------------------------------------------------------ stage4_survivors

def test_stage4_survivors_filters_to_fdr_pass_true(monkeypatch):
    catalog = _catalog({"a": True, "b": False, "c": None})
    monkeypatch.setattr(stage5, "build_catalog_rows", lambda: catalog)
    assert stage5.stage4_survivors() == ["a"]


def test_stage4_survivors_empty_when_no_fdr_column(monkeypatch):
    monkeypatch.setattr(stage5, "build_catalog_rows", lambda: pd.DataFrame({"strategy_id": ["a"]}))
    assert stage5.stage4_survivors() == []


# ------------------------------------------------------------------ run_holdout_for guard

def test_run_holdout_for_refuses_second_run(monkeypatch):
    monkeypatch.setattr(stage5, "already_run", lambda slug: True)
    with pytest.raises(RuntimeError, match="already has a Stage 5"):
        stage5.run_holdout_for("foo", cache={})


# ------------------------------------------------------------------ run_all guards

def test_run_all_previews_without_running_when_not_confirmed(monkeypatch, capsys):
    monkeypatch.setattr(stage5, "stage4_survivors", lambda: ["a", "b"])
    monkeypatch.setattr(stage5, "already_run", lambda slug: slug == "b")
    called = {"ran": False}

    def fake_run_holdout_for(*a, **k):
        called["ran"] = True

    monkeypatch.setattr(stage5, "run_holdout_for", fake_run_holdout_for)
    out = stage5.run_all(confirm=False)
    assert called["ran"] is False
    assert out.empty
    text = capsys.readouterr().out
    assert "confirm=True" in text or "--confirm-run" in text


def test_run_all_skips_already_tested_survivors(monkeypatch):
    monkeypatch.setattr(stage5, "stage4_survivors", lambda: ["a", "b"])
    monkeypatch.setattr(stage5, "already_run", lambda slug: slug == "a")
    monkeypatch.setattr(stage5, "holdout_cache", lambda: {"AAPL": pd.DataFrame()})
    monkeypatch.setattr(stage5.ev_registry, "universe_hash", lambda keys: "h")
    monkeypatch.setattr(stage5.ev_registry, "new_run_id", lambda: "run1")

    ran_slugs = []

    def fake_run_holdout_for(slug, cache, n_perm=None, seed=None):
        ran_slugs.append(slug)
        return {"strategy_id": slug, "translation_verified": "unverified",
                "holdout_n_trades": 5, "holdout_total_pnl_net": 100.0,
                "holdout_pnl_p": 0.02, "holdout_run_ts": "2026-08-13T00:00:00Z",
                "holdout_success": True}

    appended = []
    monkeypatch.setattr(stage5, "run_holdout_for", fake_run_holdout_for)
    monkeypatch.setattr(stage5.ev_registry, "append", lambda rows: appended.append(rows))
    monkeypatch.setattr(stage5, "build_catalog_rows",
                        lambda: pd.DataFrame({"strategy_id": ["a", "b"], "stage": ["stage4"] * 2,
                                             "holdout_pnl_p": [None, None],
                                             "holdout_run_ts": [None, None]}))
    written = {}
    monkeypatch.setattr(stage5, "write_catalog_table", lambda df: written.setdefault("df", df))

    out = stage5.run_all(confirm=True)

    assert ran_slugs == ["b"]  # "a" already run, skipped
    assert list(out["strategy_id"]) == ["b"]
    assert len(appended) == 1
    assert set(appended[0]["evaluation"]) == {"tv_strategy_catalog_stage5"}
    assert written["df"].set_index("strategy_id").loc["b", "stage"] == "stage5"
    assert written["df"].set_index("strategy_id").loc["a", "stage"] == "stage4"  # untouched


def test_run_all_no_op_when_nothing_left_to_run(monkeypatch, capsys):
    monkeypatch.setattr(stage5, "stage4_survivors", lambda: ["a"])
    monkeypatch.setattr(stage5, "already_run", lambda slug: True)
    out = stage5.run_all(confirm=True)
    assert out.empty
    assert "No untested" in capsys.readouterr().out
