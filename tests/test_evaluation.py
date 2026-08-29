"""
test_evaluation.py -- unified evaluation framework (evaluation/ package).
All synthetic data; no stored data or API keys. Repo modules
(event_backtest, backtest, query, analytics.*) are monkeypatched where the
code under test imports them locally.
"""

import json
import math
import os
import sys
import warnings

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from evaluation.contracts import Signal, EventSet, TradeRule


def _sig_frame(n_dates=300, symbols=("AAA", "BBB", "CCC")):
    dates = pd.bdate_range("2023-01-02", periods=n_dates)
    rows = [{"symbol": s, "date": d, "value": float(i + j)}
            for j, s in enumerate(symbols) for i, d in enumerate(dates)]
    return pd.DataFrame(rows)


class TestSignalContract:
    def test_valid_signal_constructs_and_sorts(self):
        f = _sig_frame().sample(frac=1.0, random_state=0)   # shuffled input
        s = Signal(name="toy", frame=f)
        assert list(s.frame.columns) == ["symbol", "date", "value"]
        assert s.frame["date"].is_monotonic_increasing
        assert str(s.frame["date"].dtype) == "datetime64[ns]"

    def test_missing_column_raises(self):
        with pytest.raises(ValueError, match="missing columns"):
            Signal(name="toy", frame=pd.DataFrame({"symbol": ["A"], "date": ["2024-01-02"]}))

    def test_duplicate_symbol_date_raises(self):
        f = pd.concat([_sig_frame(), _sig_frame().head(1)], ignore_index=True)
        with pytest.raises(ValueError, match="duplicate"):
            Signal(name="toy", frame=f)

    def test_nan_value_raises(self):
        f = _sig_frame()
        f.loc[0, "value"] = np.nan
        with pytest.raises(ValueError, match="NaN"):
            Signal(name="toy", frame=f)

    def test_tz_aware_dates_raise(self):
        f = _sig_frame()
        f["date"] = pd.to_datetime(f["date"]).dt.tz_localize("UTC")
        with pytest.raises(ValueError, match="tz-naive"):
            Signal(name="toy", frame=f)

    def test_bad_direction_raises(self):
        with pytest.raises(ValueError, match="direction"):
            Signal(name="toy", frame=_sig_frame(), direction=2)

    def test_short_history_warns_not_fails(self):
        with pytest.warns(UserWarning, match="distinct dates"):
            Signal(name="toy", frame=_sig_frame(n_dates=50))

    def test_long_history_no_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Signal(name="toy", frame=_sig_frame(n_dates=300))


