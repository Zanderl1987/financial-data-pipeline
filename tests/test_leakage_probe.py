"""
tests/test_leakage_probe.py -- one-switch decision-timing leakage diagnostic.

one_switch_ablation()'s single-key-changed discipline gets its own tests
(zero changes and two changes must both raise, not just "more than one").
entry_lag_leakage() gets a hand-built price path where the correct answer
is known: a sharp jump lands exactly on the event date and never reverts,
so same-bar execution (the leaky switch) bakes that jump into the trade's
own measured return while next-bar execution (the safe default) enters
after the jump has already happened and captures none of it. The two
entry_lag settings must therefore produce measurably different average
trade returns on this fixture -- not just "some numeric difference", the
specific direction the leak predicts.
"""

import numpy as np
import pandas as pd
import pytest

import event_backtest as eb
from evaluation import leakage_probe as lp


class TestOneSwitchAblation:
    def test_two_changed_keys_raises(self):
        def fn(a, b):
            return a + b
        with pytest.raises(ValueError, match="exactly one key"):
            lp.one_switch_ablation(fn, {"a": 1, "b": 1}, {"a": 2, "b": 2}, lambda r: r)

    def test_zero_changed_keys_raises(self):
        def fn(a):
            return a
        with pytest.raises(ValueError, match="exactly one key"):
            lp.one_switch_ablation(fn, {"a": 1}, {"a": 1}, lambda r: r)

    def test_reports_inflation(self):
        def fn(x):
            return x * 2
        out = lp.one_switch_ablation(fn, {"x": 1}, {"x": 5}, lambda r: r)
        assert out["safe_metric"] == 2
        assert out["leaky_metric"] == 10
        assert out["inflation"] == 8

    def test_none_metric_propagates_as_none_inflation(self):
        def fn(x):
            return None
        out = lp.one_switch_ablation(fn, {"x": 1}, {"x": 2}, lambda r: r)
        assert out["inflation"] is None


class TestEntryLagLeakage:
    @pytest.fixture
    def jump_prices(self, monkeypatch):
        idx = pd.bdate_range("2020-01-01", periods=300)
        # small daily noise throughout -- realistic, and gives the SAFE
        # (post-jump) trades nonzero return variance too, so this isn't
        # comparing a real number against a degenerate-guard None
        rng = np.random.default_rng(11)
        close = 100 * np.cumprod(1 + rng.normal(0.0, 0.001, 300))
        event_locs = [50, 100, 150, 200, 250]
        # varying jump sizes (not identical) so trade returns have nonzero
        # variance across events -- an identical jump every time gives every
        # trade the same return_pct and a degenerate (zero) std either way
        jumps = [1.14, 1.18, 1.20, 1.23, 1.27]
        for loc, jump in zip(event_locs, jumps):
            close[loc:] *= jump      # a permanent step, no reversion
        px = pd.Series(close, index=idx, name="TEST")

        def fake_load_close(symbol, start=None, end=None, price_table=None):
            return px.copy() if symbol == "TEST" else pd.Series(dtype=float)

        def fake_matrix(symbols, start=None, end=None, price_table=None):
            return pd.DataFrame({"TEST": px}) if "TEST" in symbols else pd.DataFrame()

        monkeypatch.setattr(eb, "load_close", fake_load_close)
        monkeypatch.setattr(eb, "load_close_matrix", fake_matrix)
        return px, event_locs

    def test_same_bar_execution_inflates_return(self, jump_prices):
        px, event_locs = jump_prices
        ev = pd.DataFrame({"date": px.index[event_locs]})

        leaky_sc = eb.scenario(ev, symbols="TEST", holding_days=5, entry_lag=0)
        safe_sc = eb.scenario(ev, symbols="TEST", holding_days=5, entry_lag=1)
        # ground truth the probe's claim is checked against: same-bar entry
        # bakes in the +20% jump, next-bar entry enters after it happened
        assert leaky_sc.metrics["avg_return_pct"] > 15.0
        assert abs(safe_sc.metrics["avg_return_pct"]) < 1.0

        out = lp.entry_lag_leakage(ev, symbols="TEST", holding_days=5)
        assert out["switch"] == "entry_lag"
        assert out["safe_value"] == 1 and out["leaky_value"] == 0
        assert out["leaky_metric"] > out["safe_metric"]
        assert out["inflation"] > 0

    def test_flat_series_shows_no_inflation(self, monkeypatch):
        idx = pd.bdate_range("2020-01-01", periods=200)
        px = pd.Series(100.0, index=idx, name="TEST")

        monkeypatch.setattr(eb, "load_close",
                            lambda symbol, start=None, end=None, price_table=None:
                            px.copy() if symbol == "TEST" else pd.Series(dtype=float))
        monkeypatch.setattr(eb, "load_close_matrix",
                            lambda symbols, start=None, end=None, price_table=None:
                            pd.DataFrame({"TEST": px}) if "TEST" in symbols else pd.DataFrame())

        ev = pd.DataFrame({"date": px.index[[50, 100, 150]]})
        out = lp.entry_lag_leakage(ev, symbols="TEST", holding_days=5)
        # every trade's return_pct is exactly 0 on a flat series -> zero
        # variance -> the degenerate-sd guard returns None on both sides,
        # so inflation is None, not a spurious nonzero number
        assert out["inflation"] is None


