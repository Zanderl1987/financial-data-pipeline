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
from strategies.stage3 import cost_config as stage3_cost_config  # noqa: E402


def _reg_df(rows):
    cols = ["run_id", "input_name", "input_type", "evaluation", "horizon",
            "statistic", "value", "n", "universe_hash", "date_range", "created_at"]
    return pd.DataFrame(rows, columns=cols)


def _catalog(fdr_pass_map, pnl_map=None, mismatch_map=None):
    """{strategy_id: fdr_pass_bool_or_None}; dev net P&L defaults to +1000 and
    exposure_mismatch to False, so only the FDR column varies unless asked."""
    rows = []
    for sid, fdr_pass in fdr_pass_map.items():
        rows.append({"strategy_id": sid, "fdr_pass": fdr_pass,
                     "total_pnl_net": (pnl_map or {}).get(sid, 1000.0),
                     "exposure_mismatch": (mismatch_map or {}).get(sid, False),
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


def test_holdout_opened_under_voided_endpoint_stays_spent(monkeypatch):
    """Amendment 2026-10-28 item 5: a pre-amendment Stage 5 row means the
    holdout was SEEN, so the v2 runner must refuse it too."""
    reg = _reg_df([{"run_id": "r", "input_name": "pine_old", "input_type": "trade_rule",
                    "evaluation": "tv_strategy_catalog_stage5", "horizon": -1,
                    "statistic": "holdout_pnl_p", "value": 0.0099, "n": 1,
                    "universe_hash": "u", "date_range": "2018-01-01..",
                    "created_at": "2026-09-07T12:13:15Z"}])
    monkeypatch.setattr(stage5.ev_registry, "load", lambda: reg)
    assert stage5.already_run("old") is True
    with pytest.raises(RuntimeError, match="already has a Stage 5"):
        stage5.run_holdout_for("old", cache={})


def test_already_run_false_on_empty_registry(monkeypatch):
    monkeypatch.setattr(stage5.ev_registry, "load", lambda: _reg_df([]))
    assert stage5.already_run("foo") is False


# ------------------------------------------------------------------ stage4_survivors

def test_stage4_survivors_filters_to_fdr_pass_true(monkeypatch):
    catalog = _catalog({"a": True, "b": False, "c": None})
    monkeypatch.setattr(stage5, "build_catalog_rows", lambda: catalog)
    assert stage5.stage4_survivors() == ["a"]


def test_stage4_survivors_need_dev_profit_and_matched_exposure(monkeypatch):
    """Amendment 2026-10-28 items 2-3: an FDR pass alone no longer advances."""
    catalog = _catalog({"ok": True, "loses": True, "flat": True, "mismatch": True},
                       pnl_map={"loses": -5.0, "flat": 0.0},
                       mismatch_map={"mismatch": True})
    monkeypatch.setattr(stage5, "build_catalog_rows", lambda: catalog)
    assert stage5.stage4_survivors() == ["ok"]


def test_stage4_survivors_empty_when_no_fdr_column(monkeypatch):
    monkeypatch.setattr(stage5, "build_catalog_rows", lambda: pd.DataFrame({"strategy_id": ["a"]}))
    assert stage5.stage4_survivors() == []


# ------------------------------------------------------------------ run_holdout_for guard

def test_run_holdout_for_refuses_second_run(monkeypatch):
    monkeypatch.setattr(stage5, "already_run", lambda slug: True)
    with pytest.raises(RuntimeError, match="already has a Stage 5"):
        stage5.run_holdout_for("foo", cache={})


# ------------------------------------------------------------------ dead-company re-score gate

def _rescore_row(slug, value, created="t1", statistic="rescore_pass"):
    return {"run_id": "r", "input_name": f"pine_{slug}", "input_type": "trade_rule",
            "evaluation": "tv_strategy_catalog_survivorship_v1", "horizon": -1, "statistic": statistic,
            "value": value, "n": 1, "universe_hash": "u", "date_range": "x", "created_at": created}


def test_rescore_passed_reads_the_registry(monkeypatch):
    reg = _reg_df([_rescore_row("yes", 1.0), _rescore_row("no", 0.0), _rescore_row("other", 1.0, statistic="pnl_p")])
    monkeypatch.setattr(stage5.ev_registry, "load", lambda: reg)
    assert stage5.rescore_passed("yes") is True
    assert stage5.rescore_passed("no") is False
    assert stage5.rescore_passed("other") is False
    assert stage5.rescore_passed("never_rescored") is False


def test_a_failed_rescore_is_final_even_with_a_later_pass_row(monkeypatch):
    """Change 12: failure is final, so a later row cannot rescue a strategy."""
    reg = _reg_df([_rescore_row("x", 0.0, "t1"), _rescore_row("x", 1.0, "t2")])
    monkeypatch.setattr(stage5.ev_registry, "load", lambda: reg)
    assert stage5.rescore_passed("x") is False


def test_run_all_only_tests_strategies_that_passed_the_rescore(monkeypatch, capsys):
    monkeypatch.setattr(stage5, "stage4_survivors", lambda: ["a", "b"])
    monkeypatch.setattr(stage5, "already_run", lambda slug: False)
    monkeypatch.setattr(stage5, "rescore_passed", lambda slug: slug == "b")
    out = stage5.run_all(confirm=False)
    assert out.empty
    assert "['b']" in capsys.readouterr().out


def test_nothing_runs_when_no_strategy_passed_the_rescore(monkeypatch, capsys):
    monkeypatch.setattr(stage5, "stage4_survivors", lambda: ["a"])
    monkeypatch.setattr(stage5, "already_run", lambda slug: False)
    monkeypatch.setattr(stage5, "rescore_passed", lambda slug: False)
    monkeypatch.setattr(stage5, "crsp_holdout_panel", lambda: pytest.fail("holdout prices must stay closed"))
    assert stage5.run_all(confirm=True).empty
    assert "No untested" in capsys.readouterr().out


def test_crsp_holdout_panel_loads_the_holdout_panel(monkeypatch):
    calls = {}
    monkeypatch.setattr(stage5.sv, "load_holdout_panel", lambda ranges: calls.setdefault("panel", object()))
    monkeypatch.setattr(stage5, "stage3_ranges", lambda: {"AAPL": None})
    assert stage5.crsp_holdout_panel() is calls["panel"]


def test_run_holdout_for_refuses_a_strategy_that_did_not_pass_the_rescore(monkeypatch):
    """Older scripts call this directly, so the gate must hold here and not only in run_all."""
    monkeypatch.setattr(stage5, "already_run", lambda slug: False)
    monkeypatch.setattr(stage5, "rescore_passed", lambda slug: False)
    monkeypatch.setattr(stage5.sv, "score_panel", lambda *a, **k: pytest.fail("holdout prices must stay closed"))
    with pytest.raises(RuntimeError, match="re-score"):
        stage5.run_holdout_for("s", cache="panel")


def test_run_holdout_for_scores_the_panel_and_needs_both_scenarios(monkeypatch):
    monkeypatch.setattr(stage5, "already_run", lambda slug: False)
    monkeypatch.setattr(stage5, "rescore_passed", lambda slug: True)
    monkeypatch.setattr(stage5, "load_rule_for", lambda slug: ("rule", "unit_tested"))
    monkeypatch.setattr(stage5.sv, "crsp_config", lambda: "cfg")
    seen = {}

    def fake_score(rules, panel, **kw):
        seen.update(rules=rules, panel=panel, **kw)
        return {"s": {"shumway": {"pnl_p": 0.01, "obs_pnl_dollars": 50.0, "n_trades": 7, "null_exposure_ratio": 1.0},
                      "missing_wipeout": {"pnl_p": 0.2, "obs_pnl_dollars": 40.0, "n_trades": 7}}}

    monkeypatch.setattr(stage5.sv, "score_panel", fake_score)
    row = stage5.run_holdout_for("s", cache="panel", n_perm=3, seed=1)
    assert seen["rules"] == {"s": "rule"}             # no adjusted-price floor wrapped around the rule
    assert seen["panel"] == "panel" and seen["config"] == "cfg" and seen["n_perm"] == 3
    assert row["holdout_pnl_p"] == 0.01
    assert row["holdout_wipeout_pnl_p"] == 0.2
    assert row["holdout_success"] is False


# ------------------------------------------------------------------ run_all guards

def test_run_all_previews_without_running_when_not_confirmed(monkeypatch, capsys):
    monkeypatch.setattr(stage5, "stage4_survivors", lambda: ["a", "b"])
    monkeypatch.setattr(stage5, "already_run", lambda slug: slug == "b")
    monkeypatch.setattr(stage5, "rescore_passed", lambda slug: True)
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
    monkeypatch.setattr(stage5, "rescore_passed", lambda slug: True)
    monkeypatch.setattr(stage5.sv, "crsp_config", lambda: stage3_cost_config(10.0))
    monkeypatch.setattr(stage5, "crsp_holdout_panel", lambda: {"AAPL": pd.DataFrame()})
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
    assert set(appended[0]["evaluation"]) == {"tv_strategy_catalog_stage5_v2"}
    assert written["df"].set_index("strategy_id").loc["b", "stage"] == "stage5"
    assert written["df"].set_index("strategy_id").loc["a", "stage"] == "stage4"  # untouched


def test_run_all_no_op_when_nothing_left_to_run(monkeypatch, capsys):
    monkeypatch.setattr(stage5, "stage4_survivors", lambda: ["a"])
    monkeypatch.setattr(stage5, "already_run", lambda slug: True)
    monkeypatch.setattr(stage5, "rescore_passed", lambda slug: True)
    out = stage5.run_all(confirm=True)
    assert out.empty
    assert "No untested" in capsys.readouterr().out
