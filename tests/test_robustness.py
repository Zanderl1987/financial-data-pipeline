"""
tests/test_robustness.py -- W2 robustness battery.

Covers determinism, the invariants each method claims to preserve (intrabar
geometry under noise, return multiset under permutation), the properties that
make each test non-vacuous (compounding varies where summing does not), the
statistical behavior on constructed data with a KNOWN answer (PBO ~ 0.5 on
noise, ~ 0 on a dominant column), and every '*_reason' early return.
"""

import numpy as np
import pandas as pd
import pytest

from evaluation import execution as ev_execution
from evaluation import robustness as rb
from evaluation import trades as tr
from evaluation.contracts import TradeRule


# --------------------------------------------------------------- fixtures


def _frame(closes, seed=0):
    """OHLCV frame with realistic intrabar geometry around the given closes."""
    rng = np.random.default_rng(seed)
    close = np.asarray(closes, dtype=float)
    n = len(close)
    open_ = close * (1.0 + rng.normal(0, 0.002, n))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, 0.004, n)))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "volume": rng.integers(1_000, 10_000, n).astype(float)},
        index=pd.date_range("2020-01-01", periods=n, freq="D"))


@pytest.fixture
def cache():
    rng = np.random.default_rng(7)
    out = {}
    for i, sym in enumerate(["AAA", "BBB"]):
        steps = rng.normal(0.0005, 0.01, 200)
        out[sym] = _frame(100.0 * np.exp(np.cumsum(steps)), seed=i)
    return out


@pytest.fixture
def rule():
    """Close above/below its own 10-day mean -- a real end-to-end signal."""
    def entries(df):
        return df["close"] > df["close"].rolling(10).mean()

    def exits(df):
        return df["close"] < df["close"].rolling(10).mean()

    return TradeRule(name="sma10", entries=entries, exits=exits, notional=10_000.0)


# --------------------------------------------------------------- noise test


class TestNoise:
    def test_preserves_intrabar_geometry(self):
        df = _frame(np.linspace(100, 120, 50))
        out = rb._noisy_frame(df, np.random.default_rng(0), sigma=0.05)
        assert (out["high"] >= out[["open", "close"]].max(axis=1) - 1e-9).all()
        assert (out["low"] <= out[["open", "close"]].min(axis=1) + 1e-9).all()
        assert (out["high"] >= out["low"]).all()

    def test_leaves_volume_alone(self):
        df = _frame(np.linspace(100, 120, 50))
        out = rb._noisy_frame(df, np.random.default_rng(0), sigma=0.05)
        assert (out["volume"] == df["volume"]).all()

    def test_actually_perturbs(self):
        df = _frame(np.linspace(100, 120, 50))
        out = rb._noisy_frame(df, np.random.default_rng(0), sigma=0.01)
        assert not np.allclose(out["close"], df["close"])

    def test_deterministic_under_seed(self, rule, cache):
        a = rb.noise_test(rule, cache, n_trials=15, sigma_bps=20.0, seed=3)
        b = rb.noise_test(rule, cache, n_trials=15, sigma_bps=20.0, seed=3)
        assert a == b

    def test_seed_changes_result(self, rule, cache):
        a = rb.noise_test(rule, cache, n_trials=15, sigma_bps=50.0, seed=1)
        b = rb.noise_test(rule, cache, n_trials=15, sigma_bps=50.0, seed=2)
        assert a["noise_mean_pnl_dollars"] != b["noise_mean_pnl_dollars"]

    def test_reports_expected_keys(self, rule, cache):
        out = rb.noise_test(rule, cache, n_trials=15, sigma_bps=10.0, seed=0)
        for k in ("observed_pnl_dollars", "noise_mean_pnl_dollars",
                  "noise_p5_pnl_dollars", "noise_p95_pnl_dollars",
                  "noise_pct_profitable", "n_trials"):
            assert k in out
        assert 0.0 <= out["noise_pct_profitable"] <= 100.0
        assert out["noise_p5_pnl_dollars"] <= out["noise_p95_pnl_dollars"]

    def test_costs_are_applied_to_every_trial(self, rule, cache):
        """A config that charges costs must lower the noise distribution too --
        otherwise the robustness check is run on a different strategy than the
        one being evaluated."""
        cfg = ev_execution.ExecutionConfig(
            name="t", costs=ev_execution.CostModel(commission_bps=100.0))
        free = rb.noise_test(rule, cache, n_trials=10, sigma_bps=10.0, seed=0)
        paid = rb.noise_test(rule, cache, n_trials=10, sigma_bps=10.0, seed=0,
                             config=cfg)
        assert paid["noise_mean_pnl_dollars"] < free["noise_mean_pnl_dollars"]

    def test_empty_cache_reason(self, rule):
        out = rb.noise_test(rule, {}, n_trials=5)
        assert out["noise_pct_profitable"] is None
        assert "no usable symbols" in out["noise_reason"]

    def test_nonpositive_sigma_reason(self, rule, cache):
        out = rb.noise_test(rule, cache, n_trials=5, sigma_bps=0.0)
        assert out["noise_pct_profitable"] is None
        assert "positive" in out["noise_reason"]


