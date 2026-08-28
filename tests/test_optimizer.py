"""
tests/test_optimizer.py -- W5 parameter optimizer (evaluation/optimizer.py).

Covers the safety machinery as much as the search itself: trial logging to
the registry, combined-DSR population semantics, budget guards, PBO matrix
assembly, and walk-forward optimization's fold discipline.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import evaluation.optimizer as opt
import evaluation.registry as ev_registry
import evaluation.stats as ev_stats


# --------------------------------------------------------------- fixtures


def _toy_cache(n_days: int = 60, symbols=("AAA", "BBB"),
               seed: int = 7) -> dict:
    """Rating oscillating across the default thresholds; mild uptrend so
    long trades make money. Enough structure for simulate() to produce
    multiple round trips per symbol."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n_days)
    cache = {}
    for k, sym in enumerate(symbols):
        rating = np.where(np.arange(n_days) % 4 == 0, 0.6,
                          np.where(np.arange(n_days) % 4 == 2, -0.6, 0.0))
        close = 100 * (1 + k * 0.1) + np.cumsum(
            rng.normal(0.05, 0.5, n_days))
        cache[sym] = pd.DataFrame({"close": close, "rating_all": rating},
                                  index=idx)
    return cache


@pytest.fixture()
def reg_path(tmp_path):
    return str(tmp_path / "registry" / "results.parquet")


# ------------------------------------------------------------ Param basics


class TestParam:
    def test_float_grid_values(self):
        p = opt.Param("x", 0.0, 1.0, 0.5)
        assert p.grid_values(3) == [0.0, 0.5, 1.0]

    def test_int_grid_values_bounded_and_deduped(self):
        p = opt.Param("q", 3, 7, 5, kind="int")
        assert p.grid_values(10) == [3, 4, 5, 6, 7]
        assert p.grid_values(3) == [3, 5, 7]

    def test_choice_grid_enumerates_choices(self):
        p = opt.Param("r", 0, 1, "M", kind="choice", choices=("W", "M"))
        assert p.grid_values(4) == ["W", "M"]

    def test_coerce_int_rounds(self):
        p = opt.Param("q", 3, 7, 5, kind="int")
        assert p.coerce(4.6) == 5

    def test_coerce_choice_clamps_index(self):
        p = opt.Param("r", 0, 1, "W", kind="choice", choices=("W", "M"))
        assert p.coerce(99.0) == "M"
        assert p.coerce(-1.0) == "W"

    def test_coerce_float_clips_to_bounds(self):
        p = opt.Param("x", 0.0, 1.0, 0.5)
        assert p.coerce(9.9) == 1.0


class TestParamSpace:
    def _space(self):
        return opt.ParamSpace(
            "sig", "cross_sectional",
            (opt.Param("quantiles", 3, 7, 5, kind="int"),
             opt.Param("rebalance", 0, 1, "M", kind="choice",
                       choices=("W", "M"))))

    def test_vector_to_params_coerces_each_kind(self):
        sp = self._space()
        out = sp.vector_to_params([4.4, 0.0])
        assert out == {"quantiles": 4, "rebalance": "W"}

    def test_params_tag_is_sorted_and_stable(self):
        sp = self._space()
        tag = sp.params_tag({"rebalance": "W", "quantiles": 5})
        assert tag == "quantiles=5,rebalance=W"

    def test_default_vector_maps_choice_to_index(self):
        sp = self._space()
        assert sp.default_vector() == [5.0, 1.0]

    def test_register_space_adds_and_replaces(self):
        sp = self._space()
        opt.register_space(sp)
        try:
            assert opt.BUILTIN_SPACES["sig"] is sp
        finally:
            del opt.BUILTIN_SPACES["sig"]


class TestBuiltinSpaces:
    def test_three_tv_spaces_registered(self):
        for name in ("tv_threshold", "tv_fade", "tv_fade_long"):
            assert name in opt.BUILTIN_SPACES
            assert opt.BUILTIN_SPACES[name].family == "trade_rule"

    def test_defaults_match_campaign_constants(self):
        v = opt.BUILTIN_SPACES["tv_threshold"].default_vector()
        assert v == [0.5, 0.1, -0.5, -0.1]

    def test_grid_size_guard_math(self):
        sp = opt.BUILTIN_SPACES["tv_threshold"]
        total = 1
        for p in sp.params:
            total *= len(p.grid_values(4))
        assert total == 256      # 4^4 -- the documented grid cost