class TestFeatureCenteringLeakage:
    def test_centered_window_inflates_apparent_lift(self):
        # A centered feature window can see 2 bars past entry_signal_date;
        # bake each trade's own outcome into the price exactly there, so a
        # centered feature is (almost) a direct readout of the label while
        # a trailing feature sees nothing informative. The probe should
        # show the centered (leaky) run producing more apparent win-rate
        # lift than the trailing (safe) run.
        rng = np.random.default_rng(3)
        n_trades = 120
        spacing = 8
        total_bars = n_trades * spacing + 20
        idx = pd.bdate_range("2020-01-01", periods=total_bars)
        close = np.full(total_bars, 100.0)
        labels = rng.integers(0, 2, n_trades)
        entry_locs = [10 + i * spacing for i in range(n_trades)]
        for loc, label in zip(entry_locs, labels):
            close[loc + 2] = close[loc] * (1.10 if label else 0.90)
        cache = {"TEST": pd.DataFrame({"close": close}, index=idx)}

        entry_signal_dates = idx[entry_locs]
        pnl_pct = np.where(labels == 1, 5.0, -5.0)
        trades = pd.DataFrame({
            "symbol": ["TEST"] * n_trades,
            "entry_signal_date": entry_signal_dates,
            "pnl_pct": pnl_pct,
            "pnl_dollars": pnl_pct * 100,
            "side": ["long"] * n_trades,
            "days_held": [5] * n_trades,
        })

        out = lp.feature_centering_leakage(trades, cache, windows=(4,),
                                           min_train=20, refit_every=5)
        assert out["switch"] == "centered"
        assert out["safe_value"] is False and out["leaky_value"] is True
        assert out["safe_metric"] is not None and out["leaky_metric"] is not None
        assert out["leaky_metric"] > out["safe_metric"]
        assert out["inflation"] > 0

    def test_no_scored_trades_gives_none_metric(self):
        # min_train larger than the whole trade count -> no OOS-scored
        # trades on either side -> both metrics (and inflation) are None,
        # not a spurious 0.
        idx = pd.bdate_range("2020-01-01", periods=100)
        cache = {"TEST": pd.DataFrame({"close": [100.0] * 100}, index=idx)}
        trades = pd.DataFrame({
            "symbol": ["TEST"] * 5,
            "entry_signal_date": idx[[10, 20, 30, 40, 50]],
            "pnl_pct": [1.0, -1.0, 1.0, -1.0, 1.0],
            "pnl_dollars": [100.0, -100.0, 100.0, -100.0, 100.0],
            "side": ["long"] * 5,
            "days_held": [5] * 5,
        })
        out = lp.feature_centering_leakage(trades, cache, windows=(4,),
                                           min_train=50)
        assert out["inflation"] is None


