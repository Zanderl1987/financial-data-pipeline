"""
Tests for strategies/stage4.py -- campaign-wide BH-FDR close. Uses synthetic
catalogs throughout (monkeypatched build_catalog_rows/registry.append/
write_catalog_table) so these never touch the real eval_registry or catalog
snapshot -- this campaign's real close is a one-shot, user-owned decision.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategies.stage4 as stage4  # noqa: E402


def _catalog(pnl_ps):
    """pnl_ps: {strategy_id: pnl_p_or_None}"""
    rows = []
    for sid, p in pnl_ps.items():
        row = {c: None for c in [
            "strategy_id", "tv_url", "tv_author", "tv_script_name", "tv_boosts",
            "tv_views", "collected_at", "license", "mechanism_family",
            "param_count", "screen_status", "excluded_reason",
            "translation_verified", "n_trades", "win_rate", "profit_factor",
            "sharpe", "max_dd", "turnover", "median_hold", "total_pnl_net",
            "pnl_p", "pnl_p_5bps", "pnl_p_20bps", "cost_fragile", "bh_q",
            "fdr_pass", "holdout_pnl_p", "holdout_run_ts", "provisional",
            "stage", "run_id", "git_commit", "fetched_at",
        ]}
        row["strategy_id"] = sid
        row["pnl_p"] = p
        row["provisional"] = True
        row["stage"] = "stage3" if p is not None else "stage2"
        rows.append(row)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ compute_fdr

def test_compute_fdr_excludes_untested_strategies_from_m():
    catalog = _catalog({"a": 0.01, "b": None, "c": None})
    result = stage4.compute_fdr(catalog)
    tested = result[result["strategy_id"] == "a"].iloc[0]
    assert tested["p_adj"] == pytest.approx(0.01)  # m=1 -> p_adj == p


def test_compute_fdr_rejects_strong_signal_among_weak_ones():
    catalog = _catalog({"a": 0.001, "b": 0.5, "c": 0.8, "d": 0.9, "e": 0.95})
    result = stage4.compute_fdr(catalog)
    a = result[result["strategy_id"] == "a"].iloc[0]
    assert bool(a["reject"]) is True
    others = result[result["strategy_id"] != "a"]
    assert not others["reject"].any()


def test_compute_fdr_no_survivors_when_all_null():
    catalog = _catalog({"a": 0.6, "b": 0.7, "c": 0.8})
    result = stage4.compute_fdr(catalog)
    assert not result["reject"].any()


# ------------------------------------------------------------------ preview

def test_preview_warns_below_min_campaign_size(capsys):
    catalog = _catalog({f"s{i}": 0.5 for i in range(5)})
    stage4.preview(catalog)
    out = capsys.readouterr().out
    assert "stopping rule" in out.lower()


def test_preview_reports_tested_vs_total(capsys):
    catalog = _catalog({"a": 0.1, "b": None, "c": None})
    stage4.preview(catalog)
    out = capsys.readouterr().out
    assert "1/3" in out


# ------------------------------------------------------------------ run_close guards

def test_run_close_refuses_without_confirm():
    with pytest.raises(SystemExit, match="confirm"):
        stage4.run_close(confirm=False)


def test_run_close_refuses_with_missing_stage3_results(monkeypatch):
    catalog = _catalog({"a": 0.1, "b": None})
    monkeypatch.setattr(stage4, "build_catalog_rows", lambda: catalog)
    with pytest.raises(SystemExit, match="no Stage 3 result"):
        stage4.run_close(confirm=True)


def test_run_close_writes_registry_and_catalog_when_complete(monkeypatch):
    catalog = _catalog({"a": 0.001, "b": 0.6, "c": 0.7})
    monkeypatch.setattr(stage4, "build_catalog_rows", lambda: catalog)

    written = {}

    def fake_write_catalog_table(df):
        written["df"] = df
        return "fake/path.parquet"

    appended = {}

    def fake_append(rows):
        appended["rows"] = rows

    monkeypatch.setattr(stage4, "write_catalog_table", fake_write_catalog_table)
    monkeypatch.setattr(stage4.ev_registry, "append", fake_append)
    monkeypatch.setattr(stage4.ev_registry, "new_run_id", lambda: "test-run-id")

    out = stage4.run_close(confirm=True)

    assert "df" in written
    assert (out["provisional"] == False).all()  # noqa: E712
    assert (out["stage"] == "stage4").all()
    a_row = out[out["strategy_id"] == "a"].iloc[0]
    assert bool(a_row["fdr_pass"]) is True

    assert "rows" in appended
    reg = appended["rows"]
    assert set(reg["evaluation"]) == {"tv_strategy_catalog_stage4"}
    assert set(reg["statistic"]) == {"bh_q", "fdr_pass"}