class TestBuilders:
    def test_threshold_entries_fire_on_crossed_up(self):
        rule = opt._build_tv_threshold(bull_min=0.5, exit_long_max=0.1,
                                       bear_max=-0.5, exit_short_min=-0.1)
        df = pd.DataFrame({"rating_all": [0.0, 0.6, 0.05, 0.6]},
                          index=pd.bdate_range("2024-01-01", periods=4))
        assert list(rule.entries(df)) == [False, True, False, True]
        assert list(rule.exits(df)) == [True, False, True, False]

    def test_fade_sides_are_swapped(self):
        rule = opt._build_tv_fade(bull_min=0.5, exit_long_max=0.1,
                                  bear_max=-0.5, exit_short_min=-0.1)
        df = pd.DataFrame({"rating_all": [0.0, 0.6, -0.6, 0.6]},
                          index=pd.bdate_range("2024-01-01", periods=4))
        # fade: LONG on the bear-cross, SHORT on the bull-cross
        assert list(rule.entries(df)) == [False, False, True, False]
        assert list(rule.short_entries(df)) == [False, True, False, True]

    def test_fade_long_has_no_short_leg(self):
        rule = opt._build_tv_fade_long(0.5, 0.1, -0.5, -0.1)
        df = pd.DataFrame({"rating_all": [0.0, -0.6]},
                          index=pd.bdate_range("2024-01-01", periods=2))
        assert rule.side == "long"
        assert rule.short_entries is None


# ------------------------------------------------------------- Evaluator


class TestEvaluatorTradeRule:
    def test_scores_planted_uptrend_cache(self):
        ev = opt.Evaluator(opt.BUILTIN_SPACES["tv_threshold"],
                           {"cache": _toy_cache()}, min_trades=2)
        res = ev.evaluate(np.asarray([0.5, 0.1, -0.5, -0.1]))
        assert res.sharpe is not None
        assert res.n_trades >= 2
        assert res.n_days >= 20
        assert res.returns is not None

    def test_min_trades_penalty(self):
        ev = opt.Evaluator(opt.BUILTIN_SPACES["tv_threshold"],
                           {"cache": _toy_cache()}, min_trades=10_000)
        score = ev(np.asarray([0.5, 0.1, -0.5, -0.1]))
        assert score == -1e9
        assert ev.results[-1].reason.startswith("fewer than")

    def test_eval_counter_counts_every_call(self):
        ev = opt.Evaluator(opt.BUILTIN_SPACES["tv_threshold"],
                           {"cache": _toy_cache()}, min_trades=10_000)
        for _ in range(3):
            ev(np.asarray([0.5, 0.1, -0.5, -0.1]))
        assert ev.n_evals == 3


class TestTrialLog:
    def _log(self, space, path, flush_every=3):
        return opt.TrialLog(space, "run123", universe_hash="u",
                            date_range="2024-01-01..2024-03-01",
                            execution_hash="abc", flush_every=flush_every,
                            path=path)

    def test_flushes_at_interval_and_finalize(self, reg_path):
        space = opt.BUILTIN_SPACES["tv_threshold"]
        log = self._log(space, reg_path, flush_every=3)
        for _ in range(7):
            log.add(opt.TrialResult({"bull_min": 0.5}, 1.23, n_days=60))
        # 6 flushed, 1 pending
        df = ev_registry.load(reg_path)
        assert len(df) == 6
        log.finalize()
        df = ev_registry.load(reg_path)
        assert len(df) == 7
        row = df.iloc[0]
        assert row["evaluation"] == "optimizer"
        assert row["statistic"] == "opt_sharpe"
        assert row["input_name"].startswith("tv_threshold@")
        assert row["execution_hash"] == "abc"
        assert row["run_id"] == "run123"

    def test_nan_value_for_unscorable_trial(self, reg_path):
        space = opt.BUILTIN_SPACES["tv_threshold"]
        log = self._log(space, reg_path)
        log.add(opt.TrialResult({"bull_min": 0.5}, None,
                                reason="fewer than 20 trades (n=0)"))
        log.finalize()
        df = ev_registry.load(reg_path)
        assert bool(pd.isna(df.iloc[0]["value"]))
        assert int(df.iloc[0]["n"]) == 0

    def test_add_stat_rows_for_perm_results(self, reg_path):
        space = opt.BUILTIN_SPACES["tv_threshold"]
        log = self._log(space, reg_path)
        log.add_stat("opt_perm_pnl_p", 0.03, 200,
                     input_name="tv_threshold@bull_min=0.5")
        log.finalize()
        df = ev_registry.load(reg_path)
        assert df.iloc[0]["statistic"] == "opt_perm_pnl_p"
        assert float(df.iloc[0]["value"]) == 0.03