# --------------------------------------------------------------- price MCPT


class TestPriceMCPT:
    def test_preserves_first_price_and_length(self):
        df = _frame(100.0 * np.exp(np.cumsum(
            np.random.default_rng(0).normal(0, 0.01, 100))))
        out = rb._shuffled_frame(df, np.random.default_rng(1))
        assert len(out) == len(df)
        assert out["close"].iloc[0] == pytest.approx(df["close"].iloc[0])

    def test_preserves_the_return_multiset(self):
        df = _frame(100.0 * np.exp(np.cumsum(
            np.random.default_rng(0).normal(0, 0.01, 100))))
        out = rb._shuffled_frame(df, np.random.default_rng(1))
        orig = np.sort(np.diff(np.log(df["close"].to_numpy())))
        new = np.sort(np.diff(np.log(out["close"].to_numpy())))
        assert np.allclose(orig, new)

    def test_preserves_intrabar_geometry(self):
        df = _frame(100.0 * np.exp(np.cumsum(
            np.random.default_rng(0).normal(0, 0.01, 100))))
        out = rb._shuffled_frame(df, np.random.default_rng(1))
        assert (out["high"] >= out[["open", "close"]].max(axis=1) - 1e-9).all()
        assert (out["low"] <= out[["open", "close"]].min(axis=1) + 1e-9).all()

    def test_actually_reorders(self):
        df = _frame(100.0 * np.exp(np.cumsum(
            np.random.default_rng(0).normal(0, 0.01, 100))))
        out = rb._shuffled_frame(df, np.random.default_rng(1))
        assert not np.allclose(out["close"], df["close"])

    def test_too_short_frame_returns_none(self):
        assert rb._shuffled_frame(_frame([100.0, 101.0]),
                                  np.random.default_rng(0)) is None

    def test_p_value_in_range_and_deterministic(self, rule, cache):
        a = rb.price_mcpt(rule, cache, n_perm=30, seed=5)
        b = rb.price_mcpt(rule, cache, n_perm=30, seed=5)
        assert a == b
        assert 0.0 < a["price_mcpt_p"] <= 1.0

    def test_empty_cache_reason(self, rule):
        out = rb.price_mcpt(rule, {}, n_perm=5)
        assert out["price_mcpt_p"] is None
        assert "no usable symbols" in out["price_mcpt_reason"]

    def test_too_few_usable_permutations_reason(self, rule):
        short = {"AAA": _frame([100.0, 101.0])}
        out = rb.price_mcpt(rule, short, n_perm=30)
        assert out["price_mcpt_p"] is None
        assert "usable permutations" in out["price_mcpt_reason"]


# --------------------------------------------------------------- trade order


def _trades(pnl_pcts):
    return pd.DataFrame({"pnl_pct": pnl_pcts,
                         "pnl_dollars": [p * 100.0 for p in pnl_pcts]})


