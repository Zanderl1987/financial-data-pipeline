"""
test_execution_step_b.py -- the discrete-trade simulator's config behavior
(W1 Step B). See docs/superpowers/specs/2026-08-16-execution-engine-unification-design.md.

Two classes of test here and they carry different weight:

  EQUIVALENCE -- LEGACY must reproduce pre-Step-B behavior exactly. These are
  the tests that let the campaign keep running without a protocol amendment.

  LOOK-AHEAD -- a stop observed on day t must execute at day t+1's close, the
  same as a rule exit. Filling at the trigger bar's own close is look-ahead.
  This repo has shipped that bug twice (oil_shock's entry_lag; the vol-target
  and circuit-breaker same-day bugs fixed in 35c60e3), so it gets an explicit
  hand-built price path rather than a property check.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from evaluation import execution as ex        # noqa: E402
from evaluation import stats as ev_stats      # noqa: E402
from evaluation import trades as tr           # noqa: E402
from evaluation.contracts import TradeRule    # noqa: E402


def _frame(closes, entries, exits):
    idx = pd.bdate_range("2024-01-01", periods=len(closes))
    return pd.DataFrame({"close": np.asarray(closes, dtype=float),
                         "_e": np.asarray(entries, dtype=bool),
                         "_x": np.asarray(exits, dtype=bool)}, index=idx)


def _rule(name="r", side="long", notional=10_000.0):
    return TradeRule(name=name, entries=lambda df: df["_e"],
                     exits=lambda df: df["_x"], side=side, notional=notional)


def _sim(df, cfg=None, notional=10_000.0):
    # The engine's contract is numpy flag ARRAYS (what rule_flags produces),
    # not pandas Series -- it indexes them positionally.
    return tr.simulate_symbol(df.index, df["close"],
                              df["_e"].to_numpy(bool), df["_x"].to_numpy(bool),
                              np.zeros(len(df), bool), np.zeros(len(df), bool),
                              "TEST", notional, config=cfg)


class TestLegacyEquivalence:
    """LEGACY == no config == pre-Step-B behavior."""

    def setup_method(self):
        rng = np.random.default_rng(5)
        n = 120
        self.px = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, n)))
        self.e = rng.random(n) < 0.10
        self.x = rng.random(n) < 0.15
        self.df = _frame(self.px, self.e, self.x)

    def test_none_equals_legacy(self):
        assert _sim(self.df) == _sim(self.df, ex.LEGACY)

    def test_legacy_charges_no_cost(self):
        rows = _sim(self.df, ex.LEGACY)
        for r in rows:
            pct = ((r["exit_price"] / r["entry_price"]) - 1.0)
            assert r["pnl_pct"] == round(100 * pct, 3)

    def test_legacy_exit_reason_is_always_rule(self):
        rows = _sim(self.df, ex.LEGACY)
        assert rows and {r["exit_reason"] for r in rows} == {"rule"}

    def test_legacy_skips_portfolio_pass(self):
        """Not 'runs it with no-op limits' -- skipped, so no float drift."""
        cache = {"TEST": self.df}
        out = tr.simulate(_rule(), cache)
        assert list(out["pnl_dollars"]) == [r["pnl_dollars"] for r in _sim(self.df)]

    def test_permutation_seed_stability(self):
        cache = {"TEST": self.df}
        a = ev_stats.permutation_trades(_rule(), cache, n_perm=40, seed=3)
        b = ev_stats.permutation_trades(_rule(), cache, n_perm=40, seed=3)
        assert a == b
        c = ev_stats.permutation_trades(_rule(), cache, n_perm=40, seed=3,
                                        config=ex.LEGACY)
        assert a == c


class TestLookAheadSafety:
    """A stop seen at t fills at t+1. Hand-built path where the two differ."""

    def test_stop_fills_at_next_close_not_trigger_close(self):
        # entry at idx 1 (100.0). idx 2 drops 10% -> stop triggers on that close.
        # idx 3 rebounds. A look-ahead engine exits at 90; the correct engine
        # exits at 95, so the two are trivially distinguishable.
        closes = [100, 100, 90, 95, 95, 95]
        rows = _sim(_frame(closes, [True, False, False, False, False, False],
                           [False] * 6),
                    ex.ExecutionConfig(risk=ex.RiskControls(stop_loss_pct=5)))
        assert len(rows) == 1
        r = rows[0]
        assert r["exit_reason"] == "stop"
        assert r["exit_price"] == 95.0, "stop filled at its own trigger bar (look-ahead)"
        assert r["days_held"] == 2

    def test_target_fills_at_next_close(self):
        closes = [100, 100, 110, 105, 105, 105]
        rows = _sim(_frame(closes, [True] + [False] * 5, [False] * 6),
                    ex.ExecutionConfig(risk=ex.RiskControls(take_profit_pct=5)))
        assert rows[0]["exit_reason"] == "target"
        assert rows[0]["exit_price"] == 105.0

    def test_stop_on_short_uses_opposite_sign(self):
        closes = [100, 100, 110, 105, 105, 105]
        df = _frame(closes, [False] * 6, [False] * 6)
        rows = tr.simulate_symbol(
            df.index, df["close"], np.zeros(6, bool), np.zeros(6, bool),
            np.array([True] + [False] * 5), np.zeros(6, bool), "TEST", 10_000.0,
            config=ex.ExecutionConfig(risk=ex.RiskControls(stop_loss_pct=5)))
        assert rows[0]["exit_reason"] == "stop"
        assert rows[0]["pnl_pct"] < 0      # short stopped out on a rally


class TestRiskControls:
    def test_stop_precedes_later_rule_exit(self):
        closes = [100, 100, 90, 90, 90, 90]
        exits = [False, False, False, False, True, False]
        plain = _sim(_frame(closes, [True] + [False] * 5, exits))
        stopped = _sim(_frame(closes, [True] + [False] * 5, exits),
                       ex.ExecutionConfig(risk=ex.RiskControls(stop_loss_pct=5)))
        assert plain[0]["exit_reason"] == "rule" and plain[0]["days_held"] == 4
        assert stopped[0]["exit_reason"] == "stop" and stopped[0]["days_held"] == 2

    def test_rule_wins_ties_on_the_same_bar(self):
        closes = [100, 100, 90, 90, 90, 90]
        exits = [False, False, True, False, False, False]
        rows = _sim(_frame(closes, [True] + [False] * 5, exits),
                    ex.ExecutionConfig(risk=ex.RiskControls(stop_loss_pct=5)))
        assert rows[0]["exit_reason"] == "rule"

    def test_max_holding_days_caps_trade(self):
        closes = [100] * 10
        rows = _sim(_frame(closes, [True] + [False] * 9, [False] * 10),
                    ex.ExecutionConfig(risk=ex.RiskControls(max_holding_days=3)))
        assert rows[0]["exit_reason"] == "time"
        assert rows[0]["days_held"] == 3

    def test_no_risk_controls_leaves_position_unclosed(self):
        """Without a cap, a never-exiting position is dropped and blocks the
        symbol -- the documented pre-existing behavior."""
        closes = [100] * 10
        assert _sim(_frame(closes, [True] + [False] * 9, [False] * 10)) == []

    def test_trailing_stop_measures_from_peak(self):
        # rises to 120 then falls to 114 (-5% from peak, +14% from entry)
        closes = [100, 100, 120, 114, 114, 114]
        cfg = ex.ExecutionConfig(risk=ex.RiskControls(stop_loss_pct=5, trailing=True))
        rows = _sim(_frame(closes, [True] + [False] * 5, [False] * 6), cfg)
        assert rows[0]["exit_reason"] == "stop"
        fixed = _sim(_frame(closes, [True] + [False] * 5, [False] * 6),
                     ex.ExecutionConfig(risk=ex.RiskControls(stop_loss_pct=5)))
        assert fixed == [], "a fixed stop should never fire on a position in profit"

    def test_vol_stop_needs_history(self):
        """Mirrors scenario()'s `if loc0 >= 14` guard: too little history means
        no vol stop rather than a garbage threshold."""
        closes = [100] * 10
        rows = _sim(_frame(closes, [True] + [False] * 9, [False] * 10),
                    ex.ExecutionConfig(risk=ex.RiskControls(vol_stop_mult=2.0,
                                                            max_holding_days=5)))
        assert rows[0]["exit_reason"] == "time"


class TestCosts:
    def test_round_trip_deducted_with_stage3_rounding_order(self):
        closes = [100, 100, 110, 110]
        cfg = ex.TV_CAMPAIGN
        rows = _sim(_frame(closes, [True, False, False, False],
                           [False, False, True, False]), cfg)
        r = rows[0]
        pct = 110 / 100 - 1.0
        rate = ex.round_trip_rate(cfg.costs)
        assert r["pnl_dollars"] == round(round(10_000 * pct, 2) - 10_000 * rate, 2)
        assert r["pnl_pct"] == round(round(100 * pct, 3) - 100 * rate, 3)

    def test_costs_reduce_pnl(self):
        closes = [100, 100, 110, 110]
        df = _frame(closes, [True, False, False, False], [False, False, True, False])
        assert _sim(df, ex.TV_CAMPAIGN)[0]["pnl_dollars"] < _sim(df)[0]["pnl_dollars"]

    def test_borrow_fee_accrues_on_short_holding_period(self):
        # short entry day 0 -> executes day 1 @ 100; exit signal day 3 ->
        # executes day 4 @ 100 (flat price isolates the borrow fee from
        # price P&L). days_held = 4 - 1 = 3.
        closes = [100, 100, 100, 100, 100, 100]
        cfg = ex.ExecutionConfig(costs=ex.CostModel(borrow_fee_bps=252 * 100))
        rows = tr.simulate_symbol(
            pd.bdate_range("2024-01-01", periods=6), pd.Series(closes, dtype=float),
            np.zeros(6, bool), np.zeros(6, bool),
            np.array([True] + [False] * 5),
            np.array([False, False, False, True, False, False]),
            "TEST", 10_000.0, config=cfg)
        r = rows[0]
        assert r["days_held"] == 3
        # borrow_fee_bps=252*100 -> 2520%/yr -> 1%/trading day; 3 days -> 3%
        # of notional, i.e. pnl_pct == -3.0 with flat price.
        assert r["pnl_pct"] == pytest.approx(-3.0)
        assert r["pnl_dollars"] == pytest.approx(-300.0)

    def test_borrow_fee_does_not_apply_to_longs(self):
        closes = [100, 100, 100, 100, 100, 100]
        cfg = ex.ExecutionConfig(costs=ex.CostModel(borrow_fee_bps=252 * 100))
        rows = _sim(_frame(closes, [True] + [False] * 5,
                           [False, False, False, True, False, False]), cfg)
        assert rows[0]["pnl_pct"] == 0.0

    def test_zero_borrow_fee_is_a_no_op(self):
        closes = [100, 100, 100, 100, 100, 100]
        rows_default = tr.simulate_symbol(
            pd.bdate_range("2024-01-01", periods=6), pd.Series(closes, dtype=float),
            np.zeros(6, bool), np.zeros(6, bool),
            np.array([True] + [False] * 5),
            np.array([False, False, False, True, False, False]),
            "TEST", 10_000.0, config=None)
        assert rows_default[0]["pnl_pct"] == 0.0


class TestPortfolioLimits:
    def _two_symbol_cache(self):
        # both symbols enter on the same bar and hold for 2 days
        closes = [100, 100, 110, 110, 110, 110]
        df = _frame(closes, [True] + [False] * 5,
                    [False, False, True, False, False, False])
        return {"AAA": df, "BBB": df.copy()}

    def test_max_concurrent_rejects_the_second_overlapping_trade(self):
        cache = self._two_symbol_cache()
        both = tr.simulate(_rule(), cache)
        capped = tr.simulate(_rule(), cache, config=ex.ExecutionConfig(
            limits=ex.PortfolioLimits(max_concurrent=1)))
        assert len(both) == 2 and len(capped) == 1

    def test_capital_budget_rejects_unaffordable_trade(self):
        cache = self._two_symbol_cache()
        capped = tr.simulate(_rule(), cache, config=ex.ExecutionConfig(
            limits=ex.PortfolioLimits(capital=15_000.0)))
        assert len(capped) == 1, "10k + 10k should not fit in 15k"

    def test_capital_frees_up_after_exit(self):
        # AAA: enters at bar 1, exits at bar 3. BBB: enters at bar 5 -- strictly
        # after AAA released its capital. Frames run to 8 bars so BBB's exit
        # (signal 6 -> fill 7) lands inside the data; a trade whose fill falls
        # off the end is dropped before the portfolio pass ever sees it.
        early = _frame([100, 100, 110, 110, 110, 110, 110, 110],
                       [True] + [False] * 7,
                       [False, False, True] + [False] * 5)
        late = _frame([100, 100, 100, 100, 100, 100, 110, 110],
                      [False] * 4 + [True, False, False, False],
                      [False] * 6 + [True, False])
        both = tr.simulate(_rule(), {"AAA": early, "BBB": late})
        assert len(both) == 2, "fixture is wrong: both trades must exist uncapped"

        capped = tr.simulate(_rule(), {"AAA": early, "BBB": late},
                             config=ex.ExecutionConfig(
                                 limits=ex.PortfolioLimits(capital=10_000.0)))
        assert len(capped) == 2, "the second trade starts after the first closed"

    def test_fixed_fraction_sizes_off_equity(self):
        cache = {"AAA": _frame([100, 100, 110, 110], [True, False, False, False],
                               [False, False, True, False])}
        cfg = ex.ExecutionConfig(
            sizing=ex.Sizing(mode="fixed_fraction", fraction=0.5),
            limits=ex.PortfolioLimits(capital=20_000.0))
        out = tr.simulate(_rule(), cache, config=cfg)
        assert out["pnl_dollars"].iloc[0] == pytest.approx(10_000 * 0.10, abs=0.01)

    def test_fixed_fraction_requires_capital(self):
        cache = {"AAA": _frame([100, 100, 110, 110], [True, False, False, False],
                               [False, False, True, False])}
        cfg = ex.ExecutionConfig(sizing=ex.Sizing(mode="fixed_fraction", fraction=0.5))
        with pytest.raises(ValueError, match="requires limits.capital"):
            tr.simulate(_rule(), cache, config=cfg)

    def test_permutation_applies_limits_to_the_null_too(self):
        """An unconstrained null against a rationed strategy is not the stated
        null. Asserts the constrained run's permutation actually differs."""
        rng = np.random.default_rng(9)
        n = 100
        cache = {}
        for s in ("AAA", "BBB", "CCC"):
            px = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.012, n)))
            cache[s] = _frame(px, rng.random(n) < 0.12, rng.random(n) < 0.18)
        cfg = ex.ExecutionConfig(limits=ex.PortfolioLimits(max_concurrent=1))
        free = ev_stats.permutation_trades(_rule(), cache, n_perm=30, seed=1)
        capped = ev_stats.permutation_trades(_rule(), cache, n_perm=30, seed=1,
                                             config=cfg)
        assert capped["obs_pnl_dollars"] != free["obs_pnl_dollars"]


