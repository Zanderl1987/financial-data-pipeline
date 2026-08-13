"""
Tests for the risk/ratio/factor-attribution extensions in evaluation/stats.py
(sortino_ratio, calmar_ratio, omega_ratio, value_at_risk, conditional_var,
gain_to_pain_ratio, fama_french_factor_attribution) -- added alongside the
backtest.py/event_backtest.py execution-cost and risk-control work, and
previously had zero coverage.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation import stats as ev_stats  # noqa: E402


# ------------------------------------------------------------------ sortino_ratio

def test_sortino_ratio_positive_for_upward_drift():
    rng = np.random.default_rng(0)
    ret = pd.Series(rng.normal(0.001, 0.01, 500))
    res = ev_stats.sortino_ratio(ret)
    assert res["sortino"] is not None
    assert res["sortino"] > 0


def test_sortino_ratio_none_below_min_points():
    assert ev_stats.sortino_ratio(pd.Series([0.01, -0.01]))["sortino"] is None


def test_sortino_ratio_none_when_no_downside():
    ret = pd.Series([0.01] * 10)
    res = ev_stats.sortino_ratio(ret)
    assert res["sortino"] is None
    assert "no downside" in res["sortino_reason"]


# ------------------------------------------------------------------ calmar_ratio

def test_calmar_ratio_basic():
    res = ev_stats.calmar_ratio(cagr_pct=20.0, max_drawdown_pct=-10.0)
    assert res["calmar"] == pytest.approx(2.0)


def test_calmar_ratio_none_on_missing_inputs():
    assert ev_stats.calmar_ratio(None, -10.0)["calmar"] is None
    assert ev_stats.calmar_ratio(20.0, None)["calmar"] is None


def test_calmar_ratio_none_on_zero_drawdown():
    assert ev_stats.calmar_ratio(20.0, 0.0)["calmar"] is None


# ------------------------------------------------------------------ omega_ratio

def test_omega_ratio_all_gains_is_none_no_losses():
    ret = pd.Series([0.01, 0.02, 0.03, 0.01, 0.02])
    res = ev_stats.omega_ratio(ret)
    assert res["omega"] is None
    assert "no losses" in res["omega_reason"]


def test_omega_ratio_balanced():
    ret = pd.Series([0.02, -0.01, 0.02, -0.01, 0.02])
    res = ev_stats.omega_ratio(ret)
    gains = 0.02 * 3
    losses = 0.01 * 2
    assert res["omega"] == pytest.approx(gains / losses, rel=1e-3)


# ------------------------------------------------------------------ VaR / CVaR

def test_value_at_risk_matches_percentile():
    ret = pd.Series(np.linspace(-0.05, 0.05, 100))
    res = ev_stats.value_at_risk(ret, alpha=0.05)
    expected = -np.percentile(ret, 5) * 100
    assert res["var_95_pct"] == pytest.approx(expected, abs=0.1)


def test_conditional_var_worse_than_var():
    """CVaR (expected loss beyond VaR) must be at least as severe as VaR itself."""
    rng = np.random.default_rng(1)
    ret = pd.Series(rng.normal(0, 0.02, 300))
    var = ev_stats.value_at_risk(ret)["var_95_pct"]
    cvar = ev_stats.conditional_var(ret)["cvar_95_pct"]
    assert cvar >= var


def test_var_cvar_none_below_min_points():
    ret = pd.Series([0.01] * 5)
    assert ev_stats.value_at_risk(ret)["var_95_pct"] is None
    assert ev_stats.conditional_var(ret)["cvar_95_pct"] is None


# ------------------------------------------------------------------ gain_to_pain_ratio

def test_gain_to_pain_ratio_basic():
    ret = pd.Series([0.05, -0.02, 0.03, -0.01, 0.00])
    res = ev_stats.gain_to_pain_ratio(ret)
    total_gain, total_loss = 0.08, 0.03
    assert res["gain_to_pain"] == pytest.approx((total_gain - total_loss) / total_loss, abs=0.01)


def test_gain_to_pain_ratio_none_on_zero_loss():
    ret = pd.Series([0.01, 0.02, 0.03])
    assert ev_stats.gain_to_pain_ratio(ret)["gain_to_pain"] is None


# ------------------------------------------------------------------ fama_french_factor_attribution

def test_fama_french_attribution_none_below_min_observations():
    ret = pd.Series(np.random.normal(0, 0.01, 10))
    res = ev_stats.fama_french_factor_attribution(ret)
    assert res["ff_alpha"] is None
    assert "fewer than 30" in res["ff_reason"]


def test_fama_french_attribution_graceful_when_dataset_missing(monkeypatch):
    """No ff_factors pipeline/table exists in this repo yet -- must degrade to a
    reason string, never raise, since backtest()/scenario() call this unconditionally."""
    import query as q

    def fake_load(name, **kwargs):
        raise RuntimeError(f"no such table: {name}")

    monkeypatch.setattr(q, "load", fake_load)
    ret = pd.Series(np.random.default_rng(2).normal(0, 0.01, 60),
                    index=pd.bdate_range("2024-01-01", periods=60))
    res = ev_stats.fama_french_factor_attribution(ret)
    assert res["ff_alpha"] is None
    assert "error" in res["ff_reason"].lower()


def test_fama_french_attribution_recovers_known_beta(monkeypatch):
    """Construct a strategy return series that is exactly beta * Mkt-RF + alpha,
    plus a known alpha, and check the OLS recovers both."""
    import query as q

    dates = pd.bdate_range("2020-01-01", periods=100)
    rng = np.random.default_rng(3)
    mkt_rf = rng.normal(0.0005, 0.01, len(dates))
    true_alpha_daily = 0.0002
    true_beta = 1.5
    strategy_ret = pd.Series(true_alpha_daily + true_beta * mkt_rf, index=dates)

    ff_df = pd.DataFrame({
        "date": list(dates) * 2,
        "factor": ["Mkt-RF"] * len(dates) + ["RF"] * len(dates),
        "value": list(mkt_rf * 100) + [0.0] * len(dates),
        "frequency": ["daily"] * (2 * len(dates)),
    })

    monkeypatch.setattr(q, "load", lambda name, **kwargs: ff_df)
    res = ev_stats.fama_french_factor_attribution(strategy_ret)

    assert res["ff_r_squared"] > 0.99
    assert res["beta_mkt_rf"] == pytest.approx(true_beta, abs=0.05)
    assert res["ff_alpha_ann"] == pytest.approx(true_alpha_daily * 252 * 100, abs=1.0)
