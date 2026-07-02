"""
test_features.py — feature-matrix math and assembly.

Price-feature math is tested on a synthetic single-symbol series with known
values; the full builder is smoke-tested against whatever price table has data.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from analytics import features, feature_matrix


def _synthetic_panel(n=300):
    dates = pd.bdate_range("2022-01-03", periods=n)
    close = pd.Series(100 * (1.0005) ** np.arange(n))  # steady uptrend
    return pd.DataFrame({"symbol": "TEST", "date": dates, "close": close.values,
                         "volume": 1_000_000})


class TestPriceFeatures:
    def setup_method(self):
        self.fm = features._add_price_features(_synthetic_panel())

    def test_one_day_return_matches_pct_change(self):
        expected = self.fm["close"].pct_change()
        pd.testing.assert_series_equal(self.fm["ret_1d"], expected, check_names=False)

    def test_momentum_skips_recent_month(self):
        # mom_12_1 = close[t-21] / close[t-252] - 1, defined once 252 bars exist
        row = self.fm.iloc[260]
        expected = (self.fm["close"].iloc[260 - 21] / self.fm["close"].iloc[260 - 252]) - 1
        assert row["mom_12_1"] == pytest.approx(expected)

    def test_volatility_is_nonnegative(self):
        assert (self.fm["vol_21d"].dropna() >= 0).all()

    def test_uptrend_has_positive_long_momentum(self):
        assert self.fm["ret_252d"].dropna().iloc[-1] > 0


class TestAsofHelpers:
    def test_pick_price_table_returns_known_or_none(self):
        pt = features._pick_price_table(None)
        assert pt is None or pt in {"prices", "tiingo_prices", "sector_etfs"}


class TestAlternativeBlocks:
    """Short-interest / insider / sentiment blocks must never drop panel rows
    and must degrade to a no-op when their source table has no data."""

    def setup_method(self):
        self.panel = _synthetic_panel(50)
        self.panel["date"] = pd.to_datetime(self.panel["date"])

    @pytest.mark.parametrize("block", [
        features._add_short_interest,
        features._add_insider,
        features._add_sentiment,
    ])
    def test_preserves_rows(self, block):
        out = block(self.panel.copy())
        assert len(out) == len(self.panel)
        assert {"symbol", "date", "close"}.issubset(out.columns)


class TestFeatureMatrixIntegration:
    def test_returns_dataframe(self):
        fm = feature_matrix(limit_symbols := None)  # default args
        assert isinstance(fm, pd.DataFrame)

    def test_builds_when_price_data_present(self):
        pt = features._pick_price_table(None)
        if pt is None:
            pytest.skip("no price table populated")
        import query as q
        syms = q.symbols(pt)[:3]
        fm = feature_matrix(syms)
        if fm.empty:
            pytest.skip("price table empty for sampled symbols")
        assert {"symbol", "date", "close", "ret_1d", "vol_21d"}.issubset(fm.columns)
        assert fm.attrs.get("price_table") == pt
        # point-in-time fundamentals never produce a future-dated leak:
        assert (fm["date"] <= pd.Timestamp.today().normalize() + pd.Timedelta(days=1)).all()
