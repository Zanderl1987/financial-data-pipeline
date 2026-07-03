"""
test_event_backtest.py — event-study engine + technical rating, synthetic data.

No API keys or stored data required: prices are generated in-memory and the
engine's price loader is bypassed by monkeypatching load_close/_matrix.
"""

import numpy as np
import pandas as pd
import pytest

import event_backtest as eb
from analytics.technical import indicators, tv_rating


def _synthetic_ohlcv(n=400, seed=7, drift=0.0005):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    df = pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.002, n)),
        "high": close * (1 + np.abs(rng.normal(0, 0.004, n))),
        "low":  close * (1 - np.abs(rng.normal(0, 0.004, n))),
        "close": close,
        "volume": rng.integers(1e5, 1e6, n).astype(float),
    }, index=dates)
    return df


class TestTechnical:
    def test_indicator_columns(self):
        out = indicators(_synthetic_ohlcv())
        for col in ("sma200", "ema50", "hull9", "vwma20", "rsi14", "stoch_k",
                    "cci20", "adx14", "ao", "mom10", "macd", "srsi_k",
                    "willr14", "bull_power", "uo", "ich_lead2"):
            assert col in out.columns, col
            assert out[col].notna().any(), f"{col} is all NaN"

    def test_rsi_bounds(self):
        out = indicators(_synthetic_ohlcv())
        rsi = out["rsi14"].dropna()
        assert ((rsi >= 0) & (rsi <= 100)).all()

    def test_rating_range_and_label(self):
        out = tv_rating(_synthetic_ohlcv())
        r = out["rating_all"].dropna()
        assert ((r >= -1) & (r <= 1)).all()
        assert out["rating_all"].equals(((out["rating_ma"] + out["rating_osc"]) / 2))
        tail = out.dropna(subset=["rating_all"])
        assert set(tail["rating_label"].dropna().unique()) <= {
            "strong_sell", "sell", "neutral", "buy", "strong_buy"}

    def test_uptrend_rates_higher_than_downtrend(self):
        up = tv_rating(_synthetic_ohlcv(drift=0.004, seed=1))["rating_all"].iloc[-1]
        dn = tv_rating(_synthetic_ohlcv(drift=-0.004, seed=1))["rating_all"].iloc[-1]
        assert up > dn

    def test_missing_columns_raise(self):
        with pytest.raises(ValueError):
            indicators(pd.DataFrame({"close": [1, 2, 3]}))


class TestEventStudy:
    @pytest.fixture
    def patched_prices(self, monkeypatch):
        px = _synthetic_ohlcv(n=300, seed=3)["close"]
        px.name = "TEST"
        bench = _synthetic_ohlcv(n=300, seed=4)["close"]
        bench.name = "BENCH"
        series = {"TEST": px, "BENCH": bench}

        def fake_load_close(symbol, start=None, end=None, price_table=None):
            return series.get(symbol, pd.Series(dtype=float)).copy()

        def fake_matrix(symbols, start=None, end=None, price_table=None):
            return pd.DataFrame({s: series[s] for s in dict.fromkeys(symbols)
                                 if s in series})

        monkeypatch.setattr(eb, "load_close", fake_load_close)
        monkeypatch.setattr(eb, "load_close_matrix", fake_matrix)
        return px

    def test_car_matches_manual_return(self, patched_prices):
        px = patched_prices
        t0 = px.index[100]
        ev = pd.DataFrame({"date": [t0]})
        res = eb.event_study(ev, symbols="TEST", window=(0, 5))
        manual = px.iloc[105] / px.iloc[99] - 1.0
        assert res.car.loc[0, 5] == pytest.approx(manual)
        assert res.n_events == 1

    def test_entry_lag_shifts_day0(self, patched_prices):
        px = patched_prices
        t0 = px.index[100]
        ev = pd.DataFrame({"date": [t0]})
        res = eb.event_study(ev, symbols="TEST", window=(0, 3), entry_lag=1)
        assert res.events["day0"].iloc[0] == px.index[101]

    def test_events_outside_history_dropped(self, patched_prices):
        ev = pd.DataFrame({"date": [pd.Timestamp("1980-01-05"),
                                    patched_prices.index[100]]})
        res = eb.event_study(ev, symbols="TEST", window=(0, 5))
        assert res.n_events == 1  # 1980 event has no nearby prices

    def test_benchmark_self_events_dropped(self, patched_prices):
        ev = pd.DataFrame({"date": [patched_prices.index[100]],
                           "symbol": ["BENCH"]})
        with pytest.raises(RuntimeError):
            eb.event_study(ev, benchmark="BENCH", window=(0, 5))

    def test_scenario_stop_loss_caps_loss(self, patched_prices):
        ev = pd.DataFrame({"date": patched_prices.index[[50, 100, 150, 200]]})
        sc = eb.scenario(ev, symbols="TEST", holding_days=21,
                         entry_lag=1, stop_loss_pct=2)
        assert len(sc.trades) == 4
        stopped = sc.trades[sc.trades["exit_reason"] == "stop"]
        # a close-based stop exits at the first close beyond -2%
        assert (stopped["days_held"] <= 21).all()
        assert set(sc.trades["exit_reason"]) <= {"stop", "target", "time"}


class TestEventGenerators:
    def test_price_move_events_declusters(self, monkeypatch):
        n = 200
        dates = pd.bdate_range("2021-01-01", periods=n)
        # one sharp 6-day crash in the middle of a flat series
        px = pd.Series(100.0, index=dates)
        px.iloc[100:106] = [95, 90, 86, 84, 83, 82]
        px.iloc[106:] = 82.0
        monkeypatch.setattr(eb, "load_close",
                            lambda *a, **k: px.rename("X"))
        ev = eb.price_move_events("X", pct=10, days=5, direction="down",
                                  min_gap_days=10)
        assert len(ev) == 1
        assert ev["move_pct"].iloc[0] <= -10

    def test_threshold_events_fire_on_cross_only(self, monkeypatch):
        dates = pd.bdate_range("2021-01-01", periods=10)
        vals = pd.Series([10, 20, 35, 40, 38, 25, 15, 32, 31, 12.0],
                         index=dates)
        monkeypatch.setattr(eb, "load_close",
                            lambda *a, **k: vals.rename("VIX"))
        ev = eb.threshold_events("VIX", 30, "above", min_gap_days=0)
        assert list(ev["date"]) == [dates[2], dates[7]]
