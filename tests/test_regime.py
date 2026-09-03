"""
tests/test_regime.py -- Statistical Jump Model regime detection.

Known-answer tests wherever the ground truth is constructible (a two-block
synthetic feature matrix has a known true state path), plus the properties
that matter: the jump penalty actually buys persistence (fewer switches at
higher penalty on noisy data), features use only trailing information (no
lookahead), and short-history/degenerate inputs degrade to a reason string
rather than raising or returning garbage.
"""

import numpy as np
import pandas as pd
import pytest

from evaluation import regime as rg


def _two_block_features(block=40, n_blocks=5, sep=5.0, noise=0.3, seed=0):
    rng = np.random.default_rng(seed)
    X, true = [], []
    for b in range(n_blocks):
        state = b % 2
        center = np.array([0.0, 0.0]) if state == 0 else np.array([sep, sep])
        X.append(rng.normal(center, noise, size=(block, 2)))
        true.extend([state] * block)
    return np.vstack(X), np.array(true)


def _best_alignment_accuracy(labels: np.ndarray, true: np.ndarray) -> float:
    """Cluster indices are arbitrary -- score against both label orientations
    (only meaningful for k=2, which is all these tests use)."""
    return max((labels == true).mean(), (labels == (1 - true)).mean())


class TestFitJumpModel:
    def test_recovers_known_regime_sequence(self):
        X, true = _two_block_features()
        fit = rg.fit_jump_model(X, k=2, jump_penalty=5.0, seed=1)
        assert _best_alignment_accuracy(fit["labels"], true) == 1.0
        assert fit["converged"]

    def test_higher_jump_penalty_reduces_switches_on_noisy_data(self):
        # noisy enough that an unpenalized clustering would chatter
        X, _ = _two_block_features(block=60, n_blocks=3, sep=1.5, noise=1.0, seed=2)
        low = rg.fit_jump_model(X, k=2, jump_penalty=0.01, seed=3)
        high = rg.fit_jump_model(X, k=2, jump_penalty=50.0, seed=3)
        switches_low = int((np.diff(low["labels"]) != 0).sum())
        switches_high = int((np.diff(high["labels"]) != 0).sum())
        assert switches_high <= switches_low

    def test_empty_cluster_is_reseeded_not_left_empty(self):
        # all points identical: k-means++ style init could pick two centroids
        # that collapse to one cluster taking everything. Must not crash and
        # every point still gets some label in range(k).
        X = np.zeros((30, 2))
        fit = rg.fit_jump_model(X, k=2, jump_penalty=1.0, seed=4)
        assert set(np.unique(fit["labels"])) <= {0, 1}

    def test_too_little_data_raises(self):
        X = np.zeros((3, 2))
        with pytest.raises(ValueError, match="need at least"):
            rg.fit_jump_model(X, k=2)


class TestRegimeFeatures:
    def test_columns_and_no_lookahead(self):
        idx = pd.bdate_range("2020-01-01", periods=200)
        rng = np.random.default_rng(0)
        returns = pd.Series(rng.normal(0.0005, 0.01, 200), index=idx)
        feats_full = rg.regime_features(returns, vol_window=21)
        assert set(feats_full.columns) == {"mean_return", "volatility", "downside_dev"}
        # a trailing-only feature at date t must be identical whether or not
        # data after t exists in the input series
        truncated = returns.iloc[:150]
        feats_trunc = rg.regime_features(truncated, vol_window=21)
        common = feats_full.index.intersection(feats_trunc.index)
        assert len(common) > 0
        pd.testing.assert_frame_equal(feats_full.loc[common], feats_trunc.loc[common])

    def test_drops_warmup_nans(self):
        idx = pd.bdate_range("2020-01-01", periods=50)
        returns = pd.Series(0.001, index=idx)
        feats = rg.regime_features(returns, vol_window=21)
        assert len(feats) == 50 - 21 + 1
        assert not feats.isna().any().any()