class TestStage3Migration:
    """
    The campaign now gets its costs from the engine via `config=`.

    The equivalence of the config path to the deleted monkeypatch was verified
    before deletion, both here on synthetic data and end-to-end on real prices
    (identical total_pnl_net / pnl_p / pnl_p_5bps / pnl_p_20bps / cost_fragile).
    What survives as a permanent test is the invariant that outlived the patch:
    the rate, and the fact that the null pays it too.
    """

    def _cache(self):
        rng = np.random.default_rng(17)
        n = 150
        out = {}
        for s in ("AAA", "BBB"):
            px = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.013, n)))
            out[s] = _frame(px, rng.random(n) < 0.10, rng.random(n) < 0.16)
        return out

    def test_cost_config_matches_prereg_rate(self):
        from strategies import stage3
        cfg = stage3.cost_config(stage3.PRIMARY_COST_BPS)
        assert ex.round_trip_rate(cfg.costs) == 2.0 * stage3.PRIMARY_COST_BPS / 1e4

    def test_campaign_config_reduces_pnl_versus_legacy(self):
        from strategies import stage3
        cache, rule = self._cache(), _rule()
        gross = tr.simulate(rule, cache)
        net = tr.simulate(rule, cache,
                          config=stage3.cost_config(stage3.PRIMARY_COST_BPS))
        assert len(gross) == len(net) > 0, "costs must not change WHICH trades fire"
        assert net["pnl_dollars"].sum() < gross["pnl_dollars"].sum()

    def test_null_pays_the_same_costs_as_the_strategy(self):
        """If the permutation null ran gross while the strategy ran net, the
        p-value would be biased against the strategy. Both go through the same
        config, so the observed P&L reported by permutation_trades must match a
        direct net simulate()."""
        from strategies import stage3
        cache, rule = self._cache(), _rule()
        cfg = stage3.cost_config(stage3.PRIMARY_COST_BPS)
        perm = ev_stats.permutation_trades(rule, cache, n_perm=30, seed=2, config=cfg)
        direct = tr.simulate(rule, cache, config=cfg)
        assert perm["obs_pnl_dollars"] == pytest.approx(
            round(float(direct["pnl_dollars"].sum()), 2))

    def test_stage5_holdout_uses_the_config_path_too(self):
        """Stage 5 is the one-shot holdout -- it used the same monkeypatch and
        had to migrate with stage3, not after it."""
        import inspect as _inspect
        from strategies import stage5
        src = _inspect.getsource(stage5.run_holdout_for)
        assert "cost_config" in src and "cost_adjusted" not in src


