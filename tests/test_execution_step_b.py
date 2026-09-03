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


class TestInverseVolSizing:
    def _alt(self, amt, n):
        return [100.0 + (amt if i % 2 == 0 else 0.0) for i in range(n)]

    def _two_vol_cache(self):
        # 16 pre-bars alternating by `amt` (a known, constant trailing
        # abs-close-diff -> a known entry_vol_pct), entry signal at 16,
        # fill at 17, flat hold, exit signal at 20, fill at 21.
        low = self._alt(0.5, 16) + [100.0] * 4 + [110.0] * 2
        high = self._alt(2.0, 16) + [100.0] * 4 + [110.0] * 2
        n = len(low)
        entries = [False] * 16 + [True] + [False] * (n - 17)
        exits = [False] * 20 + [True] + [False] * (n - 21)
        return {"LOW": _frame(low, entries, exits), "HIGH": _frame(high, entries, exits)}

    def test_lower_vol_symbol_gets_a_bigger_size(self):
        cache = self._two_vol_cache()
        cfg = ex.ExecutionConfig(
            sizing=ex.Sizing(mode="inverse_vol", fraction=0.1, vol_target_pct=1.0),
            limits=ex.PortfolioLimits(capital=100_000.0))
        out = tr.simulate(_rule(), cache, config=cfg).set_index("symbol")
        size_low = out.loc["LOW", "pnl_dollars"] / (out.loc["LOW", "pnl_pct"] / 100.0)
        size_high = out.loc["HIGH", "pnl_dollars"] / (out.loc["HIGH", "pnl_pct"] / 100.0)
        # LOW's trailing vol is exactly 1/4 of HIGH's (0.5 vs 2.0 alt amplitude)
        # -> inverse-vol sizing must give it ~4x the dollar size (tolerance
        # only for the pnl_dollars round(size * pnl_pct/100, 2) rounding
        # _portfolio_pass applies when re-denominating onto the new size).
        assert size_low / size_high == pytest.approx(4.0, rel=1e-4)

    def test_insufficient_vol_history_is_skipped_not_guessed(self):
        # entry at bar 1 -- far short of the 14-bar trailing window
        # entry_vol_pct needs, so it's None and this trade cannot be sized.
        cache = {"AAA": _frame([100, 100, 110, 110],
                               [True, False, False, False],
                               [False, False, True, False])}
        cfg = ex.ExecutionConfig(
            sizing=ex.Sizing(mode="inverse_vol", fraction=0.1, vol_target_pct=1.0),
            limits=ex.PortfolioLimits(capital=100_000.0))
        out = tr.simulate(_rule(), cache, config=cfg)
        assert out.empty

    def test_requires_capital(self):
        cfg = ex.ExecutionConfig(
            sizing=ex.Sizing(mode="inverse_vol", fraction=0.1, vol_target_pct=1.0))
        with pytest.raises(ValueError, match="requires limits.capital"):
            tr.simulate(_rule(), self._two_vol_cache(), config=cfg)

    def test_sizing_validation(self):
        with pytest.raises(ValueError, match="requires 0 < fraction"):
            ex.Sizing(mode="inverse_vol", vol_target_pct=1.0)
        with pytest.raises(ValueError, match="requires vol_target_pct"):
            ex.Sizing(mode="inverse_vol", fraction=0.1)
        with pytest.raises(ValueError, match="only meaningful with mode"):
            ex.Sizing(mode="fixed_notional", vol_target_pct=1.0)


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