class TestTradeOrder:
    def test_deterministic_under_seed(self):
        t = _trades([5, -3, 8, -6, 2, 4, -1, 7])
        assert (rb.trade_order_mc(t, n_trials=200, seed=0)
                == rb.trade_order_mc(t, n_trials=200, seed=0))

    def test_final_return_is_order_invariant(self):
        """prod(1 + r_i) commutes, so EVERY permutation ends at the same equity.
        This is why the method reports final_return_pct once instead of a
        percentile band around it -- such a band would always have width zero.
        Drawdown is the only thing permutation can move."""
        r = np.array([20, -15, 30, -25, 10, -8, 12, -5]) / 100.0
        rng = np.random.default_rng(0)
        finals = {round(float(np.prod(1.0 + rng.permutation(r))), 12)
                  for _ in range(200)}
        assert len(finals) == 1

        out = rb.trade_order_mc(_trades([20, -15, 30, -25, 10, -8, 12, -5]),
                                n_trials=200, seed=0)
        assert out["final_return_pct"] == pytest.approx(
            100.0 * (float(np.prod(1.0 + r)) - 1.0), abs=0.01)
        assert "final_return_p5_pct" not in out

    def test_drawdown_distribution_does_vary(self):
        """The flip side: drawdown is path-dependent, so it must show spread."""
        t = _trades([20, -15, 30, -25, 10, -8, 12, -5])
        out = rb.trade_order_mc(t, n_trials=500, seed=0)
        assert out["mdd_p95_pct"] > out["mdd_p5_pct"]

    def test_drawdown_distribution_brackets_the_observed(self):
        t = _trades([5, -3, 8, -6, 2, 4, -1, 7, -4, 6])
        out = rb.trade_order_mc(t, n_trials=500, seed=0)
        assert (out["mdd_worst_pct"] >= out["mdd_p95_pct"]
                >= out["mdd_median_pct"] >= out["mdd_p5_pct"])
        assert 0.0 <= out["observed_mdd_percentile"] <= 100.0

    def test_worst_case_ordering_is_detected(self):
        """All losses first is the worst possible ordering, so the observed
        drawdown must sit at the top of the shuffled distribution."""
        t = _trades([-10, -10, -10, 15, 15, 15])
        out = rb.trade_order_mc(t, n_trials=500, seed=0)
        assert out["observed_mdd_percentile"] > 90.0

    def test_empty_reason(self):
        out = rb.trade_order_mc(pd.DataFrame({"pnl_pct": []}))
        assert out["mdd_median_pct"] is None
        assert "no realized trades" in out["order_reason"]

    def test_too_few_trades_reason(self):
        out = rb.trade_order_mc(_trades([1, 2, 3]))
        assert out["mdd_median_pct"] is None
        assert "(< 5)" in out["order_reason"]

    def test_total_loss_reason(self):
        out = rb.trade_order_mc(_trades([5, -100, 3, 2, 1, 4]))
        assert out["mdd_median_pct"] is None
        assert "compounding undefined" in out["order_reason"]

    def test_integrates_with_the_engine(self, rule, cache):
        out = rb.trade_order_mc(tr.simulate(rule, cache), n_trials=100, seed=0)
        assert out["n_trades"] >= 5


# --------------------------------------------------------------- PBO


class TestPBO:
    def test_combination_count(self):
        M = np.random.default_rng(0).normal(0, 0.01, (600, 20))
        assert rb.pbo(M, n_splits=8)["n_combinations"] == 70     # C(8, 4)

    def test_ranking_sharpe_float_noise_column_scores_zero(self):
        """A constant 0.001 column has sd ~6e-19 in float64 -- the old bare
        `sd > 0` guard let it through and scored it like a monster instead of
        the information-free column it is (which pbo() would then always
        pick in-sample)."""
        assert rb._sharpe(np.full(50, 0.001)) == 0.0
        assert rb._sharpe(np.zeros(50)) == 0.0
        real = np.random.default_rng(0).normal(0.001, 0.01, 50)
        assert rb._sharpe(real) != 0.0

    def test_noise_pbo_is_high_but_realization_dependent(self):
        """Pins the caveat in pbo()'s docstring rather than the textbook claim.

        "No skill implies PBO ~ 0.5" is an expectation OVER DATASETS, not a
        property of any one dataset: across 8 pure-noise realizations here PBO
        spans roughly a quarter to four-fifths. The mean stays well away from
        zero, which is the part that is actually stable and actually useful.
        """
        vals = [rb.pbo(np.random.default_rng(s).normal(0, 0.01, (600, 20)),
                       n_splits=8)["pbo"] for s in range(8)]
        assert max(vals) - min(vals) > 0.2, "expected wide spread across seeds"
        assert 0.2 <= float(np.mean(vals)) <= 0.8
        assert min(vals) > 0.1

    def test_dominant_column_generalizes_on_every_realization(self):
        """The property PBO reports reliably: a column with a genuine edge is
        picked in-sample and stays best out-of-sample, driving PBO to ~0 for
        every seed -- strictly separated from the noise case above."""
        for s in range(5):
            rng = np.random.default_rng(s)
            M = rng.normal(0, 0.01, (600, 20))
            M[:, 3] += 0.02                          # unmistakable real edge
            out = rb.pbo(M, n_splits=8)
            assert out["pbo"] < 0.05
            assert out["median_logit"] > 0

    def test_accepts_a_dataframe(self):
        M = pd.DataFrame(np.random.default_rng(1).normal(0, 0.01, (400, 6)))
        assert rb.pbo(M, n_splits=6)["pbo"] is not None

    def test_custom_metric(self):
        M = np.random.default_rng(2).normal(0, 0.01, (400, 8))
        out = rb.pbo(M, n_splits=6, metric=lambda x: float(np.mean(x)))
        assert out["pbo"] is not None

    def test_constant_column_does_not_win(self):
        """A zero-variance column must not score +inf and sweep every split."""
        rng = np.random.default_rng(3)
        M = rng.normal(0.001, 0.01, (400, 5))
        M[:, 0] = 0.0
        out = rb.pbo(M, n_splits=6)
        assert out["pbo"] is not None

    def test_single_column_reason(self):
        out = rb.pbo(np.random.default_rng(0).normal(0, 1, (100, 1)))
        assert out["pbo"] is None
        assert "at least 2 configurations" in out["pbo_reason"]

    def test_odd_splits_reason(self):
        out = rb.pbo(np.random.default_rng(0).normal(0, 1, (100, 4)), n_splits=7)
        assert out["pbo"] is None
        assert "even and >= 4" in out["pbo_reason"]

    def test_too_few_periods_reason(self):
        out = rb.pbo(np.random.default_rng(0).normal(0, 1, (10, 4)), n_splits=8)
        assert out["pbo"] is None
        assert "periods" in out["pbo_reason"]

    def test_non_finite_reason(self):
        M = np.random.default_rng(0).normal(0, 1, (100, 4))
        M[5, 2] = np.nan
        out = rb.pbo(M, n_splits=4)
        assert out["pbo"] is None
        assert "non-finite" in out["pbo_reason"]