class TestRegistryExecutionHash:
    """Step 6: a recorded result should carry the execution semantics that
    produced it, and adding that column must not corrupt 1,700+ existing rows."""

    def test_hash_is_stable_and_discriminating(self):
        assert ex.config_hash(None) == ex.config_hash(ex.LEGACY)
        assert ex.config_hash(ex.LEGACY) != ex.config_hash(ex.TV_CAMPAIGN)

    def test_hash_ignores_name(self):
        """A rename is not a new experiment."""
        renamed = ex.ExecutionConfig(name="something_else",
                                     costs=ex.CostModel(commission_bps=10.0))
        assert ex.config_hash(renamed) == ex.config_hash(ex.TV_CAMPAIGN)

    def test_hash_reflects_risk_and_limits_not_just_costs(self):
        stopped = ex.ExecutionConfig(risk=ex.RiskControls(stop_loss_pct=5))
        capped = ex.ExecutionConfig(limits=ex.PortfolioLimits(max_concurrent=3))
        assert len({ex.config_hash(ex.LEGACY), ex.config_hash(stopped),
                    ex.config_hash(capped)}) == 3

    def test_old_rows_load_as_unknown_not_legacy(self, tmp_path):
        """Pre-Step-B rows must NOT be labeled 'legacy': the stage3 rows among
        them were net of 10 bps via the monkeypatch, so 'legacy' would be a
        false claim about history."""
        import pandas as pd
        from evaluation import registry as reg
        old = pd.DataFrame([{
            "run_id": "r1", "input_name": "x", "input_type": "signal",
            "evaluation": "ic", "horizon": 1, "statistic": "ic", "value": 0.01,
            "n": 10, "universe_hash": "abc", "date_range": "..",
            "created_at": "2026-01-01",
        }])
        path = tmp_path / "results.parquet"
        old.to_parquet(path)
        loaded = reg.load(str(path))
        assert list(loaded["execution_hash"]) == [reg.UNKNOWN_EXECUTION]

    def test_append_defaults_missing_column(self, tmp_path):
        import pandas as pd
        from evaluation import registry as reg
        rows = pd.DataFrame([{
            "run_id": "r2", "input_name": "y", "input_type": "signal",
            "evaluation": "ic", "horizon": 1, "statistic": "ic", "value": 0.02,
            "n": 10, "universe_hash": "abc", "date_range": "..",
            "created_at": "2026-01-02",
        }])
        path = str(tmp_path / "r.parquet")
        assert reg.append(rows, path) == 1
        assert reg.load(path)["execution_hash"].iloc[0] == reg.UNKNOWN_EXECUTION

    def test_campaign_rows_carry_a_real_hash(self):
        from strategies import stage3
        row = {"n_trades": 5, "win_rate": 50.0, "profit_factor": 1.1,
               "sharpe": 0.2, "max_dd": -3.0, "median_hold": 4.0,
               "total_pnl_net": 100.0, "pnl_p": 0.2, "pnl_p_5bps": 0.2,
               "pnl_p_20bps": 0.3, "strategy_id": "slug"}
        out = stage3.registry_rows_for(row, "run", "uhash", "..", "2026-08-17")
        expected = ex.config_hash(stage3.cost_config(stage3.PRIMARY_COST_BPS))
        assert set(out["execution_hash"]) == {expected}
        from evaluation import registry as reg
        assert expected != reg.UNKNOWN_EXECUTION


class TestSignatureCompatibility:
    """The two callers that constrain simulate_symbol's signature."""

    def test_eight_positional_args_still_work(self):
        df = _frame([100, 100, 110, 110], [True, False, False, False],
                    [False, False, True, False])
        rows = tr.simulate_symbol(df.index, df["close"],
                                  df["_e"].to_numpy(bool), df["_x"].to_numpy(bool),
                                  np.zeros(4, bool), np.zeros(4, bool),
                                  "TEST", 10_000.0)
        assert len(rows) == 1

    def test_notional_is_still_bindable_by_name(self):
        import inspect
        sig = inspect.signature(tr.simulate_symbol)
        bound = sig.bind(None, None, None, None, None, None, "TEST", 5_000.0)
        bound.apply_defaults()
        assert bound.arguments["notional"] == 5_000.0