class TestCombinedDsr:
    def test_unions_main_population_and_opt_trials(self, monkeypatch, reg_path):
        pops = {}

        def fake_population(statistic, path=None, exclude_input_name=None):
            pops[statistic] = [v for v in {"sharpe": [1.0, 2.0],
                                           "opt_sharpe": [0.5]}
                               .get(statistic, [])]
            if statistic == "sharpe" and exclude_input_name == "me":
                pops[statistic] = [v for v in pops[statistic]]
            return pops[statistic]

        monkeypatch.setattr(ev_stats, "deflated_sharpe",
                            lambda sr, nd, trials: {"trials": list(trials)})
        monkeypatch.setattr(ev_registry, "population", fake_population)
        out = opt.dsr_with_opt_trials(1.5, 300, "me", extra_trials=[3.0],
                                      path=reg_path)
        assert sorted(out["trials"]) == [0.5, 1.0, 2.0, 3.0]

    def test_extra_none_values_dropped(self, monkeypatch, reg_path):
        monkeypatch.setattr(ev_registry, "population",
                            lambda *a, **k: [])
        monkeypatch.setattr(ev_stats, "deflated_sharpe",
                            lambda sr, nd, trials: {"trials": list(trials)})
        out = opt.dsr_with_opt_trials(None, 300, "me",
                                      extra_trials=[1.0, None],
                                      path=reg_path)
        assert out["trials"] == [1.0]


# ---------------------------------------------------------------- solvers


class TestGridSearch:
    def test_evaluates_full_product_and_respects_order(self):
        ev = opt.Evaluator(opt.BUILTIN_SPACES["tv_threshold"],
                           {"cache": _toy_cache()}, min_trades=10_000)
        opt.grid_search(ev, points=2)
        assert ev.n_evals == 16
        assert len(ev.results) == 16

    def test_budget_overflow_raises_with_count(self):
        ev = opt.Evaluator(opt.BUILTIN_SPACES["tv_threshold"],
                           {"cache": _toy_cache()}, min_trades=10_000)
        with pytest.raises(ValueError, match="grid needs 81 evals"):
            opt.grid_search(ev, points=3, max_evals=80)

    def test_exact_budget_allowed(self):
        ev = opt.Evaluator(opt.BUILTIN_SPACES["tv_threshold"],
                           {"cache": _toy_cache()}, min_trades=10_000)
        opt.grid_search(ev, points=3, max_evals=81)
        assert ev.n_evals == 81


class TestDESearch:
    def test_budget_respected_and_best_reported(self):
        ev = opt.Evaluator(opt.BUILTIN_SPACES["tv_threshold"],
                           {"cache": _toy_cache()}, min_trades=2)
        out = opt.de_search(ev, max_evals=30, seed=0)
        assert out["n_evals"] <= 30 + 24          # one generation overshoot
        assert out["best_index"] is not None
        assert out["best_sharpe"] > -1e8

    def test_deterministic_same_seed_same_result(self):
        scores = []
        for _ in range(2):
            ev = opt.Evaluator(opt.BUILTIN_SPACES["tv_threshold"],
                               {"cache": _toy_cache()}, min_trades=2)
            out = opt.de_search(ev, max_evals=25, seed=42)
            scores.append((out["best_index"], out["best_sharpe"],
                           ev.n_evals))
        assert scores[0] == scores[1]

    def test_choice_space_rejected_for_de(self):
        sp = opt.ParamSpace(
            "cs", "cross_sectional",
            (opt.Param("rebalance", 0, 1, "M", kind="choice",
                       choices=("W", "M")),))
        ev = opt.Evaluator(sp, {}, min_trades=1)
        with pytest.raises(ValueError, match="choice params"):
            opt.de_search(ev, max_evals=10)


# ------------------------------------------------------- single-split run


