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
        ohlcv = _synthetic_ohlcv(n=300, seed=3)
        px = ohlcv["close"]
        px.name = "TEST"
        dollar_vol = (ohlcv["close"] * ohlcv["volume"]).rename("TEST")
        bench = _synthetic_ohlcv(n=300, seed=4)["close"]
        bench.name = "BENCH"
        series = {"TEST": px, "BENCH": bench}
        volumes = {"TEST": dollar_vol}

        def fake_load_close(symbol, start=None, end=None, price_table=None):
            return series.get(symbol, pd.Series(dtype=float)).copy()

        def fake_matrix(symbols, start=None, end=None, price_table=None):
            return pd.DataFrame({s: series[s] for s in dict.fromkeys(symbols)
                                 if s in series})

        def fake_volume_matrix(symbols, start=None, end=None, price_table=None):
            return pd.DataFrame({s: volumes[s] for s in dict.fromkeys(symbols)
                                 if s in volumes})

        monkeypatch.setattr(eb, "load_close", fake_load_close)
        monkeypatch.setattr(eb, "load_close_matrix", fake_matrix)
        monkeypatch.setattr(eb, "load_dollar_volume_matrix", fake_volume_matrix)
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

    def test_scenario_spread_and_slippage_increase_cost(self, patched_prices):
        ev = pd.DataFrame({"date": patched_prices.index[[50, 100, 150, 200]]})
        baseline = eb.scenario(ev, symbols="TEST", holding_days=21, entry_lag=1)
        costly = eb.scenario(ev, symbols="TEST", holding_days=21, entry_lag=1,
                             cost_bps=10, spread_bps=20, slippage_model="sqrt_impact")
        base_rets = baseline.trades.set_index(["symbol", "entry_date"])["return_pct"]
        costly_rets = costly.trades.set_index(["symbol", "entry_date"])["return_pct"]
        assert (costly_rets <= base_rets + 1e-9).all()
        assert (costly_rets < base_rets).any()

    def test_adv_impact_is_a_noop_by_default(self, patched_prices):
        ev = pd.DataFrame({"date": patched_prices.index[[50, 100, 150, 200]]})
        base = eb.scenario(ev, symbols="TEST", holding_days=21, entry_lag=1)
        same = eb.scenario(ev, symbols="TEST", holding_days=21, entry_lag=1,
                           notional=999_999_999.0)  # huge size, but coeff unset
        assert (base.trades["return_pct"] == same.trades["return_pct"]).all()

    def test_adv_impact_scales_with_participation(self, patched_prices):
        ev = pd.DataFrame({"date": patched_prices.index[[50, 100, 150, 200]]})
        small = eb.scenario(ev, symbols="TEST", holding_days=21, entry_lag=1,
                            notional=1_000.0, adv_impact_coeff=10.0)
        big = eb.scenario(ev, symbols="TEST", holding_days=21, entry_lag=1,
                          notional=10_000_000.0, adv_impact_coeff=10.0)
        small_rets = small.trades.set_index(["symbol", "entry_date"])["return_pct"]
        big_rets = big.trades.set_index(["symbol", "entry_date"])["return_pct"]
        # a much larger order against the same liquidity costs strictly more
        assert (big_rets < small_rets).all()

    def test_adv_impact_needs_history_before_day0(self, patched_prices):
        # day0 = first bar in the fixture: no adv_window of history exists
        # yet, so the event should fall back to zero ADV impact rather than
        # crash or apply a garbage estimate.
        ev = pd.DataFrame({"date": [patched_prices.index[0]]})
        sc = eb.scenario(ev, symbols="TEST", holding_days=5, entry_lag=1,
                         notional=10_000_000.0, adv_impact_coeff=10.0)
        no_impact = eb.scenario(ev, symbols="TEST", holding_days=5, entry_lag=1)
        assert sc.trades["return_pct"].iloc[0] == \
            no_impact.trades["return_pct"].iloc[0]

    def test_scenario_atr_stop_does_not_crash_and_produces_trades(self, patched_prices):
        ev = pd.DataFrame({"date": patched_prices.index[[50, 100, 150, 200]]})
        sc = eb.scenario(ev, symbols="TEST", holding_days=21, entry_lag=1,
                         atr_stop_mult=2.0)
        assert len(sc.trades) == 4
        assert set(sc.trades["exit_reason"]) <= {"stop", "target", "time"}

    def test_scenario_metrics_include_new_risk_stats(self, patched_prices):
        ev = pd.DataFrame({"date": patched_prices.index[[50, 100, 150, 200]]})
        sc = eb.scenario(ev, symbols="TEST", holding_days=21, entry_lag=1)
        for key in ("sortino", "calmar", "var_95_pct", "max_drawdown_pct"):
            assert key in sc.metrics


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


