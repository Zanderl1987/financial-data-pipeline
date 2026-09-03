"""
tests/test_meta_label.py -- meta-labeling on top of evaluation/trades.py.

The one property that would silently invalidate everything else in this
module if it broke is causality in walk_forward_meta_labels: an early
trade's out-of-sample probability must be identical whether or not later
trades/features exist in the frame. That gets its own dedicated test,
not just an assertion in a docstring. Everything else is either a
known-answer check (separable data, an all-one-class training window) or
an end-to-end synthetic scenario where a feature is constructed to
actually predict the label, so filtering by it should measurably improve
win rate -- the actual question meta-labeling exists to answer.
"""

import numpy as np
import pandas as pd
import pytest

from evaluation import meta_label as ml
from evaluation import trades as tr
from evaluation.contracts import TradeRule


def _frame(closes, entries, exits, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"close": np.asarray(closes, dtype=float),
                         "_e": np.asarray(entries, dtype=bool),
                         "_x": np.asarray(exits, dtype=bool)}, index=idx)


class TestTripleBarrierLabels:
    def test_sign_of_pnl(self):
        trades = pd.DataFrame({"pnl_pct": [1.5, -2.0, 0.0, 3.3]})
        out = ml.triple_barrier_labels(trades)
        assert out.tolist() == [1, 0, 0, 1]


class TestBuildFeatures:
    def test_trailing_return_known_answer(self):
        closes = [100.0] * 25
        closes[24] = 110.0  # entry_signal at loc 24: last close before the jump
        idx = pd.bdate_range("2020-01-01", periods=25)
        df = pd.DataFrame({"close": closes}, index=idx)
        trades = pd.DataFrame({"symbol": ["TEST"],
                               "entry_signal_date": [idx[24]]})
        feats = ml.build_features(trades, {"TEST": df}, windows=(5, 10, 21))
        # close[24]=110 vs close[24-5]=close[19]=100 -> +10%
        assert feats.loc[0, "ret_5d"] == pytest.approx(0.10)

    def test_missing_symbol_is_nan_not_dropped(self):
        trades = pd.DataFrame({"symbol": ["GHOST"],
                               "entry_signal_date": [pd.Timestamp("2020-01-01")]})
        feats = ml.build_features(trades, {}, windows=(5,))
        assert len(feats) == 1
        assert feats.isna().all(axis=1).iloc[0]

    def test_insufficient_history_is_nan(self):
        idx = pd.bdate_range("2020-01-01", periods=10)
        df = pd.DataFrame({"close": [100.0] * 10}, index=idx)
        trades = pd.DataFrame({"symbol": ["TEST"], "entry_signal_date": [idx[5]]})
        feats = ml.build_features(trades, {"TEST": df}, windows=(21,))
        assert feats.isna().all(axis=1).iloc[0]

    def test_centered_uses_future_bars_known_answer(self):
        idx = pd.bdate_range("2020-01-01", periods=41)
        closes = [100.0] * 41
        closes[22] = 120.0   # two bars AFTER loc=20 -- visible only if centered
        df = pd.DataFrame({"close": closes}, index=idx)
        trades = pd.DataFrame({"symbol": ["TEST"], "entry_signal_date": [idx[20]]})
        trailing = ml.build_features(trades, {"TEST": df}, windows=(4,), centered=False)
        centered = ml.build_features(trades, {"TEST": df}, windows=(4,), centered=True)
        assert trailing.loc[0, "ret_4d"] == pytest.approx(0.0)
        assert centered.loc[0, "ret_4d"] == pytest.approx(0.20)

    def test_centered_default_is_false_backward_compatible(self):
        idx = pd.bdate_range("2020-01-01", periods=25)
        closes = [100.0] * 24 + [110.0]
        df = pd.DataFrame({"close": closes}, index=idx)
        trades = pd.DataFrame({"symbol": ["TEST"], "entry_signal_date": [idx[24]]})
        default = ml.build_features(trades, {"TEST": df}, windows=(5, 10, 21))
        explicit = ml.build_features(trades, {"TEST": df}, windows=(5, 10, 21),
                                     centered=False)
        pd.testing.assert_frame_equal(default, explicit)

    def test_centered_insufficient_future_history_is_nan(self):
        idx = pd.bdate_range("2020-01-01", periods=25)
        df = pd.DataFrame({"close": [100.0] * 25}, index=idx)
        trades = pd.DataFrame({"symbol": ["TEST"], "entry_signal_date": [idx[24]]})
        feats = ml.build_features(trades, {"TEST": df}, windows=(4,), centered=True)
        assert feats.isna().all(axis=1).iloc[0]

    def test_default_indicator_cols_none_is_unchanged(self):
        idx = pd.bdate_range("2020-01-01", periods=25)
        closes = [100.0] * 24 + [110.0]
        df = pd.DataFrame({"close": closes}, index=idx)
        trades = pd.DataFrame({"symbol": ["TEST"], "entry_signal_date": [idx[24]]})
        default = ml.build_features(trades, {"TEST": df}, windows=(5, 10, 21))
        explicit = ml.build_features(trades, {"TEST": df}, windows=(5, 10, 21),
                                     indicator_cols=None)
        pd.testing.assert_frame_equal(default, explicit)

    def test_no_lookahead(self):
        idx = pd.bdate_range("2020-01-01", periods=40)
        rng = np.random.default_rng(0)
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 40)))
        df_full = pd.DataFrame({"close": close}, index=idx)
        df_trunc = df_full.iloc[:30]
        trades = pd.DataFrame({"symbol": ["TEST", "TEST"],
                               "entry_signal_date": [idx[25], idx[25]]})
        full = ml.build_features(trades.iloc[[0]], {"TEST": df_full}, windows=(5, 10))
        trunc = ml.build_features(trades.iloc[[1]], {"TEST": df_trunc}, windows=(5, 10))
        np.testing.assert_allclose(full.iloc[0].to_numpy(dtype=float),
                                   trunc.iloc[0].to_numpy(dtype=float))


