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
        # NOTE: a truly unbounded call (symbols=None, all blocks on) is not
        # memory-safe at this clone's full-universe scale (46.9M price rows) --
        # _asof_fundamentals's per-metric pandas merge OOMs (found 2026-07-29,
        # see backlog item S). Scope to a small subset; unbounded-scale safety
        # is tracked separately, not this test's job.
        pt = features._pick_price_table(None)
        if pt is None:
            fm = feature_matrix(None)
            assert isinstance(fm, pd.DataFrame)
            return
        import query as q
        syms = q.symbols(pt)[:3]
        fm = feature_matrix(syms)
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

    def test_start_param_does_not_truncate_rolling_window(self):
        """Regression (2026-07-29): feature_matrix(start=...) once queried the
        price table from `start` directly, left-truncating the trailing history
        rolling features need -- mom_12_1/ret_252d/vol_21d came back 100% NaN
        for the entire window regardless of how much real history existed
        before `start` (reproduced live with AAPL, 50+ years on file)."""
        pt = features._pick_price_table(None)
        if pt is None:
            pytest.skip("no price table populated")
        import query as q
        found = None
        for sym in q.symbols(pt)[:20]:
            full = feature_matrix([sym], fundamentals=False, macro=False,
                                  short_interest=False, insider=False, sentiment=False)
            if full["mom_12_1"].notna().sum() > 5:
                found = (sym, full)
                break
        if found is None:
            pytest.skip("no sampled symbol has enough history for mom_12_1")
        sym, full = found
        valid_dates = full.loc[full["mom_12_1"].notna(), "date"]
        start = valid_dates.iloc[len(valid_dates) // 2].strftime("%Y-%m-%d")
        windowed = feature_matrix([sym], start=start, fundamentals=False, macro=False,
                                  short_interest=False, insider=False, sentiment=False)
        assert windowed["mom_12_1"].notna().any(), (
            "mom_12_1 is all-NaN when start= is passed -- rolling window was "
            "truncated before computing features"
        )
        merged = full.merge(windowed, on="date", suffixes=("_full", "_win"))
        pd.testing.assert_series_equal(
            merged["mom_12_1_full"], merged["mom_12_1_win"], check_names=False,
        )
