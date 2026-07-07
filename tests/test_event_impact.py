"""
test_event_impact.py — point-in-time driver-exposure classification and the
date-clustering-honest event-study stat (analytics/event_impact.py).

No API keys or stored data required: driver/market/stock returns and prices
are synthetic, with the module's data loaders monkeypatched (same pattern as
tests/test_event_backtest.py).
"""

import os
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from analytics import event_impact as ei


def _dates(n, start="2015-01-02"):
    return pd.bdate_range(start, periods=n)


class TestRollingGrouping:
    @pytest.fixture
    def patched_returns(self, monkeypatch):
        # ~6 years of daily data so a 3y trailing window has full coverage
        # by the later event dates. POS moves WITH oil, NEG moves against it,
        # FLAT has no relationship (should never clear the t-stat bar).
        idx = _dates(1550)
        rng = np.random.default_rng(5)
        oil = pd.Series(rng.normal(0, 0.02, len(idx)), index=idx)
        mkt = pd.Series(rng.normal(0, 0.01, len(idx)), index=idx)
        noise = lambda seed: pd.Series(rng.normal(0, 0.002, len(idx)), index=idx)
        pos = (0.6 * oil + noise(1)).rename("POS")
        neg = (-0.5 * oil + noise(2)).rename("NEG")
        flat = (0.9 * mkt + noise(3)).rename("FLAT")

        def price_from_returns(ret):
            return 100 * (1 + ret).cumprod()

        closes = pd.DataFrame({
            "POS": price_from_returns(pos), "NEG": price_from_returns(neg),
            "FLAT": price_from_returns(flat),
        })

        monkeypatch.setattr(ei, "load_driver_returns",
                            lambda drivers, start=None, end=None:
                                pd.DataFrame({"oil": oil, "spx": mkt}))
        monkeypatch.setattr(ei, "load_close_matrix",
                            lambda symbols, start=None, end=None: closes[
                                [s for s in symbols if s in closes.columns]])
        return idx

    def test_classifies_correct_sign(self, patched_returns):
        idx = patched_returns
        event_dates = [idx[1200], idx[1400]]   # well past the 3y lookback
        grouped = ei._rolling_grouping("oil", event_dates,
                                       universe=["POS", "NEG", "FLAT"], min_t=3.0)
        assert not grouped.empty
        pos_rows = grouped[grouped["symbol"] == "POS"]
        neg_rows = grouped[grouped["symbol"] == "NEG"]
        assert (pos_rows["sign"] == 1).all() and len(pos_rows) == 2
        assert (neg_rows["sign"] == -1).all() and len(neg_rows) == 2
        assert "FLAT" not in grouped["symbol"].values

    def test_too_early_event_has_no_classification(self, patched_returns):
        idx = patched_returns
        # only ~40 trading days of history exist before this date — far
        # short of MIN_OBS with a 3y lookback window
        grouped = ei._rolling_grouping("oil", [idx[40]],
                                       universe=["POS", "NEG", "FLAT"], min_t=3.0)
        assert grouped.empty

    def test_empty_when_driver_missing(self, monkeypatch):
        monkeypatch.setattr(ei, "load_driver_returns",
                            lambda drivers, start=None, end=None: pd.DataFrame())
        grouped = ei._rolling_grouping("oil", [pd.Timestamp("2020-01-01")],
                                       universe=["POS"])
        assert grouped.empty
        assert list(grouped.columns) == ["date", "symbol", "sign", "beta_ex_mkt", "t_ex_mkt", "n"]


@dataclass
class _FakeEventStudyResult:
    car: pd.DataFrame
    events: pd.DataFrame
    horizons: pd.DataFrame
    baseline: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    params: dict = field(default_factory=dict)


class TestDateLevelStats:
    def test_matches_manual_across_dates_not_rows(self):
        # 2 dates x 2 symbols each = 4 rows, but only 2 INDEPENDENT dates.
        # Row-pooled and date-pooled means agree here (symmetric), but the
        # date-level n must be 2, not 4, and its t-stat computed across the
        # 2 per-date means.
        events = pd.DataFrame({"date": pd.to_datetime(
            ["2024-01-01", "2024-01-01", "2024-02-01", "2024-02-01"])})
        car = pd.DataFrame({1: [0.01, 0.03, -0.01, -0.03]})
        horizons = pd.DataFrame(index=[1])
        res = _FakeEventStudyResult(car=car, events=events, horizons=horizons)

        dl = ei._date_level_stats(res)
        assert dl.loc[1, "n_dates"] == 2
        per_date_means = [0.02, -0.02]
        expected_mean = np.mean(per_date_means)
        assert dl.loc[1, "mean_pct"] == pytest.approx(round(100 * expected_mean, 2))

    def test_single_date_gives_nan_t_stat(self):
        events = pd.DataFrame({"date": pd.to_datetime(["2024-01-01", "2024-01-01"])})
        car = pd.DataFrame({1: [0.01, 0.02]})
        horizons = pd.DataFrame(index=[1])
        res = _FakeEventStudyResult(car=car, events=events, horizons=horizons)
        dl = ei._date_level_stats(res)
        assert dl.loc[1, "n_dates"] == 1
        assert np.isnan(dl.loc[1, "t_stat"])


class TestOilShockSignal:
    @pytest.fixture
    def patched(self, monkeypatch):
        event_date = pd.Timestamp("2024-06-03")
        idx = pd.bdate_range("2024-05-01", periods=40)
        events = pd.DataFrame({"date": [event_date]})
        grouped = pd.DataFrame({
            "date": [event_date, event_date],
            "symbol": ["BIG", "SMALL"],
            "sign": [1, 1],
            "beta_ex_mkt": [0.9, 0.3],
            "t_ex_mkt": [4.0, 3.5],
            "n": [500, 500],
        })
        closes = pd.DataFrame({"BIG": 1.0, "SMALL": 1.0}, index=idx)

        monkeypatch.setattr(ei, "price_move_events",
                            lambda symbol, pct, days, start=None, min_gap_days=10:
                                events.copy() if pct > 0 else events.iloc[0:0].copy())
        monkeypatch.setattr(ei, "_rolling_grouping",
                            lambda driver, event_dates, universe=None, min_t=3.0,
                                   lookback_years=3, end=None: grouped)
        monkeypatch.setattr(ei, "load_close_matrix",
                            lambda symbols, start=None, end=None: closes[
                                [s for s in symbols if s in closes.columns]])
        return event_date

    def test_scales_by_beta_ex_mkt_not_flat(self, patched):
        event_date = patched
        out = ei.oil_shock_signal(symbols=["BIG", "SMALL"], reaction_days=2)
        assert not out.empty
        big = out.loc[out["symbol"] == "BIG", "oil_shock_raw"].iloc[0]
        small = out.loc[out["symbol"] == "SMALL", "oil_shock_raw"].iloc[0]
        # same event, same direction (+1) -> both positive, but NOT identical:
        # BIG's larger measured exposure (0.9 vs 0.3) must carry a larger score
        assert big > small > 0
        assert big == pytest.approx(0.9)
        assert small == pytest.approx(0.3)