class TestSinglePassPortfolio:
    """
    docs/superpowers/specs/2026-09-03-single-pass-portfolio-engine-design.md
    -- the admission-order fix. _portfolio_pass (the OLD, filter-only pass,
    retained only as a regression marker) generates candidates eagerly per
    symbol BEFORE knowing whether they'll be admitted, so a rejected
    candidate's phantom occupation blocks a different, later entry signal
    on the same symbol that a true single-pass sim would have considered.
    _simulate_single_pass (used by tr.simulate() and stats.permutation_trades()
    whenever needs_portfolio) fixes this by generating one candidate at a
    time and resuming a REJECTED symbol's search right after the rejected
    candidate's own entry signal, not its exit.
    """

    def _motivating_case(self):
        # BBB: enters day 1, exits day 3 -- occupies the single concurrent
        # slot first (its entry_date sorts before AAA's first candidate).
        # AAA: signals at day 2 (entry) and day 6 (entry), both exiting on
        # the first shared exit signal at day 9. Under max_concurrent=1:
        #   - AAA's day-2 candidate (entry idx[2]) is evaluated while BBB
        #     (open idx[1]..idx[3]) still occupies the slot -> REJECTED.
        #   - OLD engine: simulate_symbol already consumed AAA's day-6
        #     signal generating that one candidate ("already in position"),
        #     so AAA never gets a second candidate. AAA contributes 0 trades.
        #   - NEW engine: rejection resumes AAA's search at day-6's signal,
        #     which is admitted once BBB has released (idx[3] <= idx[6]).
        #     AAA contributes exactly 1 trade: entry idx[6], exit idx[9].
        n = 15
        a_close = [100.0] * n
        a_entries = [False] * n
        a_entries[1] = True     # sig_i=1 -> entry_i=2
        a_entries[5] = True     # sig_i=5 -> entry_i=6
        a_exits = [False] * n
        a_exits[8] = True       # sig_j=8 -> exit_i=9, first exit either entry finds
        aaa = _frame(a_close, a_entries, a_exits)

        b_close = [100.0] * n
        b_entries = [False] * n
        b_entries[0] = True     # sig_i=0 -> entry_i=1
        b_exits = [False] * n
        b_exits[2] = True       # sig_j=2 -> exit_i=3
        bbb = _frame(b_close, b_entries, b_exits)

        cache = {"AAA": aaa, "BBB": bbb}
        cfg = ex.ExecutionConfig(limits=ex.PortfolioLimits(max_concurrent=1))
        return cache, cfg

    def test_old_engine_shows_the_bug(self):
        """Regression marker: documents what _portfolio_pass actually does,
        so the fix below is legible as a fix and not just a different number."""
        cache, cfg = self._motivating_case()
        rows = []
        for sym, df in cache.items():
            le, lx, se, sx = tr.rule_flags(_rule(), df)
            rows.extend(tr.simulate_symbol(df.index, df["close"], le, lx, se, sx,
                                           sym, 10_000.0))
        admitted = tr._portfolio_pass(rows, cfg)
        assert [r["symbol"] for r in admitted] == ["BBB"]

    def test_new_engine_fixes_it(self):
        cache, cfg = self._motivating_case()
        out = tr.simulate(_rule(), cache, config=cfg)
        assert sorted(out["symbol"]) == ["AAA", "BBB"]
        aaa = out[out["symbol"] == "AAA"].iloc[0]
        assert aaa["entry_date"] == cache["AAA"].index[6]
        assert aaa["exit_date"] == cache["AAA"].index[9]

    def test_no_rejections_matches_old_engine_exactly(self):
        """When nothing is ever rejected, the new lazy engine must produce
        IDENTICAL output to the old filter-only pass -- the two only differ
        in what happens after a rejection."""
        rng = np.random.default_rng(11)
        n = 60
        cache = {}
        for s in ("AAA", "BBB", "CCC"):
            px = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
            cache[s] = _frame(px, rng.random(n) < 0.15, rng.random(n) < 0.2)
        cfg = ex.ExecutionConfig(
            sizing=ex.Sizing(mode="fixed_fraction", fraction=0.05),
            limits=ex.PortfolioLimits(capital=10_000_000.0))   # never binds

        rows = []
        for sym, df in cache.items():
            le, lx, se, sx = tr.rule_flags(_rule(), df)
            rows.extend(tr.simulate_symbol(df.index, df["close"], le, lx, se, sx,
                                           sym, 10_000.0))
        old = pd.DataFrame(tr._portfolio_pass(rows, cfg), columns=tr.TRADE_COLS)
        new = tr.simulate(_rule(), cache, config=cfg)
        pd.testing.assert_frame_equal(
            old.reset_index(drop=True), new.reset_index(drop=True))

    def test_resumed_candidate_still_sizes_off_current_equity(self):
        """A candidate generated AFTER a rejection (the new code path) must
        size using the ordinary formula -- entry_vol_pct and the equity it
        sizes against are untouched by this rewrite. Uses a real price move
        on AAA's resumed trade so the implied committed size is checkable,
        not just "a trade exists"."""
        n = 15
        a_close = [100.0] * 9 + [110.0] * (n - 9)   # +10% held across idx[6]->idx[9]
        a_entries = [False] * n
        a_entries[1] = True
        a_entries[5] = True
        a_exits = [False] * n
        a_exits[8] = True
        aaa = _frame(a_close, a_entries, a_exits)

        b_close = [100.0] * n
        b_entries = [False] * n
        b_entries[0] = True
        b_exits = [False] * n
        b_exits[2] = True
        bbb = _frame(b_close, b_entries, b_exits)

        cache = {"AAA": aaa, "BBB": bbb}
        cfg = ex.ExecutionConfig(
            sizing=ex.Sizing(mode="fixed_fraction", fraction=0.5),
            limits=ex.PortfolioLimits(capital=20_000.0, max_concurrent=1))
        out = tr.simulate(_rule(), cache, config=cfg).set_index("symbol")

        assert "AAA" in out.index
        # BBB (10,000 committed, sized off the initial 20,000 equity)
        # releases before AAA's resumed candidate enters, so AAA also sizes
        # off the full 20,000 * 0.5 = 10,000 -- the ordinary formula, not a
        # smaller number left over from some stale partial-equity state.
        implied_size = out.loc["AAA", "pnl_dollars"] / (out.loc["AAA", "pnl_pct"] / 100.0)
        assert implied_size == pytest.approx(10_000.0, rel=1e-6)

    def test_permutation_null_uses_the_new_engine_too(self):
        """stats.permutation_trades' portfolio-constrained path must use the
        same fixed engine as tr.simulate(), or the null and the observed run
        aren't a fair comparison (see the design spec)."""
        rng = np.random.default_rng(12)
        n = 80
        cache = {}
        for s in ("AAA", "BBB", "CCC"):
            px = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.011, n)))
            cache[s] = _frame(px, rng.random(n) < 0.12, rng.random(n) < 0.18)
        cfg = ex.ExecutionConfig(limits=ex.PortfolioLimits(max_concurrent=1))

        a = ev_stats.permutation_trades(_rule(), cache, n_perm=25, seed=4, config=cfg)
        b = ev_stats.permutation_trades(_rule(), cache, n_perm=25, seed=4, config=cfg)
        assert a == b     # deterministic under a fixed seed

    def test_permutation_null_no_longer_calls_the_old_pass(self, monkeypatch):
        called = {"hit": False}
        orig = tr._portfolio_pass

        def spy(*a, **k):
            called["hit"] = True
            return orig(*a, **k)

        monkeypatch.setattr(tr, "_portfolio_pass", spy)
        rng = np.random.default_rng(13)
        n = 50
        cache = {"AAA": _frame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))),
                               rng.random(n) < 0.15, rng.random(n) < 0.2)}
        cfg = ex.ExecutionConfig(limits=ex.PortfolioLimits(max_concurrent=1))
        ev_stats.permutation_trades(_rule(), cache, n_perm=10, seed=5, config=cfg)
        assert called["hit"] is False