# --------------------------------------------------------------- CPCV


class TestCPCV:
    def test_split_count_matches_the_combinatorics(self):
        splits = list(rb.cpcv_splits(600, n_groups=6, k_test=2, embargo_pct=0.0))
        assert len(splits) == 15                     # C(6, 2)
        assert rb.cpcv_report(600, n_groups=6, k_test=2)["n_splits"] == 15

    def test_train_and_test_are_disjoint(self):
        for train, test in rb.cpcv_splits(600, n_groups=6, k_test=2,
                                          embargo_pct=0.01):
            assert not set(train) & set(test)

    def test_test_blocks_cover_every_observation_across_splits(self):
        seen = set()
        for _, test in rb.cpcv_splits(600, n_groups=6, k_test=2, embargo_pct=0.0):
            seen |= set(test.tolist())
        assert seen == set(range(600))

    def test_embargo_removes_observations_after_each_test_block(self):
        no_emb = list(rb.cpcv_splits(600, n_groups=6, k_test=1, embargo_pct=0.0))
        with_emb = list(rb.cpcv_splits(600, n_groups=6, k_test=1, embargo_pct=0.05))
        # first split's test block is the leading group, so an embargo follows it
        assert len(with_emb[0][0]) < len(no_emb[0][0])

    def test_purge_removes_overlapping_labels(self):
        """A training observation whose label resolves inside the test window
        has seen the test period's outcome and must not survive purging."""
        n = 300
        t1 = np.minimum(np.arange(n) + 20, n - 1)    # 20-bar label horizon
        unpurged = list(rb.cpcv_splits(n, n_groups=6, k_test=1, embargo_pct=0.0))
        purged = list(rb.cpcv_splits(n, n_groups=6, k_test=1, embargo_pct=0.0,
                                     t1=t1))
        assert len(purged[1][0]) < len(unpurged[1][0])
        for train, test in purged:
            a, b = test.min(), test.max()
            assert not ((t1[train] >= a) & (train <= b)).any()

    def test_report_flags_whether_purging_is_exact(self):
        n = 300
        assert rb.cpcv_report(n, n_groups=6, k_test=2)["purge"] == "embargo-only"
        assert rb.cpcv_report(n, n_groups=6, k_test=2,
                              t1=np.arange(n))["purge"] == "exact"

    def test_report_bad_config_reason(self):
        out = rb.cpcv_report(300, n_groups=4, k_test=4)
        assert out["n_splits"] is None
        assert "k_test" in out["cpcv_reason"]

    def test_report_too_few_observations_reason(self):
        out = rb.cpcv_report(3, n_groups=6, k_test=2)
        assert out["n_splits"] is None
        assert "observations" in out["cpcv_reason"]

    def test_bad_config_raises(self):
        with pytest.raises(ValueError, match="k_test"):
            list(rb.cpcv_splits(300, n_groups=4, k_test=4))
        with pytest.raises(ValueError, match="observations"):
            list(rb.cpcv_splits(3, n_groups=6, k_test=2))