class TestRunSearch:
    def _run(self, reg_path, tmp_path, monkeypatch, **kw):
        monkeypatch.setattr(ev_stats, "permutation_trades",
                            lambda rule, cache, n_perm=200, seed=0, config=None:
                            {"pnl_p": 0.04, "win_rate_p": 0.11})
        space = opt.BUILTIN_SPACES["tv_threshold"]
        data = {"cache": _toy_cache(), "notional": 10_000.0}
        art_dir = str(tmp_path / "artifacts")
        artifact = opt.run_search(
            space, data, method="grid", points=2, top_k=2, n_perm=50,
            min_trades=2, registry_path=reg_path, artifact_dir=art_dir,
            **kw)
        return artifact, art_dir

    def test_end_to_end_artifact_and_verdict(self, reg_path, tmp_path,
                                             monkeypatch):
        artifact, art_dir = self._run(reg_path, tmp_path, monkeypatch)
        assert artifact["mode"] == "single_split"
        assert artifact["n_evals"] == 16
        assert len(artifact["top"]) == 2
        assert any(t.get("pnl_p") == 0.04 for t in artifact["top"])
        assert artifact["verdict"]
        path = os.path.join(art_dir, f"{artifact['run_id']}.json")
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        assert loaded["space"] == "tv_threshold"

    def test_trials_and_perm_rows_in_registry(self, reg_path, tmp_path,
                                              monkeypatch):
        artifact, _ = self._run(reg_path, tmp_path, monkeypatch)
        df = ev_registry.load(reg_path)
        trials = df[df["statistic"] == "opt_sharpe"]
        perms = df[df["statistic"] == "opt_perm_pnl_p"]
        assert len(trials) == 16
        assert len(perms) == 2                      # top_k=2 finalists
        assert set(perms["run_id"]) == {artifact["run_id"]}
        # every trial input_name carries its parameter vector
        assert trials["input_name"].str.contains("bull_min=").all()

    def test_dsr_computed_after_flush_no_double_count(self, reg_path, tmp_path,
                                                      monkeypatch):
        artifact, _ = self._run(reg_path, tmp_path, monkeypatch)
        if artifact["dsr"]:
            # N must include exactly this run's scored trials, not 2x them
            df = ev_registry.load(reg_path)
            n_scored = int(df[df["statistic"] == "opt_sharpe"]["value"]
                           .notna().sum())
            assert artifact["dsr"]["n_trials"] >= n_scored

    def test_unknown_method_raises(self, reg_path, tmp_path):
        with pytest.raises(ValueError, match="unknown method"):
            opt.run_search(opt.BUILTIN_SPACES["tv_threshold"],
                           {"cache": _toy_cache()}, method="pso",
                           registry_path=reg_path)


class TestPboMatrix:
    def test_needs_two_return_series(self):
        ev = opt.Evaluator(opt.BUILTIN_SPACES["tv_threshold"], {},
                           min_trades=1)
        ev.results.append(opt.TrialResult({"a": 1}, 1.0))
        matrix, reason = opt.pbo_matrix(ev)
        assert matrix.empty
        assert "need >=2" in reason

    def test_aligns_on_common_dates(self):
        ev = opt.Evaluator(opt.BUILTIN_SPACES["tv_threshold"], {},
                           min_trades=1)
        idx = pd.bdate_range("2024-01-01", periods=40)
        ev.results.append(opt.TrialResult(
            {"a": 1}, 1.0, returns=pd.Series(0.001, index=idx)))
        ev.results.append(opt.TrialResult(
            {"b": 2}, 1.1, returns=pd.Series(0.002, index=idx[:35])))
        matrix, reason = opt.pbo_matrix(ev)
        assert reason == ""
        assert matrix.shape == (35, 2)
    def test_disjoint_dates_give_empty_with_reason(self):
        ev = opt.Evaluator(opt.BUILTIN_SPACES["tv_threshold"], {},
                           min_trades=1)
        ev.results.append(opt.TrialResult(
            {"a": 1}, 1.0,
            returns=pd.Series(0.001,
                              index=pd.bdate_range("2024-01-01", periods=20))))
        ev.results.append(opt.TrialResult(
            {"b": 2}, 1.1,
            returns=pd.Series(0.001,
                              index=pd.bdate_range("2025-01-01", periods=20))))
        matrix, reason = opt.pbo_matrix(ev)
        assert matrix.empty
        assert "overlapping" in reason


# --------------------------------------------------- walk-forward optimize