class TestEarningsEvents:
    """earnings_events() — unions alpha_vantage_earnings + finnhub_earnings_history."""

    def _patch_sources(self, monkeypatch, av=None, fh=None):
        av_cols = ["ticker", "report_type", "reportedDate", "estimatedEPS",
                   "reportedEPS", "surprisePercentage"]
        fh_cols = ["symbol", "period", "estimate", "actual", "surprisePercent"]
        av_df = pd.DataFrame(av, columns=av_cols) if av else pd.DataFrame(columns=av_cols)
        fh_df = pd.DataFrame(fh, columns=fh_cols) if fh else pd.DataFrame(columns=fh_cols)

        def fake_load(table, *a, **k):
            if table == "alpha_vantage_earnings":
                return av_df
            if table == "finnhub_earnings_history":
                return fh_df
            return pd.DataFrame()

        monkeypatch.setattr(eb.q, "load", fake_load)

    def test_unions_both_sources(self, monkeypatch):
        self._patch_sources(
            monkeypatch,
            av=[["AAPL", "quarterly", "2024-01-25", 2.10, 2.18, 3.8]],
            fh=[["MSFT", "2024-01-25", 2.65, 2.93, 10.6]],
        )
        ev = eb.earnings_events()
        assert set(ev["symbol"]) == {"AAPL", "MSFT"}
        assert len(ev) == 2

    def test_dedup_prefers_alpha_vantage(self, monkeypatch):
        self._patch_sources(
            monkeypatch,
            av=[["AAPL", "quarterly", "2024-01-25", 2.10, 2.18, 3.8]],
            fh=[["AAPL", "2024-01-25", 999, 999, 999]],
        )
        ev = eb.earnings_events()
        assert len(ev) == 1
        assert ev["eps_actual"].iloc[0] == 2.18

    def test_beat_and_min_surprise_filters(self, monkeypatch):
        self._patch_sources(
            monkeypatch,
            fh=[
                ["A", "2024-01-25", 1.00, 1.10, 10.0],   # beat, big
                ["B", "2024-01-25", 1.00, 1.01, 1.0],    # beat, small
                ["C", "2024-01-25", 1.00, 0.80, -20.0],  # miss, big
            ],
        )
        ev = eb.earnings_events(beat=True, min_surprise_pct=5)
        assert list(ev["symbol"]) == ["A"]