class TestLabelRegimes:
    def _two_regime_returns(self, seed=0):
        idx = pd.bdate_range("2020-01-01", periods=400)
        rng = np.random.default_rng(seed)
        calm = rng.normal(0.0015, 0.004, 200)
        stressed = rng.normal(-0.002, 0.03, 200)
        return pd.Series(np.concatenate([calm, stressed]), index=idx)

    def test_worse_regime_is_labeled_zero(self):
        returns = self._two_regime_returns()
        out = rg.label_regimes(returns, k=2, jump_penalty=5.0, vol_window=21, seed=1)
        assert "regime_reason" not in out
        stats = out["regime_stats"]
        assert stats[0]["ann_return_pct"] <= stats[1]["ann_return_pct"]

    def test_too_short_history_reason(self):
        idx = pd.bdate_range("2020-01-01", periods=30)
        returns = pd.Series(0.001, index=idx)
        out = rg.label_regimes(returns, k=2, vol_window=21)
        assert out["labels"] is None
        assert "regime_reason" in out

    def test_n_switches_counted(self):
        returns = self._two_regime_returns()
        out = rg.label_regimes(returns, k=2, jump_penalty=5.0, vol_window=21, seed=1)
        assert out["n_switches"] >= 1
        assert out["n_switches"] < len(out["labels"])  # not chattering every day


class TestWalkForwardRegimes:
    def _series(self, n, seed=0):
        idx = pd.bdate_range("2015-01-01", periods=n)
        rng = np.random.default_rng(seed)
        return pd.Series(rng.normal(0.0004, 0.012, n), index=idx)

    def test_too_short_history_reason(self):
        returns = self._series(100)
        out = rg.walk_forward_regimes(returns, min_train=756)
        assert out["labels"] is None
        assert "regime_reason" in out

    def test_no_lookahead_on_the_causal_portion(self):
        """The definitive causality check, same technique as
        TestRegimeFeatures.test_columns_and_no_lookahead but run through
        the whole walk-forward labeling pipeline: truncated right at a
        refit boundary (feats length exactly 300 for vol_window=21 at
        return-series length 320), so every refit the truncated run
        performs trains on IDENTICAL data to the corresponding refit in
        the full run. Labels on the shared date range must match exactly
        -- proving nothing after the truncation point ever leaked back."""
        full = self._series(600)
        truncated = full.iloc[:320]
        kwargs = dict(k=2, jump_penalty=5.0, vol_window=21, min_train=200,
                     refit_every=50, n_init=3, seed=1)
        out_full = rg.walk_forward_regimes(full, **kwargs)
        out_trunc = rg.walk_forward_regimes(truncated, **kwargs)
        assert "regime_reason" not in out_trunc
        common = out_trunc["labels"].index
        assert len(common) > 0
        pd.testing.assert_series_equal(
            out_full["labels"].loc[common], out_trunc["labels"].loc[common],
            check_names=False)

    def test_warmup_boundary_reported(self):
        full = self._series(900)
        out = rg.walk_forward_regimes(full, min_train=300, refit_every=60,
                                      n_init=3, seed=2)
        feats = rg.regime_features(full, vol_window=21)
        assert out["warmup_end"] == feats.index[299]
        assert out["walk_forward_start"] == feats.index[300]

    def test_labels_cover_the_entire_feature_index(self):
        full = self._series(900)
        out = rg.walk_forward_regimes(full, min_train=300, refit_every=60,
                                      n_init=3, seed=2)
        feats = rg.regime_features(full, vol_window=21)
        assert len(out["labels"]) == len(feats)
        assert list(out["labels"].index) == list(feats.index)
        assert out["n_refits"] >= 2

    def test_n_switches_and_regime_stats_present(self):
        full = self._series(900)
        out = rg.walk_forward_regimes(full, min_train=300, refit_every=60,
                                      n_init=3, seed=2)
        assert out["n_switches"] >= 0
        assert out["n_switches"] < len(out["labels"])
        assert set(out["regime_stats"]) == {0, 1}

    def test_single_window_when_history_barely_meets_min_train(self):
        full = self._series(300)
        feats = rg.regime_features(full, vol_window=21)
        out = rg.walk_forward_regimes(full, min_train=len(feats),
                                      refit_every=60, n_init=3, seed=2)
        assert out["n_refits"] == 1
        assert len(out["labels"]) == len(feats)
        assert out["walk_forward_start"] is None   # nothing after warmup


class TestRegimeReport:
    def test_reuses_tearsheet_headline_metrics_per_regime(self):
        idx = pd.bdate_range("2020-01-01", periods=100)
        rng = np.random.default_rng(5)
        returns = pd.Series(np.concatenate([
            rng.normal(0.01, 0.001, 50), rng.normal(-0.01, 0.001, 50)]), index=idx)
        labels = pd.Series(np.concatenate([np.zeros(50), np.ones(50)]).astype(int),
                           index=idx)
        out = rg.regime_report(returns, labels)
        assert set(out) == {0, 1}
        assert out[0]["sharpe"] is not None
        assert out[0]["cagr_pct"] > out[1]["cagr_pct"]