class TestBuildFeaturesIndicators:
    def _ohlc(self, n=60, seed=5):
        idx = pd.bdate_range("2020-01-01", periods=n)
        rng = np.random.default_rng(seed)
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
        high = close * 1.01
        low = close * 0.99
        return pd.DataFrame({"open": close, "high": high, "low": low,
                             "close": close}, index=idx), idx

    def test_matches_analytics_technical_indicators(self):
        df, idx = self._ohlc()
        trades = pd.DataFrame({"symbol": ["TEST"], "entry_signal_date": [idx[40]]})
        feats = ml.build_features(trades, {"TEST": df}, windows=(5,),
                                  indicator_cols=["rsi14", "macd"])
        from analytics import technical as tech
        ind = tech.indicators(df)
        assert feats.loc[0, "ind_rsi14"] == pytest.approx(ind.loc[idx[40], "rsi14"])
        assert feats.loc[0, "ind_macd"] == pytest.approx(ind.loc[idx[40], "macd"])

    def test_missing_ohlc_columns_gives_nan_not_error(self):
        idx = pd.bdate_range("2020-01-01", periods=30)
        df = pd.DataFrame({"close": [100.0] * 30}, index=idx)   # no open/high/low
        trades = pd.DataFrame({"symbol": ["TEST"], "entry_signal_date": [idx[25]]})
        feats = ml.build_features(trades, {"TEST": df}, windows=(5,),
                                  indicator_cols=["rsi14"])
        assert pd.isna(feats.loc[0, "ind_rsi14"])

    def test_insufficient_indicator_history_is_nan(self):
        df, idx = self._ohlc()
        # rsi14 needs 14 bars of history -- day 2 has none
        trades = pd.DataFrame({"symbol": ["TEST"], "entry_signal_date": [idx[2]]})
        feats = ml.build_features(trades, {"TEST": df}, windows=(1,),
                                  indicator_cols=["rsi14"])
        assert pd.isna(feats.loc[0, "ind_rsi14"])

    def test_missing_symbol_is_nan(self):
        trades = pd.DataFrame({"symbol": ["GHOST"],
                               "entry_signal_date": [pd.Timestamp("2020-01-01")]})
        feats = ml.build_features(trades, {}, windows=(5,), indicator_cols=["rsi14"])
        assert pd.isna(feats.loc[0, "ind_rsi14"])

    def test_columns_prefixed_ind(self):
        df, idx = self._ohlc()
        trades = pd.DataFrame({"symbol": ["TEST"], "entry_signal_date": [idx[40]]})
        feats = ml.build_features(trades, {"TEST": df}, windows=(5,),
                                  indicator_cols=["rsi14", "adx14"])
        assert list(feats.columns) == ["ret_5d", "volatility", "dist_from_sma",
                                       "ind_rsi14", "ind_adx14"]


class TestLogisticCore:
    def test_recovers_separable_boundary(self):
        rng = np.random.default_rng(1)
        n = 200
        x = rng.normal(0, 1, n)
        y = (x > 0).astype(float)
        X = np.column_stack([x])
        w = ml.fit_logistic(ml._add_bias(X), y, l2=0.01)
        preds = (ml.predict_proba(X, w) >= 0.5).astype(float)
        assert (preds == y).mean() > 0.95

    def test_l2_shrinks_weights(self):
        rng = np.random.default_rng(2)
        n = 300
        x = rng.normal(0, 1, n)
        y = (x + rng.normal(0, 0.1, n) > 0).astype(float)
        X = np.column_stack([x])
        w_low = ml.fit_logistic(ml._add_bias(X), y, l2=0.001)
        w_high = ml.fit_logistic(ml._add_bias(X), y, l2=100.0)
        assert abs(w_high[1]) < abs(w_low[1])