class TestRatingChanges:
    """rating_changes() / tv_snapshot_changes() — bucket-change scanning."""

    def _fake_rating_history(self, labels, scores=None):
        dates = pd.bdate_range("2024-01-01", periods=len(labels))
        scores = scores or [
            {"strong_sell": -0.7, "sell": -0.3, "neutral": 0.0,
             "buy": 0.3, "strong_buy": 0.7}[lab] for lab in labels
        ]
        return pd.DataFrame({"rating_label": labels, "rating_all": scores},
                            index=dates)

    def test_detects_bucket_change(self, monkeypatch):
        # neutral -> neutral -> buy -> buy -> strong_buy
        labels = ["neutral", "neutral", "buy", "buy", "strong_buy"]
        df = self._fake_rating_history(labels)
        monkeypatch.setattr("analytics.technical.rating_history",
                            lambda sym, **k: df)
        ev = eb.rating_changes("X", start="2024-01-01", end="2024-01-10")
        assert list(ev["direction"]) == ["upgrade", "upgrade"]
        assert list(ev["from_label"]) == ["neutral", "buy"]
        assert list(ev["to_label"]) == ["buy", "strong_buy"]
        assert (ev["step"] == 1).all()

    def test_direction_filter_keeps_only_downgrades(self, monkeypatch):
        labels = ["buy", "neutral", "strong_buy", "sell"]
        df = self._fake_rating_history(labels)
        monkeypatch.setattr("analytics.technical.rating_history",
                            lambda sym, **k: df)
        ev = eb.rating_changes("X", start="2024-01-01", end="2024-01-10",
                               direction="down")
        assert (ev["direction"] == "downgrade").all()
        assert len(ev) == 2

    def test_min_step_filters_small_jumps(self, monkeypatch):
        # neutral -> buy (step 1), buy -> strong_sell (step 3)
        labels = ["neutral", "buy", "strong_sell"]
        df = self._fake_rating_history(labels)
        monkeypatch.setattr("analytics.technical.rating_history",
                            lambda sym, **k: df)
        ev = eb.rating_changes("X", start="2024-01-01", end="2024-01-10",
                               min_step=2)
        assert len(ev) == 1
        assert ev["step"].iloc[0] == 3

    def test_date_mode_isolates_single_day(self, monkeypatch):
        labels = ["neutral", "buy", "strong_buy", "strong_buy"]
        df = self._fake_rating_history(labels)
        target = df.index[2]
        monkeypatch.setattr("analytics.technical.rating_history",
                            lambda sym, **k: df)
        ev = eb.rating_changes("X", date=target.strftime("%Y-%m-%d"))
        assert len(ev) == 1
        assert ev["date"].iloc[0] == target
        assert ev["to_label"].iloc[0] == "strong_buy"

    def test_no_changes_returns_empty_expected_columns(self, monkeypatch):
        labels = ["buy"] * 5
        df = self._fake_rating_history(labels)
        monkeypatch.setattr("analytics.technical.rating_history",
                            lambda sym, **k: df)
        ev = eb.rating_changes("X", start="2024-01-01", end="2024-01-10")
        assert ev.empty
        assert list(ev.columns) == eb._CHANGE_COLS

    def test_tv_snapshot_changes_requires_two_snapshots(self, monkeypatch):
        single = pd.DataFrame({
            "symbol": ["AAPL", "MSFT"],
            "date": ["2026-07-03", "2026-07-03"],
            "rating_label": ["buy", "neutral"],
            "rating_all": [0.3, 0.0],
        })
        monkeypatch.setattr(eb.q, "load", lambda table: single)
        with pytest.raises(RuntimeError, match="Need >= 2"):
            eb.tv_snapshot_changes()

    def test_tv_snapshot_changes_diffs_two_dates(self, monkeypatch):
        both = pd.DataFrame({
            "symbol":       ["AAPL", "AAPL", "MSFT", "MSFT"],
            "date":         ["2026-07-01", "2026-07-02",
                             "2026-07-01", "2026-07-02"],
            "rating_label": ["neutral", "buy", "sell", "sell"],
            "rating_all":   [0.0, 0.3, -0.3, -0.3],
        })
        monkeypatch.setattr(eb.q, "load", lambda table: both)
        ev = eb.tv_snapshot_changes()
        assert len(ev) == 1
        assert ev["symbol"].iloc[0] == "AAPL"
        assert ev["source"].iloc[0] == "tv_snapshot"
        assert ev["direction"].iloc[0] == "upgrade"