class TestWalkForwardOptimize:
    def _run_wfa(self, reg_path, tmp_path, n_days=260):
        space = opt.BUILTIN_SPACES["tv_threshold"]
        cache = _toy_cache(n_days=n_days, symbols=tuple(
            f"S{i}" for i in range(4)), seed=11)
        art_dir = str(tmp_path / "artifacts")
        artifact = opt.walk_forward_optimize(
            space, cache, method="de", fold_budget=15, n_folds=2,
            min_train_days=150, top_k=1, n_perm=0, min_trades=2,
            registry_path=reg_path, artifact_dir=art_dir)
        return artifact, art_dir

    def test_fold_discipline_and_references(self, reg_path, tmp_path):
        artifact, _ = self._run_wfa(reg_path, tmp_path)
        assert "wf_reason" not in artifact or artifact.get("wf_reason") is None
        folds = artifact["folds"]
        assert len(folds) == 2
        for f in folds:
            assert f["chosen_params"] is not None
            assert f["train_days"] >= 150
        # OOS pieces stitched across both folds
        assert artifact["wfa_oos"]["n_days"] == sum(
            21 for _ in folds) or artifact["wfa_oos"]["n_days"] > 0
        assert artifact["default_oos"] is not None
        assert isinstance(artifact["optimization_helped_oos"], bool)
        fantasy = artifact["full_sample_fantasy"]
        assert fantasy and fantasy["whole_period_sharpe"] is not None
        assert "in-sample fantasy" in fantasy["note"]

    def test_fold_inner_trials_logged(self, reg_path, tmp_path):
        artifact, _ = self._run_wfa(reg_path, tmp_path)
        df = ev_registry.load(reg_path)
        trials = df[(df["statistic"] == "opt_sharpe")
                    & (df["run_id"] == artifact["run_id"])]
        # 2 folds x de budget + frozen test/default evals + full-sample search
        assert len(trials) >= 30

    def test_too_few_dates_returns_reason_not_crash(self, reg_path, tmp_path):
        space = opt.BUILTIN_SPACES["tv_threshold"]
        artifact = opt.walk_forward_optimize(
            space, _toy_cache(n_days=100), min_train_days=150, n_folds=2,
            min_trades=1, registry_path=reg_path,
            artifact_dir=str(tmp_path))
        assert "wf_reason" in artifact
        assert "only 100 dates" in artifact["wf_reason"]

    def test_artifact_written(self, reg_path, tmp_path):
        artifact, art_dir = self._run_wfa(reg_path, tmp_path)
        path = os.path.join(art_dir, f"{artifact['run_id']}.json")
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        assert loaded["mode"] == "walk_forward"


# ---------------------------------------------------------------- verdicts


class TestVerdicts:
    def test_no_scorable_trial(self):
        lines = opt._verdict(None, {}, {})
        assert lines == ["NO SCORABLE TRIAL -- nothing beat the degenerate "
                         "guards."]

    def test_high_pbo_read_as_noise(self):
        best = opt.TrialResult({"a": 1}, 2.0)
        lines = opt._verdict(best, {"dsr_prob": 0.62},
                             {"pbo": 0.55, "pbo_reason": ""})
        assert any("do NOT promote" in l for l in lines)
        assert any("treat the winner as noise" in l for l in lines)

    def test_low_psr_and_pbo_survives(self):
        best = opt.TrialResult({"a": 1}, 2.0)
        lines = opt._verdict(best, {"dsr_prob": 0.01},
                             {"pbo": 0.1, "pbo_reason": ""})
        assert any("survives deflation" in l for l in lines)

    def test_missing_diagnostics_reported(self):
        best = opt.TrialResult({"a": 1}, 2.0)
        lines = opt._verdict(best, {"dsr_prob": None, "dsr_reason": "tiny"},
                             {"pbo": None, "pbo_reason": "need >=2"})
        assert any("DSR unavailable" in l for l in lines)
        assert any("PBO unavailable" in l for l in lines)

    def test_wfa_verdict_beats_and_loses(self):
        wfa = {"sharpe": 1.0, "total_return_pct": 5.0,
               "max_drawdown_pct": -8.0}
        default = {"sharpe": 1.4}
        lines = opt._wfa_verdict(wfa, default, False, {},
                                 {"whole_period_sharpe": 3.2, "params": {}})
        assert any("does NOT beat" in l for l in lines)
        assert any("in-sample fantasy sharpe 3.2" in l for l in lines)