class TestWalkForwardCausality:
    def _rule_trades(self, n_symbols=8, n_days=250, seed=3):
        rng = np.random.default_rng(seed)
        cache, rows = {}, []
        for k in range(n_symbols):
            idx = pd.bdate_range("2020-01-01", periods=n_days)
            close = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, n_days)))
            df = pd.DataFrame({"close": close}, index=idx)
            cache[f"S{k}"] = df
            entries = np.zeros(n_days, bool)
            exits = np.zeros(n_days, bool)
            entries[10::15] = True
            exits[17::15] = True
            rule = TradeRule(name="r", entries=lambda d: pd.Series(entries, index=d.index),
                             exits=lambda d: pd.Series(exits, index=d.index),
                             side="long", notional=10_000.0)
            rows.extend(tr.simulate_symbol(
                df.index, df["close"], entries, exits,
                np.zeros(n_days, bool), np.zeros(n_days, bool), f"S{k}", 10_000.0))
        trades = pd.DataFrame(rows, columns=tr.TRADE_COLS).sort_values(
            "entry_signal_date").reset_index(drop=True)
        return trades, cache

    def test_early_trade_proba_unaffected_by_later_trades(self):
        trades, cache = self._rule_trades()
        feats = ml.build_features(trades, cache)
        full = ml.walk_forward_meta_labels(trades, feats, min_train=20, refit_every=5)

        cutoff_pos = 40
        cutoff_date = trades["entry_signal_date"].sort_values().iloc[cutoff_pos]
        keep = trades["entry_signal_date"] <= cutoff_date
        trunc_trades = trades[keep].reset_index(drop=True)
        trunc_feats = feats[keep].reset_index(drop=True)
        trunc = ml.walk_forward_meta_labels(trunc_trades, trunc_feats,
                                            min_train=20, refit_every=5)

        # match the truncated frame's rows back to the full frame's by
        # (symbol, entry_signal_date) since positional indices differ after
        # the reset_index above
        merged = trunc.merge(
            full[["symbol", "entry_signal_date", "meta_proba"]],
            on=["symbol", "entry_signal_date"], suffixes=("_trunc", "_full"))
        both_scored = merged.dropna(subset=["meta_proba_trunc", "meta_proba_full"])
        assert len(both_scored) > 0
        assert np.allclose(both_scored["meta_proba_trunc"],
                           both_scored["meta_proba_full"], atol=1e-9)

    def test_warmup_rows_are_nan(self):
        trades, cache = self._rule_trades(n_symbols=2, n_days=200)
        feats = ml.build_features(trades, cache)
        out = ml.walk_forward_meta_labels(trades, feats, min_train=1000, refit_every=5)
        assert out["meta_proba"].isna().all()

    def test_single_class_training_window_does_not_crash(self):
        # every trade a winner -> no class-1/0 split ever exists to fit
        trades = pd.DataFrame({
            "symbol": ["S"] * 60,
            "entry_signal_date": pd.bdate_range("2020-01-01", periods=60),
            "pnl_pct": [1.0] * 60,
        })
        feats = pd.DataFrame({"ret_5d": np.random.default_rng(0).normal(0, 1, 60)})
        out = ml.walk_forward_meta_labels(trades, feats, min_train=10, refit_every=5)
        assert out["meta_proba"].isna().all()


class TestEvaluateMetaFilter:
    def test_no_scored_trades_reason(self):
        trades = pd.DataFrame({"pnl_pct": [1.0, -1.0]})
        out = ml.evaluate_meta_filter(trades.assign(meta_proba=np.nan))
        assert "meta_reason" in out

    def test_filtering_improves_win_rate_when_feature_is_predictive(self):
        # construct trades where a feature genuinely predicts the outcome:
        # positive ret_5d -> win, negative -> loss, plus noise
        rng = np.random.default_rng(4)
        n = 400
        ret_5d = rng.normal(0, 1, n)
        wins = (ret_5d + rng.normal(0, 0.6, n)) > 0
        pnl_pct = np.where(wins, rng.uniform(1, 5, n), -rng.uniform(1, 5, n))
        dates = pd.bdate_range("2020-01-01", periods=n)
        trades = pd.DataFrame({"symbol": "S", "entry_signal_date": dates,
                               "pnl_pct": pnl_pct,
                               "pnl_dollars": pnl_pct * 100,
                               "side": "long", "days_held": 5})
        feats = pd.DataFrame({"ret_5d": ret_5d}, index=trades.index)
        scored = ml.walk_forward_meta_labels(trades, feats, min_train=100, refit_every=10)
        out = ml.evaluate_meta_filter(scored, threshold=0.6)
        assert "meta_reason" not in out
        assert out["filtered"]["win_rate_pct"] > out["unfiltered"]["win_rate_pct"]