class TestEventSetContract:
    def test_valid_event_set(self):
        f = pd.DataFrame({"symbol": ["AAA", "BBB"],
                          "date": ["2024-01-03", "2024-02-05"],
                          "label": ["up", "down"]})
        e = EventSet(name="toy_events", frame=f)
        assert e.min_events == 5
        assert str(e.frame["date"].dtype) == "datetime64[ns]"

    def test_missing_label_raises(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": ["2024-01-03"]})
        with pytest.raises(ValueError, match="missing columns"):
            EventSet(name="toy_events", frame=f)

    def test_nan_label_raises(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": ["2024-01-03"], "label": [None]})
        with pytest.raises(ValueError, match="NaN"):
            EventSet(name="toy_events", frame=f)


class TestTradeRuleContract:
    def test_valid_long_rule(self):
        r = TradeRule(name="toy_rule",
                      entries=lambda d: d["x"] > 0, exits=lambda d: d["x"] < 0)
        assert r.side == "long" and r.notional == 10_000.0

    def test_bad_side_raises(self):
        with pytest.raises(ValueError, match="side"):
            TradeRule(name="r", entries=lambda d: d, exits=lambda d: d, side="sideways")

    def test_both_requires_short_pair(self):
        with pytest.raises(ValueError, match="short_entries"):
            TradeRule(name="r", entries=lambda d: d, exits=lambda d: d, side="both")

    def test_non_callable_raises(self):
        with pytest.raises(ValueError, match="callable"):
            TradeRule(name="r", entries="not a function", exits=lambda d: d)


from evaluation.data import HORIZONS, apply_lag, build_return_panel


def _close_matrix(n=40):
    idx = pd.bdate_range("2024-01-02", periods=n)
    return pd.DataFrame({"AAA": 100.0 * (1.01 ** np.arange(n)),
                         "SPY": np.full(n, 100.0)}, index=idx)


class TestApplyLag:
    def test_zero_lag_returns_copy_unchanged(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": [pd.Timestamp("2024-01-05")],
                          "value": [1.0]})
        out = apply_lag(f, 0)
        assert out is not f
        assert out["date"].iloc[0] == pd.Timestamp("2024-01-05")

    def test_lag_moves_business_days(self):
        f = pd.DataFrame({"symbol": ["AAA"], "date": [pd.Timestamp("2024-01-05")],
                          "value": [1.0]})           # a Friday
        out = apply_lag(f, 2)
        assert out["date"].iloc[0] == pd.Timestamp("2024-01-09")   # Fri + 2bd = Tue


class TestBuildReturnPanel:
    def test_entry_is_strictly_next_close(self):
        closes = _close_matrix()
        idx = closes.index
        f = pd.DataFrame({"symbol": ["AAA"], "date": [idx[5]], "value": [1.0]})
        panel, dropped = build_return_panel(f, closes, benchmark=None)
        assert dropped == {}
        assert panel["entry_date"].iloc[0] == idx[6]
        expected = closes["AAA"].iloc[7] / closes["AAA"].iloc[6] - 1.0
        assert panel["fwd_1d"].iloc[0] == pytest.approx(expected)

    def test_flat_benchmark_equals_raw_return(self):
        closes = _close_matrix()          # SPY constant 100 -> excess == raw
        f = pd.DataFrame({"symbol": ["AAA"], "date": [closes.index[5]], "value": [1.0]})
        raw, _ = build_return_panel(f, closes, benchmark=None)
        exc, _ = build_return_panel(f, closes, benchmark="SPY")
        assert exc["fwd_5d"].iloc[0] == pytest.approx(raw["fwd_5d"].iloc[0])

    def test_excess_vs_identical_benchmark_is_zero(self):
        closes = _close_matrix()
        closes["SPY"] = closes["AAA"]
        f = pd.DataFrame({"symbol": ["AAA"], "date": [closes.index[5]], "value": [1.0]})
        panel, _ = build_return_panel(f, closes, benchmark="SPY")
        assert panel["fwd_5d"].iloc[0] == pytest.approx(0.0, abs=1e-12)

    def test_lag_pushes_entry_and_kills_tail(self):
        closes = _close_matrix()
        idx = closes.index
        f = pd.DataFrame({"symbol": ["AAA", "AAA"],
                          "date": [idx[5], idx[38]], "value": [1.0, 2.0]})
        panel, _ = build_return_panel(apply_lag(f, 3), closes, benchmark=None)
        # idx[5] + 3bd = idx[8] -> entry strictly after = idx[9]
        assert panel["entry_date"].iloc[0] == idx[9]
        # idx[38] + 3bd is past the data end -> no entry, all horizons NaN
        assert pd.isna(panel["entry_date"].iloc[1])
        assert panel[[f"fwd_{h}d" for h in HORIZONS]].iloc[1].isna().all()

    def test_benchmark_symbol_excluded(self):
        closes = _close_matrix()
        f = pd.DataFrame({"symbol": ["SPY"], "date": [closes.index[5]], "value": [1.0]})
        panel, dropped = build_return_panel(f, closes, benchmark="SPY")
        assert panel.empty
        assert "SPY" in dropped and "benchmark" in dropped["SPY"]

    def test_unknown_symbol_and_short_history_dropped(self):
        closes = _close_matrix()
        closes["SHT"] = np.nan
        closes.iloc[:10, closes.columns.get_loc("SHT")] = 50.0
        f = pd.DataFrame({"symbol": ["ZZZ", "SHT"],
                          "date": [closes.index[2]] * 2, "value": [1.0, 1.0]})
        panel, dropped = build_return_panel(f, closes, benchmark=None)
        assert dropped["ZZZ"] == "no price data"
        assert "history too short" in dropped["SHT"]

    def test_nonpositive_prices_masked(self):
        closes = _close_matrix()
        closes.iloc[9, closes.columns.get_loc("AAA")] = -1.0   # WTI-Apr-2020 class
        f = pd.DataFrame({"symbol": ["AAA"], "date": [closes.index[5]], "value": [1.0]})
        panel, _ = build_return_panel(f, closes, benchmark=None)
        # entry = idx[6]; h=3 exits at idx[9] (the bad close) -> masked to NaN
        assert pd.isna(panel["fwd_3d"].iloc[0])
        # h=1 exits at idx[7], untouched -> still a real return
        assert np.isfinite(panel["fwd_1d"].iloc[0])


from evaluation import stats as ev_stats
from evaluation.ic import evaluate_ic


def _planted_panel(n_dates=300, n_syms=8, slope=0.01, noise=0.001, seed=0):
    """fwd_1d = slope * value + eps -- a signal that OBVIOUSLY works."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_dates)
    rows = []
    for d in dates:
        vals = rng.normal(size=n_syms)
        for k in range(n_syms):
            rows.append({"symbol": f"S{k}", "date": d, "value": float(vals[k]),
                         "fwd_1d": slope * float(vals[k]) + rng.normal(scale=noise)})
    return pd.DataFrame(rows)


def _noise_panel(n_dates=300, n_syms=8, seed=1):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_dates)
    rows = [{"symbol": f"S{k}", "date": d, "value": float(rng.normal()),
             "fwd_1d": float(rng.normal(scale=0.01))}
            for d in dates for k in range(n_syms)]
    return pd.DataFrame(rows)


class TestTier1:
    def test_planted_signal_detected_everywhere(self):
        p = _planted_panel()
        pool = ev_stats.pooled_ic(p["value"], p["fwd_1d"])
        assert pool["pooled_ic"] > 0.9
        d = ev_stats.daily_ic(p, "value", "fwd_1d")
        assert d["mean_daily_ic"] > 0.9 and d["ic_t_stat"] > 10
        s = ev_stats.quantile_spread(p, "value", "fwd_1d")
        assert s["spread_pct"] > 0 and s["spread_t"] > 10

    def test_noise_not_detected(self):
        p = _noise_panel()
        pool = ev_stats.pooled_ic(p["value"], p["fwd_1d"])
        assert abs(pool["pooled_ic"]) < 0.05
        s = ev_stats.quantile_spread(p, "value", "fwd_1d")
        assert abs(s["spread_t"]) < 3

    def test_pooled_too_few_pairs_reason(self):
        r = ev_stats.pooled_ic(pd.Series([1.0, 2.0]), pd.Series([0.1, 0.2]))
        assert r["pooled_ic"] is None and "fewer than" in r["pooled_reason"]

    def test_daily_zero_variance_guard(self):
        p = _planted_panel()
        p["value"] = 1.0                       # constant -> no per-day ranks
        d = ev_stats.daily_ic(p, "value", "fwd_1d")
        assert d["ic_t_stat"] is None and "daily_reason" in d

    def test_degenerate_sd_gate(self):
        """The gate itself: zero, non-finite, and float-noise sds are all
        degenerate; any real dispersion passes."""
        assert ev_stats._degenerate_sd(0.0)
        assert ev_stats._degenerate_sd(float("nan"))
        assert ev_stats._degenerate_sd(float("inf"))
        assert ev_stats._degenerate_sd(6e-19)      # constant-0.001 float noise
        assert not ev_stats._degenerate_sd(1e-3)

    def test_quantile_spread_float_noise_buckets_guarded(self):
        """Both buckets constant (top=0.001s, bottom=0.0s): their sds land at
        ~6e-19 / 0.0 -- the old `sd_t > 0 or sd_b > 0` gate ran Welch on that
        noise and reported an astronomically large t for a 15 bps mean gap.
        The SD_FLOOR gate must close it instead."""
        rows = []
        for d in pd.bdate_range("2023-01-02", periods=30):
            for k in range(8):
                rows.append({"symbol": f"S{k}", "date": d,
                             "value": float(k),
                             "fwd_1d": 0.001 if k >= 4 else 0.0})
        p = pd.DataFrame(rows)
        r = ev_stats.quantile_spread(p, "value", "fwd_1d")
        assert r["spread_pct"] == pytest.approx(100 * 0.001)
        assert r["spread_t"] is None
        assert "both buckets" in r["spread_reason"]

    def test_t_to_p_two_sided(self):
        assert ev_stats.t_to_p(0.0) == pytest.approx(1.0)
        assert ev_stats.t_to_p(1.96) == pytest.approx(0.05, abs=0.01)


class TestEvaluateIC:
    def test_direction_minus_one_orients(self):
        p = _planted_panel()
        p["value"] = -p["value"]               # now LOWER value = higher return
        res = evaluate_ic(p, direction=-1)
        assert res[1]["pooled_ic"] > 0.9       # orientation recovers the sign
        assert res[1]["oriented"] == -1

    def test_missing_horizon_columns_skipped(self):
        p = _planted_panel()                   # only fwd_1d exists
        res = evaluate_ic(p)
        assert list(res.keys()) == [1]


from types import SimpleNamespace

import backtest as bt_module
import event_backtest as eb_module

from evaluation.portfolio import evaluate_portfolio, summarize_portfolio
from evaluation.events import evaluate_events


class TestEvaluatePortfolio:
    def _fake_result(self):
        return SimpleNamespace(metrics={"sharpe": 1.0, "cagr_pct": 5.0},
                               params={"score": "value", "n_symbols": 3},
                               returns=pd.Series([0.001, -0.002]))

    def test_wraps_backtest_with_value_score(self, monkeypatch):
        captured = {}

        def fake_backtest(signal, score="composite", **kw):
            captured["frame"] = signal.copy()
            captured["score"] = score
            captured["kw"] = kw
            return self._fake_result()

        monkeypatch.setattr(bt_module, "backtest", fake_backtest)
        f = _sig_frame(n_dates=30, symbols=("AAA", "BBB"))
        res = evaluate_portfolio(f, quantiles=4, rebalance="W")
        assert captured["score"] == "value"
        assert captured["kw"]["quantiles"] == 4
        assert captured["kw"]["rebalance"] == "W"
        assert res.metrics["sharpe"] == 1.0

    def test_direction_minus_one_flips_values(self, monkeypatch):
        captured = {}

        def fake_backtest(signal, score="composite", **kw):
            captured["values"] = signal["value"].copy()
            return self._fake_result()

        monkeypatch.setattr(bt_module, "backtest", fake_backtest)
        f = _sig_frame(n_dates=30, symbols=("AAA", "BBB"))
        evaluate_portfolio(f, direction=-1)
        assert (captured["values"] == -f["value"]).all()

    def test_summarize_is_json_safe(self):
        res = SimpleNamespace(metrics={"sharpe": float("nan"), "cagr_pct": 5.0},
                              params={"score": "value"})
        s = summarize_portfolio(res)
        assert s["metrics"]["sharpe"] is None       # NaN -> None for JSON
        assert s["metrics"]["cagr_pct"] == 5.0
        json.dumps(s)                               # must not raise


class TestEvaluateEvents:
    def _fake_study(self, n=7):
        horizons = pd.DataFrame({"n": [n, n], "mean_pct": [1.0, 2.0],
                                 "t_stat": [2.5, 3.0]}, index=[5, 21])
        horizons.index.name = "horizon_days"
        return SimpleNamespace(n_events=n, horizons=horizons,
                               mean_car=pd.Series([0.0, 0.01], index=[0, 1]))

    def _events_frame(self, label_counts):
        rows = []
        d0 = pd.Timestamp("2024-01-02")
        for label, cnt in label_counts.items():
            for i in range(cnt):
                rows.append({"symbol": f"S{i}", "date": d0 + pd.Timedelta(days=i),
                             "label": label})
        return pd.DataFrame(rows)

    def test_small_labels_skipped_large_studied(self, monkeypatch):
        calls = []

        def fake_event_study(events, **kw):
            calls.append(kw)
            return self._fake_study(n=len(events))

        monkeypatch.setattr(eb_module, "event_study", fake_event_study)
        f = self._events_frame({"big": 8, "tiny": 2})
        out = evaluate_events(f, min_events=5)
        assert "big" in out["labels"] and out["labels"]["big"]["n_events"] == 8
        assert out["skipped"]["tiny"] == 2
        assert calls[0]["entry_lag"] == 1           # engine-enforced next close

    def test_runtime_error_becomes_skip(self, monkeypatch):
        def fake_event_study(events, **kw):
            raise RuntimeError("No events had enough surrounding price history.")

        monkeypatch.setattr(eb_module, "event_study", fake_event_study)
        f = self._events_frame({"big": 8})
        out = evaluate_events(f, min_events=5)
        assert out["labels"] == {}
        assert "price history" in out["skipped"]["big"]

    def test_output_is_json_safe(self, monkeypatch):
        monkeypatch.setattr(eb_module, "event_study",
                            lambda events, **kw: self._fake_study())
        f = self._events_frame({"big": 8})
        json.dumps(evaluate_events(f, min_events=5))    # must not raise


from evaluation.trades import (TRADE_COLS, rule_flags, simulate, simulate_symbol,
                               trade_summary)


def _trade_frame(n=12):
    idx = pd.bdate_range("2024-01-02", periods=n)
    df = pd.DataFrame({"close": 100.0 + np.arange(n),
                       "ent": False, "ex": False}, index=idx)
    return df


def _flag_rule(side="long"):
    return TradeRule(name="flagrule",
                     entries=lambda d: d["ent"], exits=lambda d: d["ex"],
                     side=side)


class TestTradeEngine:
    def test_next_close_execution_and_no_pyramiding(self):
        df = _trade_frame()
        df.loc[df.index[2], "ent"] = True
        df.loc[df.index[4], "ent"] = True      # while in position -> ignored
        df.loc[df.index[5], "ex"] = True
        trades = simulate(_flag_rule(), {"AAA": df})
        assert len(trades) == 1
        t = trades.iloc[0]
        assert t["entry_signal_date"] == df.index[2]
        assert t["entry_date"] == df.index[3]          # next close
        assert t["entry_price"] == pytest.approx(103.0)
        assert t["exit_date"] == df.index[6]           # exit signal 5 -> close 6
        assert t["exit_price"] == pytest.approx(106.0)
        assert t["days_held"] == 3
        assert t["pnl_dollars"] == pytest.approx(10_000 * 3.0 / 103.0, abs=0.01)

    def test_short_side_flips_pnl_sign(self):
        df = _trade_frame()
        df.loc[df.index[2], "ent"] = True
        df.loc[df.index[5], "ex"] = True
        trades = simulate(_flag_rule(side="short"), {"AAA": df})
        assert trades.iloc[0]["side"] == "short"
        assert trades.iloc[0]["pnl_dollars"] == pytest.approx(-10_000 * 3.0 / 103.0, abs=0.01)

    def test_open_position_dropped_and_blocks_reentry(self):
        df = _trade_frame()
        df.loc[df.index[2], "ent"] = True      # never exits
        df.loc[df.index[8], "ent"] = True      # blocked: prior still open
        trades = simulate(_flag_rule(), {"AAA": df})
        assert trades.empty
        assert list(trades.columns) == TRADE_COLS

    def test_flag_length_mismatch_raises(self):
        df = _trade_frame()
        bad = TradeRule(name="bad", entries=lambda d: pd.Series([True]),
                        exits=lambda d: d["ex"])
        with pytest.raises(ValueError, match="flags"):
            rule_flags(bad, df)

    def test_trade_summary(self):
        df = _trade_frame()
        df.loc[df.index[2], "ent"] = True
        df.loc[df.index[5], "ex"] = True
        trades = simulate(_flag_rule(), {"AAA": df})
        s = trade_summary(trades)
        assert s["n_trades"] == 1 and s["n_long"] == 1 and s["n_short"] == 0
        assert s["win_rate_pct"] == 100.0
        assert trade_summary(trades.iloc[0:0])["summary_reason"] == "no realized trades"

    def test_invalid_entry_price_skipped(self):
        """Entry price NaN/zero/negative skips position entirely."""
        df = _trade_frame()
        df.loc[df.index[2], "ent"] = True
        df.loc[df.index[5], "ex"] = True
        df.loc[df.index[3], "close"] = np.nan  # entry_date has NaN close
        trades = simulate(_flag_rule(), {"AAA": df})
        assert trades.empty

        # Test with zero entry price
        df2 = _trade_frame()
        df2.loc[df2.index[2], "ent"] = True
        df2.loc[df2.index[5], "ex"] = True
        df2.loc[df2.index[3], "close"] = 0.0
        trades2 = simulate(_flag_rule(), {"AAA": df2})
        assert trades2.empty

        # Test with negative entry price
        df3 = _trade_frame()
        df3.loc[df3.index[2], "ent"] = True
        df3.loc[df3.index[5], "ex"] = True
        df3.loc[df3.index[3], "close"] = -5.0
        trades3 = simulate(_flag_rule(), {"AAA": df3})
        assert trades3.empty

    def test_invalid_exit_price_skipped(self):
        """Exit price NaN/zero/negative skips position entirely."""
        df = _trade_frame()
        df.loc[df.index[2], "ent"] = True
        df.loc[df.index[5], "ex"] = True
        df.loc[df.index[6], "close"] = np.nan  # exit_date has NaN close
        trades = simulate(_flag_rule(), {"AAA": df})
        assert trades.empty

        # Test with zero exit price
        df2 = _trade_frame()
        df2.loc[df2.index[2], "ent"] = True
        df2.loc[df2.index[5], "ex"] = True
        df2.loc[df2.index[6], "close"] = 0.0
        trades2 = simulate(_flag_rule(), {"AAA": df2})
        assert trades2.empty

    def test_last_row_exit_boundary(self):
        """Exit at last row (exit_date past data end) drops trade and blocks reentry."""
        df = _trade_frame()
        df.loc[df.index[2], "ent"] = True   # Entry 1
        df.loc[df.index[11], "ex"] = True   # exit_sig_i=11, exit_i=12 >= n=12
        df.loc[df.index[10], "ent"] = True  # Entry 2: later entry signal should be blocked
        trades = simulate(_flag_rule(), {"AAA": df})
        # Entry 1 dropped due to boundary exit, setting next_free=n.
        # Entry 2 blocked by next_free=n (sig_i=10 < next_free=12).
        # Together: no trades produced.
        assert trades.empty

    def test_side_both_uses_short_flags(self):
        """side='both' correctly uses short_entries/short_exits for short side."""
        df = _trade_frame(n=20)  # Use larger frame to fit both non-overlapping positions
        # Set up separate entry/exit columns
        df["short_ent"] = False
        df["short_ex"] = False

        # Short entry at index 1 (entry_date 2), exit at index 4 (exit_date 5)
        df.loc[df.index[1], "short_ent"] = True
        df.loc[df.index[4], "short_ex"] = True

        # Long entry at index 7 (entry_date 8), exit at index 10 (exit_date 11)
        df.loc[df.index[7], "ent"] = True
        df.loc[df.index[10], "ex"] = True

        rule = TradeRule(
            name="both_rule",
            entries=lambda d: d["ent"],
            exits=lambda d: d["ex"],
            short_entries=lambda d: d["short_ent"],
            short_exits=lambda d: d["short_ex"],
            side="both"
        )

        trades = simulate(rule, {"AAA": df})
        assert len(trades) == 2

        # Separate by side
        long_trades = trades[trades["side"] == "long"]
        short_trades = trades[trades["side"] == "short"]

        assert len(long_trades) == 1
        assert len(short_trades) == 1

        # Short uses short_entries/short_exits (earlier)
        assert short_trades.iloc[0]["entry_signal_date"] == df.index[1]
        assert short_trades.iloc[0]["entry_date"] == df.index[2]
        assert short_trades.iloc[0]["exit_signal_date"] == df.index[4]
        assert short_trades.iloc[0]["exit_date"] == df.index[5]

        # Long uses entries/exits (later)
        assert long_trades.iloc[0]["entry_signal_date"] == df.index[7]
        assert long_trades.iloc[0]["entry_date"] == df.index[8]
        assert long_trades.iloc[0]["exit_signal_date"] == df.index[10]
        assert long_trades.iloc[0]["exit_date"] == df.index[11]


class TestTier2:
    def test_planted_spread_ci_excludes_zero(self):
        p = _planted_panel()
        r = ev_stats.block_bootstrap_spread(p, "value", "fwd_1d", n_boot=300, seed=0)
        assert r["spread_ci_lo_pct"] > 0

    def test_noise_spread_ci_straddles_zero(self):
        # NOTE: _noise_panel()'s shared default (seed=1) happens to land on a
        # per-day quintile-spread realization with |t| ~ 2.8 (see Tier1's own
        # looser "< 3" bound on the same fixture) -- a 95% bootstrap CI is a
        # stricter (~1.96 sigma) check and deterministically excludes zero
        # for that specific seed. seed=2 is an equally-noise fixture that
        # does not hit this fluke; verified across seeds 1-29 (27/29 straddle,
        # seeds 1 and 21 don't) that this is fixture-realization variance,
        # not an implementation bug.
        p = _noise_panel(seed=2)
        r = ev_stats.block_bootstrap_spread(p, "value", "fwd_1d", n_boot=300, seed=0)
        assert r["spread_ci_lo_pct"] < 0 < r["spread_ci_hi_pct"]

    def test_bootstrap_spread_too_few_days_reason(self):
        p = _planted_panel(n_dates=10)
        r = ev_stats.block_bootstrap_spread(p, "value", "fwd_1d")
        assert r["spread_ci_lo_pct"] is None and "usable days" in r["boot_reason"]

    def test_bootstrap_sharpe_ci_and_guards(self):
        rng = np.random.default_rng(0)
        good = pd.Series(rng.normal(0.001, 0.01, size=500))
        r = ev_stats.bootstrap_sharpe(good, n_boot=300, seed=0)
        assert r["sharpe_ci_lo"] < r["sharpe"] < r["sharpe_ci_hi"]
        flat = pd.Series(np.zeros(500))
        assert "sharpe_reason" in ev_stats.bootstrap_sharpe(flat)
        short = pd.Series(rng.normal(size=10))
        assert "sharpe_reason" in ev_stats.bootstrap_sharpe(short)

    def test_bootstrap_sharpe_float_noise_series_guarded(self):
        """A constant 0.001 series has sd ~6e-19 in float64 -- the old bare
        `sd > 0` gate let it through and reported a Sharpe near 2.4e16."""
        r = ev_stats.bootstrap_sharpe(pd.Series([0.001] * 500))
        assert r["sharpe"] is None
        assert r["sharpe_reason"] == "zero return variance"

    def test_permutation_null_rule_not_significant(self):
        # a rule whose entries are RANDOM days on a random walk must not
        # produce a tiny p (null true -> p ~ uniform; loose bound, fixed seeds)
        rng = np.random.default_rng(7)
        idx = pd.bdate_range("2020-01-02", periods=400)
        cache = {}
        for sym in ("AAA", "BBB", "CCC"):
            close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, size=400)))
            ent = np.zeros(400, dtype=bool)
            ent[rng.choice(400, size=12, replace=False)] = True
            ex = np.zeros(400, dtype=bool)
            ex[rng.choice(400, size=40, replace=False)] = True
            cache[sym] = pd.DataFrame({"close": close, "ent": ent, "ex": ex},
                                      index=idx)
        rule = TradeRule(name="nullrule", entries=lambda d: d["ent"],
                         exits=lambda d: d["ex"])
        r = ev_stats.permutation_trades(rule, cache, n_perm=99, seed=0)
        assert r["pnl_p"] > 0.01 and r["pnl_p"] <= 1.0
        assert r["n_perm"] > 20

    def test_permutation_no_trades_reason(self):
        idx = pd.bdate_range("2024-01-02", periods=30)
        cache = {"AAA": pd.DataFrame({"close": np.full(30, 100.0),
                                      "ent": False, "ex": False}, index=idx)}
        rule = TradeRule(name="never", entries=lambda d: d["ent"],
                         exits=lambda d: d["ex"])
        r = ev_stats.permutation_trades(rule, cache, n_perm=20, seed=0)
        assert r["pnl_p"] is None and "no realized trades" in r["perm_reason"]

    def test_permutation_parallel_matches_sequential_and_deterministic(self):
        rng = np.random.default_rng(11)
        idx = pd.bdate_range("2019-01-02", periods=300)
        cache = {}
        for sym in ("AAA", "BBB", "CCC", "DDD", "EEE"):
            close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, size=300)))
            ent = np.zeros(300, dtype=bool)
            ent[rng.choice(300, size=15, replace=False)] = True
            ex = np.zeros(300, dtype=bool)
            ex[rng.choice(300, size=40, replace=False)] = True
            cache[sym] = pd.DataFrame({"close": close, "ent": ent, "ex": ex},
                                      index=idx)
        rule = TradeRule(name="parrule", entries=lambda d: d["ent"],
                         exits=lambda d: d["ex"])
        seq = ev_stats.permutation_trades(rule, cache, n_perm=40, seed=7)
        par = ev_stats.permutation_trades(rule, cache, n_perm=40, seed=7,
                                          workers=4)
        par3 = ev_stats.permutation_trades(rule, cache, n_perm=40, seed=7,
                                           workers=3)
        assert par == par3 == seq, "symbol-split null must match the sequential null"
        assert seq["pnl_p"] is not None and 0.0 <= seq["pnl_p"] <= 1.0
        assert seq["n_perm"] > 20

    def test_permutation_parallel_single_symbol_falls_back(self):
        # A one-symbol cache can't be sharded; the result must still come back
        # and match the sequential path (singleton guard in the workers branch).
        rng = np.random.default_rng(3)
        idx = pd.bdate_range("2021-01-04", periods=200)
        close = 50 * np.exp(np.cumsum(rng.normal(0, 0.005, size=200)))
        ent = np.zeros(200, dtype=bool)
        ent[rng.choice(200, size=8, replace=False)] = True
        ex = np.zeros(200, dtype=bool)
        ex[rng.choice(200, size=20, replace=False)] = True
        cache = {"AAA": pd.DataFrame({"close": close, "ent": ent, "ex": ex},
                                     index=idx)}
        rule = TradeRule(name="one", entries=lambda d: d["ent"],
                         exits=lambda d: d["ex"])
        seq = ev_stats.permutation_trades(rule, cache, n_perm=30, seed=5)
        par = ev_stats.permutation_trades(rule, cache, n_perm=30, seed=5,
                                          workers=8)
        assert par is not None and par == seq

    def test_permutation_portfolio_config_stays_classic(self):
        # A config with a capital/concurrency budget couples symbols and must
        # take the classic portfolio-pass loop; the result stays deterministic
        # AND respects the budget (not an error, no silent unbounded null).
        from evaluation import execution as ev_execution
        rng = np.random.default_rng(3)
        idx = pd.bdate_range("2021-01-04", periods=200)
        cache = {}
        for sym in ("AAA", "BBB", "CCC"):
            close = 50 * np.exp(np.cumsum(rng.normal(0, 0.005, size=200)))
            ent = np.zeros(200, dtype=bool)
            ent[rng.choice(200, size=8, replace=False)] = True
            ex = np.zeros(200, dtype=bool)
            ex[rng.choice(200, size=20, replace=False)] = True
            cache[sym] = pd.DataFrame({"close": close, "ent": ent, "ex": ex},
                                      index=idx)
        rule = TradeRule(name="cap", entries=lambda d: d["ent"],
                         exits=lambda d: d["ex"])
        cfg = ev_execution.ExecutionConfig(
            name="capped", limits=ev_execution.PortfolioLimits(capital=100_000.0))
        a = ev_stats.permutation_trades(rule, cache, n_perm=30, seed=1, config=cfg)
        b = ev_stats.permutation_trades(rule, cache, n_perm=30, seed=1, config=cfg,
                                        workers=4)
        assert a is not None and a == b

    def test_bh_fdr_known_vector(self):
        # NOTE: brief's original literals (0.039, 0.041) sit just ABOVE their
        # own BH thresholds (0.03, 0.04), so the largest-k-satisfying-p_(k)
        # <= alpha*k/m rule actually stops at k=2, not k=4 -- verified against
        # the reference bh_fdr implementation transcribed verbatim below.
        # Using 0.029/0.031 (just under the same thresholds) match the
        # comment's stated intent (reject first 4) without changing alpha,
        # m, or the algorithm.
        recs = [{"id": i, "p": p} for i, p in
                enumerate([0.001, 0.008, 0.029, 0.031, 0.20, None])]
        out = ev_stats.bh_fdr(recs, alpha=0.05)
        # m=5 valid; BH thresholds 0.01,0.02,0.03,0.04,0.05 -> reject first 4
        assert out.loc[out["id"] == 0, "reject"].item() is np.True_ or \
               bool(out.loc[out["id"] == 0, "reject"].item())
        assert bool(out.loc[out["id"] == 3, "reject"].item())
        assert not bool(out.loc[out["id"] == 4, "reject"].item())
        assert not bool(out.loc[out["id"] == 5, "reject"].item())   # None p
        assert pd.isna(out.loc[out["id"] == 5, "p_adj"].item())

    def test_noise_grid_survives_nothing(self):
        # spec falsification: pure-noise stats must NOT survive FDR
        recs = []
        for seed in range(12):
            p = _noise_panel(seed=seed + 10)
            d = ev_stats.daily_ic(p, "value", "fwd_1d")
            if d["ic_t_stat"] is not None:
                recs.append({"id": seed, "p": ev_stats.t_to_p(d["ic_t_stat"])})
        out = ev_stats.bh_fdr(recs, alpha=0.10)
        assert int(out["reject"].sum()) == 0


class TestTier3:
    def test_walk_forward_planted_oos_holds(self):
        p = _planted_panel(n_dates=400)
        r = ev_stats.walk_forward(p, "value", "fwd_1d", n_folds=4,
                                  min_train_days=126)
        assert len(r["folds"]) == 4
        assert r["oos"]["mean_daily_ic"] > 0.9
        assert all(f["mean_daily_ic"] > 0.9 for f in r["folds"])

    def test_walk_forward_too_short_reason(self):
        p = _planted_panel(n_dates=100)
        r = ev_stats.walk_forward(p, "value", "fwd_1d")
        assert r["oos"] is None and "dates" in r["wf_reason"]

    def test_regime_conditioning_partitions_days(self):
        # benchmark: 300 rising days (ends above SMA) then 200 falling days
        idx = pd.bdate_range("2022-01-03", periods=500)
        px = np.concatenate([100 * (1.004 ** np.arange(300)),
                             100 * (1.004 ** 299) * (0.996 ** np.arange(1, 201))])
        bench = pd.Series(px, index=idx)
        rng = np.random.default_rng(0)
        rows = [{"symbol": f"S{k}", "date": d, "value": float(rng.normal()),
                 "fwd_1d": float(rng.normal(scale=0.01))}
                for d in idx for k in range(6)]
        panel = pd.DataFrame(rows)
        r = ev_stats.regime_conditioning(panel, "value", "fwd_1d", bench)
        assert set(r) == {"bull", "bear", "high_vol", "low_vol"}
        assert r["bull"]["n_days"] > 0 and r["bear"]["n_days"] > 0
        # bull+bear cover exactly the SMA-defined dates
        assert r["bull"]["n_days"] + r["bear"]["n_days"] == 500 - 199

    def test_regime_short_benchmark_reason(self):
        bench = pd.Series(np.arange(50, dtype=float),
                          index=pd.bdate_range("2024-01-02", periods=50))
        r = ev_stats.regime_conditioning(_planted_panel(), "value", "fwd_1d", bench)
        assert "regime_reason" in r

    def test_deflated_sharpe_monotone_in_trial_dispersion(self):
        tight = ev_stats.deflated_sharpe(2.0, 500, [0.2, 0.3, 0.1, -0.1])
        wide = ev_stats.deflated_sharpe(2.0, 500, [3.0, -3.0, 2.5, -2.5])
        assert tight["dsr_prob"] > 0.9
        assert wide["dsr_prob"] < tight["dsr_prob"]

    def test_deflated_sharpe_small_population_reason(self):
        r = ev_stats.deflated_sharpe(2.0, 500, [1.0])
        assert r["dsr_prob"] is None and "population too small" in r["dsr_reason"]

    def test_registry_percentile(self):
        r = ev_stats.registry_percentile(0.5, [0.1, 0.2, 0.6, 0.9])
        assert r["percentile"] == 50.0 and r["n_population"] == 4
        assert "pct_reason" in ev_stats.registry_percentile(0.5, [0.1])


from evaluation import registry as ev_registry


def _reg_rows(run_id="r1", name="sig_a", value=0.02, created="2026-07-19T10:00:00",
              uhash="abc123", statistic="pooled_ic", horizon=1):
    return pd.DataFrame([{
        "run_id": run_id, "input_name": name, "input_type": "signal",
        "evaluation": "ic", "horizon": horizon, "statistic": statistic,
        "value": value, "n": 100, "universe_hash": uhash,
        "date_range": "2024-01-02..2025-01-31", "created_at": created,
    }])


class TestRegistry:
    def test_roundtrip_and_missing_file(self, tmp_path):
        path = str(tmp_path / "reg" / "results.parquet")
        assert ev_registry.load(path).empty
        assert list(ev_registry.load(path).columns) == ev_registry.COLUMNS
        n = ev_registry.append(_reg_rows(), path)
        assert n == 1
        reg = ev_registry.load(path)
        assert len(reg) == 1
        assert list(reg.columns) == ev_registry.COLUMNS
        assert not os.path.exists(path + ".tmp")

    def test_append_is_additive(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        ev_registry.append(_reg_rows(run_id="r1"), path)
        ev_registry.append(_reg_rows(run_id="r2"), path)
        assert len(ev_registry.load(path)) == 2

    def test_append_rejects_missing_columns(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        bad = _reg_rows().drop(columns=["statistic"])
        with pytest.raises(ValueError, match="statistic"):
            ev_registry.append(bad, path)

    def test_baselines_latest_wins(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        ev_registry.append(_reg_rows(run_id="r1", value=0.01,
                                     created="2026-07-18T10:00:00"), path)
        ev_registry.append(_reg_rows(run_id="r2", value=0.03,
                                     created="2026-07-19T10:00:00"), path)
        base = ev_registry.baselines(path=path)
        assert len(base) == 1
        assert base.iloc[0]["value"] == pytest.approx(0.03)
        assert base.iloc[0]["run_id"] == "r2"

    def test_compare_within_tol_and_universe_guard(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        ev_registry.append(_reg_rows(run_id="r1", value=0.010, uhash="aaa"), path)
        fresh_ok = _reg_rows(run_id="r2", value=0.012, uhash="aaa")
        cmp = ev_registry.compare(fresh_ok, path=path, tol=0.005)
        assert bool(cmp.iloc[0]["within_tol"]) is True
        assert cmp.iloc[0]["baseline"] == pytest.approx(0.010)
        fresh_far = _reg_rows(run_id="r3", value=0.030, uhash="aaa")
        cmp2 = ev_registry.compare(fresh_far, path=path, tol=0.005)
        assert bool(cmp2.iloc[0]["within_tol"]) is False
        fresh_mismatch = _reg_rows(run_id="r4", value=0.011, uhash="bbb")
        with pytest.raises(ValueError, match="universe"):
            ev_registry.compare(fresh_mismatch, path=path)
        cmp3 = ev_registry.compare(fresh_mismatch, path=path,
                                   allow_universe_mismatch=True)
        assert len(cmp3) == 1

    def test_population_latest_per_input(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        ev_registry.append(_reg_rows(run_id="r1", name="sig_a", value=1.0,
                                     statistic="sharpe", horizon=-1,
                                     created="2026-07-18T10:00:00"), path)
        ev_registry.append(_reg_rows(run_id="r2", name="sig_a", value=1.5,
                                     statistic="sharpe", horizon=-1,
                                     created="2026-07-19T10:00:00"), path)
        ev_registry.append(_reg_rows(run_id="r3", name="sig_b", value=-0.2,
                                     statistic="sharpe", horizon=-1), path)
        pop = ev_registry.population("sharpe", path=path)
        assert sorted(pop) == [pytest.approx(-0.2), pytest.approx(1.5)]
        assert ev_registry.population("nope", path=path) == []

    def test_population_exclude_input_name(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        ev_registry.append(_reg_rows(run_id="r1", name="sig_a", value=1.0,
                                     statistic="sharpe", horizon=-1,
                                     created="2026-07-18T10:00:00"), path)
        ev_registry.append(_reg_rows(run_id="r2", name="sig_b", value=-0.2,
                                     statistic="sharpe", horizon=-1,
                                     created="2026-07-18T10:00:00"), path)
        # excluding sig_a's own name drops its entry, keeps sig_b's
        pop = ev_registry.population("sharpe", path=path,
                                     exclude_input_name="sig_a")
        assert pop == [pytest.approx(-0.2)]
        # a different signal's own prior entries are unaffected
        pop_other = ev_registry.population("sharpe", path=path,
                                           exclude_input_name="sig_c")
        assert sorted(pop_other) == [pytest.approx(-0.2), pytest.approx(1.0)]
        # no exclusion (default) still returns both, matching prior behavior
        assert sorted(ev_registry.population("sharpe", path=path)) == \
            [pytest.approx(-0.2), pytest.approx(1.0)]

    def test_universe_hash_order_and_case_invariant(self):
        h1 = ev_registry.universe_hash(["AAPL", "MSFT", "SPY"])
        h2 = ev_registry.universe_hash(["spy", "msft", "aapl"])
        h3 = ev_registry.universe_hash(["AAPL", "MSFT"])
        assert h1 == h2
        assert h1 != h3
        assert len(h1) == 12

    def test_summary_is_ascii(self, tmp_path):
        path = str(tmp_path / "results.parquet")
        assert "empty" in ev_registry.summary(path)
        ev_registry.append(_reg_rows(), path)
        s = ev_registry.summary(path)
        assert s.isascii()
        assert "sig_a" in s and "1 rows" in s


import types

from evaluation import runner as ev_runner
import evaluate as ev_cli


class TestStatRows:
    def test_excludes_metadata_keeps_real_stats(self):
        d = {"pooled_ic": 0.05, "pooled_p": 0.02, "n": 900, "oriented": 1,
             "mean_daily_ic": 0.01, "ic_days": 250, "top_n": 200,
             "bottom_n": 200, "spread_pct": 0.1, "ic_pct_positive": 60.0}
        rows = ev_runner._stat_rows("ic", 5, d, n_key="n")
        stats = {r["statistic"] for r in rows}
        # metadata keys (counts/sizes/orientation flag) must not appear
        assert stats.isdisjoint({"n", "oriented", "ic_days", "top_n",
                                 "bottom_n"})
        # real measured statistics from the same dict must still appear
        assert {"pooled_ic", "pooled_p", "mean_daily_ic", "spread_pct",
                "ic_pct_positive"} <= stats
        # n_key's value still flows into every emitted row's n column
        assert all(r["n"] == 900 for r in rows)


def _fake_price_world(n=320, syms=("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"),
                      seed=3):
    idx = pd.bdate_range("2024-01-02", periods=n)
    rng = np.random.default_rng(seed)
    data = {s: 100.0 * np.cumprod(1 + rng.normal(0.0003, 0.01, n))
            for s in syms}
    data["SPY"] = 100.0 * np.cumprod(1 + rng.normal(0.0003, 0.008, n))
    return pd.DataFrame(data, index=idx)


def _runner_signal(closes, n_sig_dates=260, seed=7):
    syms = [c for c in closes.columns if c != "SPY"]
    dates = closes.index[:n_sig_dates]
    rng = np.random.default_rng(seed)
    rows = [{"symbol": s, "date": d, "value": float(rng.normal())}
            for d in dates for s in syms]
    return Signal(name="test_sig", frame=pd.DataFrame(rows), lag_days=1)


def _install_fake_market(monkeypatch, closes):
    """Fake the two repo modules the evaluation package imports locally."""
    fake_eb = types.SimpleNamespace(
        load_close_matrix=lambda syms, start=None, end=None, price_table=None:
            closes[[s for s in syms if s in closes.columns]])
    monkeypatch.setitem(sys.modules, "event_backtest", fake_eb)

    ls = closes.drop(columns=["SPY"]).pct_change().mean(axis=1).fillna(0.0)
    fake_res = types.SimpleNamespace(
        returns=ls, equity=(1 + ls).cumprod(),
        benchmark=closes["SPY"].pct_change().fillna(0.0),
        weights=None,
        metrics={"sharpe": 0.9, "cagr_pct": 7.5, "max_drawdown_pct": -12.0},
        params={"quantiles": 5, "rebalance": "M"})
    fake_bt = types.SimpleNamespace(backtest=lambda *a, **kw: fake_res)
    monkeypatch.setitem(sys.modules, "backtest", fake_bt)
    return fake_eb, fake_bt


class TestRunner:
    def test_signal_end_to_end(self, tmp_path, monkeypatch):
        closes = _fake_price_world()
        _install_fake_market(monkeypatch, closes)
        sig = _runner_signal(closes)
        reg_path = str(tmp_path / "reg" / "results.parquet")
        res = ev_runner.run(sig, out_root=str(tmp_path / "reports"),
                            registry_path=reg_path,
                            n_boot=50, n_perm=10, seed=0)
        assert res["input_type"] == "signal"
        assert res["n_evaluations"] >= 2          # ic + portfolio at minimum
        out_dir = res["out_dir"]
        for fname in ("results.json", "run_meta.json", "panel.parquet"):
            assert os.path.exists(os.path.join(out_dir, fname))
        with open(os.path.join(out_dir, "run_meta.json")) as fh:
            meta = json.load(fh)
        assert meta["input_name"] == "test_sig"
        assert meta["universe_hash"]
        assert "dropped" in meta and "git_commit" in meta
        assert ".." in meta["date_range"]
        ic1 = res["results"]["ic"][1]
        assert ic1["pooled_ic"] is not None
        assert "tier2" in res["results"] and "tier3" in res["results"]
        assert "fdr" in res["results"]
        reg = ev_registry.load(reg_path)
        assert res["rows_written"] == len(reg) > 0
        assert (reg["statistic"] == "pooled_ic").any()
        assert (reg["statistic"] == "sharpe").any()
        assert reg["run_id"].nunique() == 1

    def test_dsr_trials_exclude_own_prior_run(self, tmp_path, monkeypatch):
        """A re-run of an already-registered signal must not double-count
        its own prior sharpe in the DSR trial population (finding 1)."""
        closes = _fake_price_world()
        _install_fake_market(monkeypatch, closes)
        sig = _runner_signal(closes)
        reg_path = str(tmp_path / "reg" / "results.parquet")

        # Seed the registry as if this exact signal already ran once (own
        # prior sharpe=1.0), plus a different signal's own prior (2.0).
        ev_registry.append(_reg_rows(run_id="prior_x", name=sig.name,
                                     value=1.0, statistic="sharpe",
                                     horizon=-1,
                                     created="2026-07-18T10:00:00"), reg_path)
        ev_registry.append(_reg_rows(run_id="prior_y", name="other_sig",
                                     value=2.0, statistic="sharpe",
                                     horizon=-1,
                                     created="2026-07-18T10:00:00"), reg_path)

        captured = {}
        real_dsr = ev_stats.deflated_sharpe

        def fake_dsr(sharpe_ann, n_days, trials, *a, **kw):
            captured["trials"] = list(trials)
            return real_dsr(sharpe_ann, n_days, trials, *a, **kw)

        monkeypatch.setattr(ev_stats, "deflated_sharpe", fake_dsr)

        ev_runner.run(sig, out_root=str(tmp_path / "reports"),
                      registry_path=reg_path, n_boot=50, n_perm=10, seed=0)

        assert "trials" in captured, "DSR path did not run"
        trials = captured["trials"]
        # X's own stale prior entry (1.0) must be fully excluded -- not
        # merely deduped -- before this run's fresh sharpe_now is appended.
        assert trials.count(1.0) == 0
        # Y's own prior entry is a DIFFERENT signal's trial and must
        # still be counted.
        assert 2.0 in trials
        assert len(trials) == 2          # [other_sig's 2.0, this run's own]

    def test_signal_no_registry_write(self, tmp_path, monkeypatch):
        closes = _fake_price_world()
        _install_fake_market(monkeypatch, closes)
        sig = _runner_signal(closes)
        reg_path = str(tmp_path / "reg.parquet")
        res = ev_runner.run(sig, out_root=str(tmp_path / "reports"),
                            registry_path=reg_path, write_registry=False,
                            n_boot=20, n_perm=5)
        assert res["rows_written"] == 0
        assert not os.path.exists(reg_path)

    def test_events_dispatch(self, tmp_path, monkeypatch):
        fake_events_result = {
            "labels": {"up": {"n_events": 6,
                              "horizons": {1: {"n": 6, "mean_pct": 0.5,
                                               "t_stat": 1.2},
                                           5: {"n": 6, "mean_pct": 1.1,
                                               "t_stat": 1.8}},
                              "mean_car_pct": {0: 0.0, 1: 0.4}}},
            "skipped": {"tiny": "3 events < min_events=5"},
        }
        seen = {}

        def fake_evaluate_events(frame, **kw):
            seen["frame"] = frame
            seen["kw"] = kw
            return fake_events_result

        import evaluation.events as ev_events_mod
        monkeypatch.setattr(ev_events_mod, "evaluate_events",
                            fake_evaluate_events)
        ev = EventSet(name="test_ev", frame=pd.DataFrame({
            "symbol": ["AAA"] * 6, "label": ["up"] * 6,
            "date": pd.bdate_range("2024-02-01", periods=6)}), lag_days=1)
        reg_path = str(tmp_path / "reg.parquet")
        res = ev_runner.run(ev, out_root=str(tmp_path / "reports"),
                            registry_path=reg_path)
        # runner applied the 1-BDay lag before handing the frame over
        assert (pd.to_datetime(seen["frame"]["date"]).min()
                > pd.Timestamp("2024-02-01"))
        assert seen["kw"]["entry_lag"] == 1
        assert res["results"]["events"] == fake_events_result
        reg = ev_registry.load(reg_path)
        assert (reg["evaluation"] == "events:up").any()
        row = reg[(reg["evaluation"] == "events:up")
                  & (reg["horizon"] == 5) & (reg["statistic"] == "mean_pct")]
        assert row.iloc[0]["value"] == pytest.approx(1.1)

    def test_trade_rule_dispatch(self, tmp_path):
        idx = pd.bdate_range("2024-01-02", periods=40)
        close = pd.Series(np.linspace(100, 120, 40), index=idx)
        ent = np.zeros(40, dtype=bool)
        ent[[5, 20]] = True
        exi = np.zeros(40, dtype=bool)
        exi[[10, 25]] = True
        df = pd.DataFrame({"close": close, "ent": ent, "exi": exi}, index=idx)
        rule = TradeRule(name="test_rule",
                         entries=lambda d: d["ent"], exits=lambda d: d["exi"])
        reg_path = str(tmp_path / "reg.parquet")
        res = ev_runner.run(rule, cache={"AAA": df},
                            out_root=str(tmp_path / "reports"),
                            registry_path=reg_path, n_perm=20, seed=0)
        assert res["input_type"] == "trade_rule"
        assert res["results"]["summary"]["n_trades"] == 2
        assert os.path.exists(os.path.join(res["out_dir"], "trades.parquet"))
        perm = res["results"]["permutation"]
        assert perm["pnl_p"] is None or 0.0 <= perm["pnl_p"] <= 1.0
        reg = ev_registry.load(reg_path)
        assert (reg["evaluation"] == "trades").any()

    def test_trade_rule_requires_cache(self, tmp_path):
        rule = TradeRule(name="r", entries=lambda d: d["close"] > 0,
                         exits=lambda d: d["close"] < 0)
        with pytest.raises(ValueError, match="cache"):
            ev_runner.run(rule, out_root=str(tmp_path / "reports"))


from evaluation import adapters as ev_adapters


def _fake_panel_frame():
    dates = pd.bdate_range("2024-01-02", periods=10)
    rows = []
    for d in dates:
        for s in ("AAA", "BBB"):
            rows.append({"symbol": s, "date": d,
                         "momentum": 0.1, "value": -0.2, "composite": 0.05})
    return pd.DataFrame(rows)


class TestAdapters:
    def test_from_signal_panel(self, monkeypatch):
        import analytics.signals as sig_mod
        monkeypatch.setattr(sig_mod, "signal_panel",
                            lambda symbols=None, start=None, end=None:
                            _fake_panel_frame())
        s = ev_adapters.from_signal_panel(factor="momentum")
        assert isinstance(s, Signal)
        assert s.name == "factor_momentum"
        assert s.lag_days == 0 and s.direction == 1
        assert list(s.frame.columns) == ["symbol", "date", "value"]
        assert (s.frame["value"] == 0.1).all()

    def test_from_signal_panel_value_factor_no_collision(self, monkeypatch):
        # the FACTOR named "value" must land in the contract column "value"
        import analytics.signals as sig_mod
        monkeypatch.setattr(sig_mod, "signal_panel",
                            lambda symbols=None, start=None, end=None:
                            _fake_panel_frame())
        s = ev_adapters.from_signal_panel(factor="value")
        assert (s.frame["value"] == -0.2).all()

    def test_from_signal_panel_unknown_factor(self, monkeypatch):
        import analytics.signals as sig_mod
        monkeypatch.setattr(sig_mod, "signal_panel",
                            lambda symbols=None, start=None, end=None:
                            _fake_panel_frame())
        with pytest.raises(ValueError, match="factor"):
            ev_adapters.from_signal_panel(factor="nope")

    def test_from_signal_panel_eligible_filters_rows(self, monkeypatch):
        import analytics.signals as sig_mod
        import analytics.features as feat_mod
        monkeypatch.setattr(feat_mod, "feature_matrix",
                            lambda *a, **k: pd.DataFrame())
        monkeypatch.setattr(sig_mod, "signal_panel",
                            lambda fm=None, symbols=None, start=None, end=None:
                            _fake_panel_frame())
        dates = pd.bdate_range("2024-01-02", periods=10)
        eligible = pd.DataFrame({
            "symbol": ["AAA"] * len(dates) + ["BBB"] * len(dates),
            "date": list(dates) * 2,
            "eligible": [True] * len(dates) + [False] * len(dates),
        })
        s = ev_adapters.from_signal_panel(factor="momentum", eligible=eligible)
        assert set(s.frame["symbol"]) == {"AAA"}
        assert len(s.frame) == len(dates)

    def test_from_signal_panel_eligible_none_is_unfiltered(self, monkeypatch):
        import analytics.signals as sig_mod
        monkeypatch.setattr(sig_mod, "signal_panel",
                            lambda symbols=None, start=None, end=None:
                            _fake_panel_frame())
        s = ev_adapters.from_signal_panel(factor="momentum", eligible=None)
        assert set(s.frame["symbol"]) == {"AAA", "BBB"}

    def test_from_sentiment(self, monkeypatch):
        import sentiment_eval as se_mod
        fake = pd.DataFrame({"symbol": ["AAA", "BBB"],
                             "date": pd.to_datetime(["2024-01-02",
                                                     "2024-01-02"]),
                             "sent_score": [0.3, -0.1],
                             "n_articles": [4, 2]})
        monkeypatch.setattr(se_mod, "daily_signals",
                            lambda min_articles=1, start=None, end=None: fake)
        s = ev_adapters.from_sentiment()
        assert s.name == "news_sentiment"
        assert list(s.frame.columns) == ["symbol", "date", "value"]
        assert s.frame["value"].tolist() == [0.3, -0.1]

    def test_from_rating_history(self):
        idx = pd.bdate_range("2024-01-02", periods=6)
        cache = {"AAA": pd.DataFrame({"close": 100.0, "rating_all": 0.4,
                                      "rating_ma": 0.2}, index=idx),
                 "BBB": pd.DataFrame({"close": 50.0, "rating_all": -0.3,
                                      "rating_ma": -0.1}, index=idx)}
        s = ev_adapters.from_rating_history(signal_col="rating_all",
                                            cache=cache)
        assert s.name == "tv_rating_all"
        assert len(s.frame) == 12
        assert set(s.frame["symbol"]) == {"AAA", "BBB"}
        aaa = s.frame[s.frame["symbol"] == "AAA"]
        assert (aaa["value"] == 0.4).all()

    def test_from_rating_changes(self, monkeypatch):
        import event_backtest as eb_mod
        fake = pd.DataFrame({"symbol": ["AAA", "BBB"],
                             "date": pd.to_datetime(["2024-03-01",
                                                     "2024-03-04"]),
                             "from_label": ["neutral", "buy"],
                             "to_label": ["buy", "neutral"],
                             "from_score": [0.0, 0.5], "to_score": [0.5, 0.0],
                             "step": [1, 1], "direction": ["up", "down"]})
        seen = {}

        def fake_changes(symbols, start=None, end=None, min_step=1,
                         price_table=None, **kw):
            seen["start"] = start
            return fake

        monkeypatch.setattr(eb_mod, "rating_changes", fake_changes)
        ev = ev_adapters.from_rating_changes(symbols=["AAA", "BBB"],
                                             min_events=1)
        assert isinstance(ev, EventSet)
        assert ev.name == "tv_rating_changes"
        assert sorted(ev.frame["label"].unique()) == ["down", "up"]
        assert seen["start"] is not None      # full-history scan needs start

    def test_tv_threshold_rule_matches_legacy_semantics(self):
        import tv_rating_eval as tv
        rule = ev_adapters.tv_threshold_rule()
        assert isinstance(rule, TradeRule)
        assert rule.side == "both"
        assert rule.notional == tv.NOTIONAL
        idx = pd.bdate_range("2024-01-02", periods=6)
        df = pd.DataFrame({"close": 100.0,
                           "rating_all": [0.0, 0.6, 0.6, 0.05, 0.6, -0.6]},
                          index=idx)
        le = rule.entries(df).to_numpy()
        lx = rule.exits(df).to_numpy()
        se_ = rule.short_entries(df).to_numpy()
        sx = rule.short_exits(df).to_numpy()
        # long entry only on the CROSS up through +0.5 (days 1 and 4)
        assert le.tolist() == [False, True, False, False, True, False]
        # long exit whenever rating < +0.1 (days 0, 3, 5)
        assert lx.tolist() == [True, False, False, True, False, True]
        # short entry on the cross down through -0.5 (day 5)
        assert se_.tolist() == [False, False, False, False, False, True]
        # short exit whenever rating > -0.1 (days 0..4)
        assert sx.tolist() == [True, True, True, True, True, False]


class TestCliAdapters:
    def test_cli_adapter_signal_panel(self, tmp_path, monkeypatch, capsys):
        closes = _fake_price_world()
        _install_fake_market(monkeypatch, closes)
        import analytics.signals as sig_mod
        dates = closes.index[:260]
        syms = [c for c in closes.columns if c != "SPY"]
        rng = np.random.default_rng(11)
        rows = [{"symbol": s, "date": d, "composite": float(rng.normal())}
                for d in dates for s in syms]
        monkeypatch.setattr(sig_mod, "signal_panel",
                            lambda symbols=None, start=None, end=None:
                            pd.DataFrame(rows))
        rc = ev_cli.main(["--adapter", "signal-panel", "--factor", "composite",
                          "--out-root", str(tmp_path / "reports"),
                          "--registry-path", str(tmp_path / "reg.parquet"),
                          "--n-boot", "20", "--n-perm", "5"])
        assert rc == 0
        assert "factor_composite" in capsys.readouterr().out

    def test_cli_requires_exactly_one_source(self):
        with pytest.raises(SystemExit):
            ev_cli.main(["--name", "x"])                       # neither
        with pytest.raises(SystemExit):
            ev_cli.main(["--input-parquet", "a.parquet",
                         "--adapter", "sentiment", "--name", "x"])  # both

    def test_cli_adapter_tv_rule(self, tmp_path, monkeypatch, capsys):
        import evaluation.adapters as ad_mod
        idx = pd.bdate_range("2024-01-02", periods=30)
        df = pd.DataFrame({"close": np.linspace(100, 110, 30),
                           "rating_all": [0.0] * 5 + [0.6] * 5 + [0.0] * 20},
                          index=idx)
        monkeypatch.setattr(ad_mod, "rating_cache",
                            lambda **kw: {"AAA": df})
        rc = ev_cli.main(["--adapter", "tv-rule",
                          "--out-root", str(tmp_path / "reports"),
                          "--registry-path", str(tmp_path / "reg.parquet"),
                          "--n-perm", "10"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "tv_threshold" in out
        assert out.isascii()


class TestCli:
    def _write_signal_parquet(self, tmp_path, closes):
        sig = _runner_signal(closes)
        p = str(tmp_path / "sig.parquet")
        sig.frame.to_parquet(p, index=False)
        return p

    def test_cli_signal_happy_path(self, tmp_path, monkeypatch, capsys):
        closes = _fake_price_world()
        _install_fake_market(monkeypatch, closes)
        p = self._write_signal_parquet(tmp_path, closes)
        rc = ev_cli.main([
            "--input-parquet", p, "--input-type", "signal",
            "--name", "cli_sig", "--lag-days", "1",
            "--out-root", str(tmp_path / "reports"),
            "--registry-path", str(tmp_path / "reg.parquet"),
            "--n-boot", "20", "--n-perm", "5"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "cli_sig" in out
        assert out.isascii()

    def test_cli_zero_evaluations_exits_nonzero(self, tmp_path, monkeypatch,
                                                capsys):
        closes = _fake_price_world()
        _install_fake_market(monkeypatch, closes)
        frame = pd.DataFrame({"symbol": ["ZZZ"] * 5,
                              "date": pd.bdate_range("2024-02-01", periods=5),
                              "value": [1.0, 2.0, 3.0, 4.0, 5.0]})
        p = str(tmp_path / "zzz.parquet")
        frame.to_parquet(p, index=False)
        rc = ev_cli.main([
            "--input-parquet", p, "--name", "no_prices",
            "--out-root", str(tmp_path / "reports"), "--no-registry"])
        assert rc == 1

    def test_cli_rejects_bad_input_type(self, tmp_path):
        with pytest.raises(SystemExit):
            ev_cli.main(["--input-parquet", "x.parquet",
                         "--input-type", "bogus", "--name", "n"])


import generate_eval_report as ev_report


def _write_fake_run(root, name="fake_sig", ts="20260719_120000"):
    d = os.path.join(str(root), f"{name}_{ts}")
    os.makedirs(d, exist_ok=True)
    results = {
        "ic": {"1": {"pooled_ic": 0.011, "pooled_p": 0.2, "n": 900,
                     "mean_daily_ic": 0.010, "ic_t_stat": 1.1,
                     "ic_days": 250, "spread_pct": 0.05, "spread_t": 0.8,
                     "spread_p": 0.4, "top_n": 200, "bottom_n": 200,
                     "oriented": 1},
               "5": {"pooled_ic": 0.031, "pooled_p": 0.01, "n": 880,
                     "mean_daily_ic": 0.028, "ic_t_stat": 2.4,
                     "ic_days": 248, "spread_pct": 0.22, "spread_t": 2.1,
                     "spread_p": 0.04, "top_n": 190, "bottom_n": 190,
                     "oriented": 1}},
        "tier2": {"1": {"spread_boot_mean_pct": 0.05,
                        "spread_ci_lo_pct": -0.1, "spread_ci_hi_pct": 0.2,
                        "n_boot": 50, "boot_days": 250},
                  "5": {"spread_boot_mean_pct": 0.22,
                        "spread_ci_lo_pct": 0.02, "spread_ci_hi_pct": 0.4,
                        "n_boot": 50, "boot_days": 248}},
        "tier3": {"walk_forward": {"oos": {"mean_daily_ic": 0.02,
                                           "ic_t_stat": 1.5, "ic_days": 60},
                                   "n_train_days": 126},
                  "regimes": {"bull": {"n_days": 150, "mean_daily_ic": 0.03},
                              "bear": {"n_days": 100, "mean_daily_ic": -0.01},
                              "high_vol": {"n_days": 125,
                                           "mean_daily_ic": 0.02},
                              "low_vol": {"n_days": 125,
                                          "mean_daily_ic": 0.01}},
                  "deflated_sharpe": {"dsr_prob": 0.62, "sr0_ann": 0.8,
                                      "n_trials": 3}},
        "portfolio": {"metrics": {"sharpe": 0.9},
                      "sharpe_bootstrap": {"sharpe": 0.9,
                                           "sharpe_ci_lo": 0.1,
                                           "sharpe_ci_hi": 1.6,
                                           "n_boot": 50}},
        "fdr": [{"evaluation": "ic", "horizon": 5, "statistic": "pooled_p",
                 "p": 0.01, "p_adj": 0.05, "reject": True}],
    }
    meta = {"run_id": "abc123", "input_name": name, "input_type": "signal",
            "created_at": "2026-07-19T12:00:00+00:00", "git_commit": "deadbeef",
            "universe": ["AAA", "BBB"], "universe_hash": "aaa111bbb222",
            "date_range": "2024-01-02..2025-01-31",
            "dropped": {"ZZZ": "no price data"}, "n_evaluations": 4,
            "params": {"lag_days": 0, "direction": 1, "benchmark": "SPY"}}
    with open(os.path.join(d, "results.json"), "w") as fh:
        json.dump(results, fh)
    with open(os.path.join(d, "run_meta.json"), "w") as fh:
        json.dump(meta, fh)
    return d


class TestReport:
    def test_signal_report_end_to_end(self, tmp_path, capsys):
        d = _write_fake_run(tmp_path)
        out = str(tmp_path / "report.html")
        rc = ev_report.main(["--run-dir", d, "--out", out])
        assert rc == 0
        assert capsys.readouterr().out.isascii()
        html = open(out, encoding="utf-8").read()
        assert "plotly" in html.lower()
        assert "fake_sig" in html
        assert "deadbeef" in html          # provenance in the header

    def test_latest_picks_newest(self, tmp_path):
        _write_fake_run(tmp_path, ts="20260101_000000")
        d2 = _write_fake_run(tmp_path, ts="20260301_000000")
        assert ev_report.find_latest("fake_sig", root=str(tmp_path)) == d2
        assert ev_report.find_latest("nope", root=str(tmp_path)) is None

    def test_missing_run_dir_fails_cleanly(self, tmp_path, capsys):
        rc = ev_report.main(["--run-dir", str(tmp_path / "absent")])
        assert rc == 1
        assert "X" in capsys.readouterr().out

    def test_classify_significance_tiers(self):
        assert ev_report.classify_significance(0.01, 3.0) == "noise"
        assert ev_report.classify_significance(0.03, 1.0) == "noise"
        assert ev_report.classify_significance(0.03, 2.5) == "weak"
        assert ev_report.classify_significance(0.06, 2.5) == "significant"
        assert ev_report.classify_significance(None, None) == "noise"

    def test_trade_report(self, tmp_path):
        d = os.path.join(str(tmp_path), "rule_20260719_120000")
        os.makedirs(d)
        results = {"summary": {"n_trades": 2, "n_long": 2, "n_short": 0,
                               "total_pnl_dollars": 350.0,
                               "win_rate_pct": 100.0, "avg_pnl_pct": 1.7,
                               "median_days_held": 5.0, "n_symbols": 1},
                   "permutation": {"obs_pnl_dollars": 350.0,
                                   "obs_win_rate_pct": 100.0,
                                   "pnl_p": 0.2, "win_rate_p": 0.3,
                                   "n_perm": 20}}
        meta = {"run_id": "r", "input_name": "rule", "input_type":
                "trade_rule", "created_at": "2026-07-19", "git_commit": "x",
                "universe": ["AAA"], "universe_hash": "h",
                "date_range": "2024-01-02..2024-03-01", "dropped": {},
                "n_evaluations": 2, "params": {}}
        with open(os.path.join(d, "results.json"), "w") as fh:
            json.dump(results, fh)
        with open(os.path.join(d, "run_meta.json"), "w") as fh:
            json.dump(meta, fh)
        pd.DataFrame({"symbol": ["AAA", "AAA"], "side": ["long", "long"],
                      "pnl_pct": [2.0, 1.4], "pnl_dollars": [200.0, 150.0],
                      "days_held": [5, 6]}).to_parquet(
            os.path.join(d, "trades.parquet"), index=False)
        out = str(tmp_path / "rule.html")
        assert ev_report.main(["--run-dir", d, "--out", out]) == 0
        assert "rule" in open(out, encoding="utf-8").read()
