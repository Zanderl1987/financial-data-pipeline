"""
test_analytics.py — verify the analytics package imports and function signatures.

No API keys or data files required.  Tests confirm:
  - every public function is importable from analytics
  - functions accept their documented parameters without TypeError
  - functions return a DataFrame (even when empty) rather than raising
"""

import sys
import os
import inspect
import pytest
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


# ── Import guard ───────────────────────────────────────────────────────────────

class TestAnalyticsImports:
    def test_import_analytics_package(self):
        import analytics  # noqa: F401

    def test_all_public_exports_importable(self):
        import analytics
        missing = [name for name in analytics.__all__ if not hasattr(analytics, name)]
        assert not missing, f"Names in __all__ but missing from package: {missing}"

    def test_submodule_imports(self):
        from analytics import fundamentals   # noqa: F401
        from analytics import options        # noqa: F401
        from analytics import events         # noqa: F401
        from analytics import macro          # noqa: F401
        from analytics import sectors        # noqa: F401
        from analytics import short_interest # noqa: F401


# ── Function signatures ────────────────────────────────────────────────────────

class TestFunctionSignatures:
    """Verify each public function has the parameters its docstring advertises."""

    def _sig(self, func):
        return list(inspect.signature(func).parameters.keys())

    # fundamentals
    def test_yoy_growth_params(self):
        from analytics import yoy_growth
        assert "symbols" in self._sig(yoy_growth)
        assert "metric" in self._sig(yoy_growth)

    def test_valuation_params(self):
        from analytics import valuation
        assert "symbols" in self._sig(valuation)

    def test_top_by_metric_params(self):
        from analytics import top_by_metric
        assert "metric" in self._sig(top_by_metric)
        assert "n" in self._sig(top_by_metric)

    # options
    def test_iv_summary_params(self):
        from analytics import iv_summary
        assert "symbol" in self._sig(iv_summary)
        assert "date" in self._sig(iv_summary)

    def test_put_call_ratio_params(self):
        from analytics import put_call_ratio
        assert "symbol" in self._sig(put_call_ratio)

    # events
    def test_upcoming_earnings_params(self):
        from analytics import upcoming_earnings
        assert "days_ahead" in self._sig(upcoming_earnings)

    def test_insider_sentiment_params(self):
        from analytics import insider_sentiment
        assert "symbol" in self._sig(insider_sentiment)
        assert "days" in self._sig(insider_sentiment)

    def test_dividend_history_params(self):
        from analytics import dividend_history
        assert "symbols" in self._sig(dividend_history)
        assert "start" in self._sig(dividend_history)

    def test_dividend_calendar_params(self):
        from analytics import dividend_calendar
        assert "days_ahead" in self._sig(dividend_calendar)

    def test_news_sentiment_params(self):
        from analytics import news_sentiment
        assert "symbols" in self._sig(news_sentiment)
        assert "days" in self._sig(news_sentiment)

    def test_sentiment_summary_params(self):
        from analytics import sentiment_summary
        assert "symbols" in self._sig(sentiment_summary)

    # macro
    def test_rate_environment_params(self):
        from analytics import rate_environment
        assert "start" in self._sig(rate_environment)

    def test_inversion_params(self):
        from analytics import inversion
        assert "start" in self._sig(inversion)

    def test_credit_spreads_params(self):
        from analytics import credit_spreads
        assert "start" in self._sig(credit_spreads)

    # sectors
    def test_sector_performance_params(self):
        from analytics import sector_performance
        sig = self._sig(sector_performance)
        assert "start" in sig
        assert "end" in sig

    def test_sector_vs_spy_params(self):
        from analytics import sector_vs_spy
        assert "start" in self._sig(sector_vs_spy)

    def test_sector_rotation_params(self):
        from analytics import sector_rotation
        assert "lookback_days" in self._sig(sector_rotation)

    # short interest
    def test_squeeze_candidates_params(self):
        from analytics import squeeze_candidates
        sig = self._sig(squeeze_candidates)
        assert "min_short_pct" in sig
        assert "max_days_to_cover" in sig

    def test_short_change_params(self):
        from analytics import short_change
        assert "symbols" in self._sig(short_change)
        assert "periods" in self._sig(short_change)

    def test_ftd_pressure_params(self):
        from analytics import ftd_pressure
        assert "top_n" in self._sig(ftd_pressure)

    def test_short_vs_ftd_params(self):
        from analytics import short_vs_ftd
        assert "symbols" in self._sig(short_vs_ftd)


# ── Return type: functions should return DataFrame, not raise, when table empty ─

class TestEmptyDataBehavior:
    """
    With no data files on disk, load() returns an empty DataFrame.
    Analytics functions should propagate that gracefully (return empty DF,
    not raise AttributeError / KeyError).
    """

    def test_rate_environment_empty_returns_df(self):
        from analytics import rate_environment
        result = rate_environment()
        assert isinstance(result, pd.DataFrame)

    def test_inversion_empty_returns_df(self):
        from analytics import inversion
        result = inversion()
        assert isinstance(result, pd.DataFrame)

    def test_credit_spreads_empty_returns_df(self):
        from analytics import credit_spreads
        result = credit_spreads()
        assert isinstance(result, pd.DataFrame)

    def test_upcoming_earnings_empty_returns_df(self):
        from analytics import upcoming_earnings
        result = upcoming_earnings()
        assert isinstance(result, pd.DataFrame)

    def test_squeeze_candidates_empty_returns_df(self):
        from analytics import squeeze_candidates
        result = squeeze_candidates()
        assert isinstance(result, pd.DataFrame)

    def test_ftd_pressure_empty_returns_df(self):
        from analytics import ftd_pressure
        result = ftd_pressure()
        assert isinstance(result, pd.DataFrame)

    def test_sector_performance_empty_returns_df(self):
        from analytics import sector_performance
        result = sector_performance()
        assert isinstance(result, pd.DataFrame)