class TestHrpSizing:
    """mode='hrp' -- evaluation/hrp.py's Hierarchical Risk Parity wired into
    the single-pass engine as a sizing mode. The core HRP math (distance
    matrix, quasi-diagonalization, recursive bisection) is exhaustively
    covered in tests/test_hrp.py; these tests cover the WIRING: cohort
    selection (open symbols + candidate), the PIT trailing-window boundary,
    and graceful rejection when the cohort can't support a real estimate."""

    def _state(self, sym, closes, dates):
        return {sym: {"close": pd.Series(np.asarray(closes, dtype=float),
                                         index=pd.DatetimeIndex(dates))}}

    def test_requires_capital(self):
        cfg = ex.ExecutionConfig(sizing=ex.Sizing(mode="hrp", fraction=0.5))
        cache = {"AAA": _frame([100, 100, 110, 110],
                               [True, False, False, False],
                               [False, False, True, False])}
        with pytest.raises(ValueError, match="requires limits.capital"):
            tr.simulate(_rule(), cache, config=cfg)

    def test_no_open_positions_gets_full_fraction_budget(self):
        idx = pd.bdate_range("2024-01-01", periods=60)
        symbols = self._state("AAA", 100 + np.arange(60) * 0.1, idx)
        row = {"symbol": "AAA", "entry_date": idx[40]}
        sizing = ex.Sizing(mode="hrp", fraction=0.4, hrp_lookback=30)
        size = tr._hrp_size(row, sizing, symbols, set(), equity=50_000.0)
        assert size == pytest.approx(0.4 * 50_000.0)

    def test_known_weight_via_stubbed_hrp_weights(self, monkeypatch):
        """Exact wiring check: with hrp_weights() stubbed to a known Series,
        _hrp_size must return exactly fraction * equity * that symbol's
        weight -- decoupled from whether the real algorithm agrees, which
        test_hrp.py already covers on its own."""
        idx = pd.bdate_range("2024-01-01", periods=60)
        symbols = {}
        symbols.update(self._state("AAA", 100 + np.random.default_rng(1).normal(0, 1, 60), idx))
        symbols.update(self._state("BBB", 100 + np.random.default_rng(2).normal(0, 1, 60), idx))
        row = {"symbol": "BBB", "entry_date": idx[50]}
        sizing = ex.Sizing(mode="hrp", fraction=0.5, hrp_lookback=30)

        monkeypatch.setattr(tr.ev_hrp, "hrp_weights",
                            lambda returns: pd.Series({"AAA": 0.7, "BBB": 0.3}))
        size = tr._hrp_size(row, sizing, symbols, {"AAA"}, equity=100_000.0)
        assert size == pytest.approx(0.5 * 100_000.0 * 0.3)

    def test_hrp_weights_valueerror_is_a_clean_rejection(self, monkeypatch):
        idx = pd.bdate_range("2024-01-01", periods=60)
        symbols = {}
        symbols.update(self._state("AAA", 100 + np.random.default_rng(3).normal(0, 1, 60), idx))
        symbols.update(self._state("BBB", 100 + np.random.default_rng(4).normal(0, 1, 60), idx))
        row = {"symbol": "BBB", "entry_date": idx[50]}
        sizing = ex.Sizing(mode="hrp", fraction=0.5, hrp_lookback=30)

        def boom(returns):
            raise ValueError("near-zero variance")

        monkeypatch.setattr(tr.ev_hrp, "hrp_weights", boom)
        assert tr._hrp_size(row, sizing, symbols, {"AAA"}, equity=100_000.0) is None

    def test_insufficient_overlap_rejects_without_calling_hrp_weights(self, monkeypatch):
        # AAA's history ends well before BBB's candidate window even starts --
        # the two series share zero overlapping trading days.
        idx_a = pd.bdate_range("2020-01-01", periods=30)
        idx_b = pd.bdate_range("2024-01-01", periods=60)
        symbols = {}
        symbols.update(self._state("AAA", 100 + np.arange(30) * 0.1, idx_a))
        symbols.update(self._state("BBB", 100 + np.arange(60) * 0.1, idx_b))
        row = {"symbol": "BBB", "entry_date": idx_b[50]}
        sizing = ex.Sizing(mode="hrp", fraction=0.5, hrp_lookback=30)

        called = {"hit": False}
        monkeypatch.setattr(tr.ev_hrp, "hrp_weights",
                            lambda returns: called.__setitem__("hit", True))
        assert tr._hrp_size(row, sizing, symbols, {"AAA"}, equity=100_000.0) is None
        assert called["hit"] is False   # rejected before ever calling hrp_weights

    def test_pit_boundary_excludes_the_entry_bar_itself(self):
        """A price shock exactly ON the candidate's entry_date must not
        affect its own sizing -- the trailing window is strictly before
        entry_date, the same boundary entry_vol_pct already uses."""
        idx = pd.bdate_range("2024-01-01", periods=60)
        a_close = 100 + np.cumsum(np.random.default_rng(7).normal(0, 0.5, 60))
        b_close = 100 + np.cumsum(np.random.default_rng(8).normal(0, 0.5, 60))
        symbols_normal = {}
        symbols_normal.update(self._state("AAA", a_close, idx))
        symbols_normal.update(self._state("BBB", b_close, idx))

        b_close_shocked = b_close.copy()
        b_close_shocked[50] *= 5.0    # a huge shock exactly AT entry_date
        symbols_shocked = {}
        symbols_shocked.update(self._state("AAA", a_close, idx))
        symbols_shocked.update(self._state("BBB", b_close_shocked, idx))

        row = {"symbol": "BBB", "entry_date": idx[50]}
        sizing = ex.Sizing(mode="hrp", fraction=0.5, hrp_lookback=30)
        size_normal = tr._hrp_size(row, sizing, symbols_normal, {"AAA"}, 100_000.0)
        size_shocked = tr._hrp_size(row, sizing, symbols_shocked, {"AAA"}, 100_000.0)
        assert size_normal is not None
        # The shock never enters the trailing window (strictly < entry_date),
        # so it must not move the computed size at all.
        assert size_shocked == pytest.approx(size_normal)

    def test_lower_variance_symbol_gets_bigger_hrp_weight_end_to_end(self):
        """Real end-to-end check (no mocking): two symbols held concurrently,
        one visibly calmer than the other over the trailing window feeding
        the second one's entry -- the calmer one's own later entry (once it
        becomes the candidate relative to an already-open choppier name)
        should be sized up, mirroring inverse_vol's own directional test."""
        n = 220
        rng_calm = 100 + np.cumsum(np.random.default_rng(5).normal(0, 0.2, n))
        rng_choppy = 100 + np.cumsum(np.random.default_rng(6).normal(0, 2.0, n))

        # CHOPPY enters early and stays open a long time.
        choppy_entries = [False] * n
        choppy_entries[9] = True
        choppy_exits = [False] * n
        choppy_exits[199] = True
        choppy = _frame(rng_choppy, choppy_entries, choppy_exits)

        # CALM enters at bar 100 (choppy still open), a short hold.
        calm_entries = [False] * n
        calm_entries[99] = True
        calm_exits = [False] * n
        calm_exits[119] = True
        calm = _frame(rng_calm, calm_entries, calm_exits)

        cache = {"CALM": calm, "CHOPPY": choppy}
        cfg = ex.ExecutionConfig(
            sizing=ex.Sizing(mode="hrp", fraction=0.3, hrp_lookback=60),
            limits=ex.PortfolioLimits(capital=200_000.0, max_concurrent=2))
        out = tr.simulate(_rule(), cache, config=cfg).set_index("symbol")
        assert "CALM" in out.index
        # CALM's implied committed size should exceed a naive equal split
        # (0.3 * 200,000 / 2 = 30,000) since it carries less risk than its
        # open cohort-mate -- HRP overweights it accordingly.
        implied_size = out.loc["CALM", "pnl_dollars"] / (out.loc["CALM", "pnl_pct"] / 100.0)
        assert implied_size > 0.3 * 200_000.0 / 2.0

    def test_permutation_with_hrp_mode_is_deterministic(self):
        rng = np.random.default_rng(14)
        n = 100
        cache = {}
        for s in ("AAA", "BBB", "CCC"):
            px = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
            cache[s] = _frame(px, rng.random(n) < 0.15, rng.random(n) < 0.2)
        cfg = ex.ExecutionConfig(
            sizing=ex.Sizing(mode="hrp", fraction=0.2, hrp_lookback=30),
            limits=ex.PortfolioLimits(capital=500_000.0, max_concurrent=3))
        a = ev_stats.permutation_trades(_rule(), cache, n_perm=15, seed=6, config=cfg)
        b = ev_stats.permutation_trades(_rule(), cache, n_perm=15, seed=6, config=cfg)
        assert a == b