class TestFundamentalsGrowthSignal:
    def test_bad_date_col_raises(self):
        with pytest.raises(ValueError, match="date_col"):
            lp._fundamentals_growth_signal(["AAPL"], "net_income", "made_up")

    def test_empty_source_gives_empty_frame(self, monkeypatch):
        import query as q
        monkeypatch.setattr(q, "load", lambda table: pd.DataFrame(
            columns=["symbol", "metric", "form", "period_end", "filed", "value"]))
        out = lp._fundamentals_growth_signal(["AAPL"], "net_income", "filed")
        assert out.empty
        assert list(out.columns) == ["symbol", "date", "value"]

    def test_yoy_growth_known_answer(self, monkeypatch):
        import query as q
        df = pd.DataFrame([
            {"symbol": "AAA", "metric": "net_income", "form": "10-K",
            "period_end": pd.Timestamp("2019-12-31"),
            "filed": pd.Timestamp("2020-02-01"), "value": 100.0},
            {"symbol": "AAA", "metric": "net_income", "form": "10-K",
            "period_end": pd.Timestamp("2020-12-31"),
            "filed": pd.Timestamp("2021-02-01"), "value": 150.0},
        ])
        monkeypatch.setattr(q, "load", lambda table: df)
        out = lp._fundamentals_growth_signal(["AAA"], "net_income", "filed")
        assert len(out) == 1                     # first year has no prior -> dropped
        assert out.iloc[0]["value"] == pytest.approx(0.5)   # 150/100 - 1
        assert out.iloc[0]["date"] == pd.Timestamp("2021-02-01")


class TestPitLagFundamentalsLeakage:
    @pytest.fixture
    def fundamentals_and_prices(self, monkeypatch):
        """
        10 symbols, each with a year-2 net_income YoY growth of +50% (even
        index, "good") or -50% (odd index, "bad"), and a price series that
        jumps in the SAME direction exactly at the real filing date (the
        market's real reaction to the eventual disclosure) -- never at
        period_end, which lands `gap` trading days earlier. Only a signal
        dated by period_end can have that jump land inside its forward
        return window; a signal dated by filed enters after the jump has
        already happened and captures none of it.
        """
        symbols = [f"SYM{i}" for i in range(10)]
        idx = pd.bdate_range("2020-01-01", periods=300)
        p_loc, gap, horizon = 100, 40, 45
        f_loc = p_loc + gap
        period_end_y2, filed_y2 = idx[p_loc], idx[f_loc]
        period_end_y1 = period_end_y2 - pd.tseries.offsets.BDay(252)
        filed_y1 = period_end_y1 + pd.tseries.offsets.BDay(30)

        rng = np.random.default_rng(4)
        rows, price_frames = [], {}
        for i, sym in enumerate(symbols):
            good = (i % 2 == 0)
            close = 100.0 + np.cumsum(rng.normal(0, 0.05, 300))
            close[f_loc:] *= 1.15 if good else 0.85
            price_frames[sym] = pd.Series(close, index=idx, name=sym)

            prior_val = 100.0
            new_val = prior_val * (1.5 if good else 0.5)
            rows.append({"symbol": sym, "metric": "net_income", "form": "10-K",
                        "period_end": period_end_y1, "filed": filed_y1,
                        "value": prior_val})
            rows.append({"symbol": sym, "metric": "net_income", "form": "10-K",
                        "period_end": period_end_y2, "filed": filed_y2,
                        "value": new_val})
        fundamentals = pd.DataFrame(rows)

        import query as q
        monkeypatch.setattr(q, "load", lambda table: fundamentals.copy())

        import event_backtest as eb

        def fake_matrix(syms, start=None, end=None, price_table=None):
            return pd.DataFrame({s: price_frames[s] for s in syms
                                 if s in price_frames})

        monkeypatch.setattr(eb, "load_close_matrix", fake_matrix)
        return symbols, horizon

    def test_period_end_inflates_ic_vs_filed(self, fundamentals_and_prices):
        symbols, horizon = fundamentals_and_prices
        out = lp.pit_lag_fundamentals_leakage(symbols, horizon=horizon,
                                              benchmark=None)
        assert out["switch"] == "date_col"
        assert out["safe_value"] == "filed" and out["leaky_value"] == "period_end"
        assert out["safe_metric"] is not None and out["leaky_metric"] is not None
        assert out["leaky_metric"] > out["safe_metric"]
        assert out["inflation"] > 0

    def test_bad_metric_gives_none_metric_not_error(self, fundamentals_and_prices):
        symbols, horizon = fundamentals_and_prices
        out = lp.pit_lag_fundamentals_leakage(symbols, metric="no_such_metric",
                                              horizon=horizon, benchmark=None)
        assert out["safe_metric"] is None and out["leaky_metric"] is None
        assert out["inflation"] is None
