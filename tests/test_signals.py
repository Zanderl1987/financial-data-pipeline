"""
test_signals.py — factor derivation, z-scoring, and composite blending.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from analytics import signals, signal_panel, rank_symbols


def _synthetic_fm():
    """Two dates, four symbols, with the fields signals needs."""
    rows = []
    for date in ("2024-06-28", "2024-06-29"):
        for i, sym in enumerate(["A", "B", "C", "D"]):
            rows.append({
                "symbol": sym, "date": pd.Timestamp(date),
                "close": 100 + 10 * i,
                "mom_12_1": 0.1 * i, "ret_63d": 0.05 * i,
                "vol_21d": 0.2 + 0.05 * i,
                "fund_eps": 5 - i, "fund_net_income": 100 - 10 * i,
                "fund_total_assets": 1000.0, "fund_gross_profit": 400 - 20 * i,
                "fund_revenue": 1000.0, "fund_shares": 100.0,
                "si_days_to_cover": 1.0 + i,
                "insider_net_90d": 10.0 * (3 - i),
                "news_score_21d": 0.5 - 0.2 * i,
            })
    return pd.DataFrame(rows)


class TestZScore:
    def test_zero_when_no_spread(self):
        z = signals._zscore(pd.Series([5.0, 5.0, 5.0]))
        assert (z == 0).all()

    def test_standardizes_to_unit_scale(self):
        z = signals._zscore(pd.Series([1.0, 2.0, 3.0, 4.0]))
        assert z.mean() == pytest.approx(0.0, abs=1e-9)
        assert z.std(ddof=0) == pytest.approx(1.0, abs=1e-9)

    def test_all_nan_group_stays_nan(self):
        # A sparse, event-triggered factor (e.g. oil_shock) with no event on
        # this date must NOT be promoted to "present, neutral" — that would
        # get it counted in the composite's renormalization denominator on
        # every date, diluting factors that actually have data that day.
        z = signals._zscore(pd.Series([np.nan, np.nan, np.nan]))
        assert z.isna().all()

    def test_degenerate_group_preserves_missing_entries(self):
        # constant value for present symbols, but one symbol has no value at
        # all — that symbol must stay NaN, not get pulled in as a 0
        z = signals._zscore(pd.Series([5.0, 5.0, np.nan]))
        assert list(z.iloc[:2]) == [0.0, 0.0]
        assert np.isnan(z.iloc[2])


class TestRawSignals:
    def setup_method(self):
        self.raw = signals._raw_signals(_synthetic_fm())

    def test_value_is_earnings_yield(self):
        row = self.raw.iloc[0]
        assert row["value"] == pytest.approx(row["fund_eps"] / row["close"])

    def test_low_vol_tracks_vol(self):
        # Sign-flipped 2026-07-23 — see analytics/signals.py docstring.
        assert (self.raw["low_vol"] == self.raw["vol_21d"]).all()

    def test_quality_present(self):
        assert "quality" in self.raw.columns and self.raw["quality"].notna().any()

    def test_short_pressure_is_negated_days_to_cover(self):
        assert (self.raw["short_pressure"] == -self.raw["si_days_to_cover"]).all()

    def test_insider_flow_scaled_by_shares(self):
        row = self.raw.iloc[0]
        assert row["insider_flow"] == pytest.approx(
            row["insider_net_90d"] / row["fund_shares"])

    def test_insider_flow_unscaled_without_shares(self):
        raw = signals._raw_signals(_synthetic_fm().drop(columns=["fund_shares"]))
        assert (raw["insider_flow"] == raw["insider_net_90d"]).all()

    def test_sentiment_is_news_score(self):
        assert (self.raw["sentiment"] == self.raw["news_score_21d"]).all()


class TestSignalPanel:
    def setup_method(self):
        self.panel = signal_panel(fm=_synthetic_fm())

    def test_composite_present_and_finite(self):
        assert "composite" in self.panel.columns
        assert np.isfinite(self.panel["composite"]).all()

    def test_zscores_centered_per_date(self):
        # each present factor should average ~0 within a date
        for _, grp in self.panel.groupby("date"):
            for f in self.panel.attrs["factors"]:
                assert grp[f].mean() == pytest.approx(0.0, abs=1e-9)

    def test_weights_change_ranking(self):
        base = rank_symbols(fm=_synthetic_fm())
        tilt = rank_symbols(fm=_synthetic_fm(), weights={"value": 10, "low_vol": 0, "quality": 0})
        # value-only ranking is led by the highest earnings-yield symbol (A)
        assert tilt.iloc[0]["symbol"] == "A"
        assert "rank" in base.columns

    def test_empty_input_returns_empty(self):
        assert signal_panel(fm=pd.DataFrame()).empty


class TestComposueRenormalization:
    def test_missing_factor_does_not_nan_composite(self):
        fm = _synthetic_fm()
        fm.loc[fm["symbol"] == "A", "fund_eps"] = np.nan  # A has no value factor
        panel = signal_panel(fm=fm)
        a = panel[panel["symbol"] == "A"]
        assert np.isfinite(a["composite"]).all()  # renormalized over present factors
