"""
test_sentiment_eval.py — verify the sentiment evaluation harness.

No API keys or data files required.  Tests confirm:
  - sentiment_eval imports and exposes the documented functions/params
  - thresholds stay in sync with news_sentiment_pipeline
  - evaluate() recovers a known positive relationship from synthetic data
    and withholds statistics when data is insufficient
"""

import sys
import os
import inspect

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import sentiment_eval as se


class TestImportsAndSignatures:
    def _sig(self, func):
        return list(inspect.signature(func).parameters.keys())

    def test_daily_signals_params(self):
        sig = self._sig(se.daily_signals)
        assert "min_articles" in sig
        assert "start" in sig and "end" in sig

    def test_forward_returns_params(self):
        sig = self._sig(se.forward_returns)
        assert "signals" in sig
        assert "horizons" in sig
        assert "benchmark" in sig

    def test_evaluate_params(self):
        sig = self._sig(se.evaluate)
        assert "panel" in sig
        assert "min_names" in sig

    def test_thresholds_match_scorer(self):
        # sentiment_eval buckets must agree with the pipeline's labeling
        import news_sentiment_pipeline as nsp
        assert se.BULLISH_MIN == nsp.BULLISH_THRESHOLD
        assert se.BEARISH_MAX == nsp.BEARISH_THRESHOLD


def _synthetic_panel(n_days=30, n_syms=10, noise=0.0, seed=7):
    """Panel where fwd_1d is a monotone function of sent_score (+ noise)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-02", periods=n_days)
    rows = []
    for d in dates:
        for i in range(n_syms):
            score = rng.uniform(-1, 1)
            rows.append({
                "symbol": f"S{i}",
                "date": d,
                "sent_score": score,
                "fwd_1d": 0.01 * score + noise * rng.normal(),
            })
    return pd.DataFrame(rows)


class TestEvaluate:
    def test_recovers_positive_signal(self):
        panel = _synthetic_panel()
        res = se.evaluate(panel, horizons=(1,))
        assert 1 in res
        r = res[1]
        assert r["pooled_ic"] > 0.9          # near-perfect rank relation
        assert r["mean_daily_ic"] > 0.9
        assert r["ic_days"] == 30
        assert r["spread_pct"] > 0           # bullish beats bearish

    def test_insufficient_rows_skipped(self):
        panel = _synthetic_panel(n_days=1, n_syms=5)
        res = se.evaluate(panel, horizons=(1,))
        assert res == {} or "mean_daily_ic" not in res.get(1, {})

    def test_daily_ic_withheld_below_min_names(self):
        panel = _synthetic_panel(n_days=30, n_syms=3)
        res = se.evaluate(panel, horizons=(1,), min_names=5)
        assert "mean_daily_ic" not in res[1]

    def test_missing_horizon_column_ignored(self):
        panel = _synthetic_panel()
        res = se.evaluate(panel, horizons=(1, 21))
        assert 1 in res and 21 not in res
