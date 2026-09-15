"""
Tests for strategies/portfolio.py -- the Phase 8 combined-survivor portfolio.
Uses synthetic rules + monkeypatched catalog/engine/cache so these never
touch real data or the eval registry.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import evaluation.trades as ev_trades  # noqa: E402
import strategies.portfolio as pf  # noqa: E402
from evaluation.contracts import TradeRule  # noqa: E402


def _long_rule(name="long", fast=2):
    """Crosses up through the 2-day EMA -> buy; crosses down -> sell."""
    def crosses_up(s1, s2):
        return (s1 > s2) & (s1.shift(1) <= s2.shift(1))

    def entries(df):
        return crosses_up(df["close"], df["close"].rolling(fast).mean())

    def exits(df):
        return crosses_up(df["close"].rolling(fast).mean(), df["close"])

    return TradeRule(name=name, side="long", entries=entries, exits=exits)


def _short_rule(name="short", period=2):
    """RSI<30 -> short; RSI>70 -> cover (time-symmetric counterpart)."""
    def entries(df):
        delta = df["close"].diff()
        up = delta.clip(lower=0).rolling(period).mean()
        dn = (-delta).clip(lower=0).rolling(period).mean()
        rsi = 100 - 100 / (1 + up / (dn + 1e-12))
        return rsi < 30

    def exits(df):
        delta = df["close"].diff()
        up = delta.clip(lower=0).rolling(period).mean()
        dn = (-delta).clip(lower=0).rolling(period).mean()
        rsi = 100 - 100 / (1 + up / (dn + 1e-12))
        return rsi > 70

    return TradeRule(name=name, side="short", entries=entries, exits=exits)


def _ohlcv(dates, closes, volumes=None):
    df = pd.DataFrame({"date": dates})
    df["close"] = pd.Series(closes, dtype=float)
    df["open"] = df["close"]
    df["high"] = df["close"]
    df["low"] = df["close"]
    df["volume"] = (pd.Series(volumes) if volumes is not None
                    else pd.Series(1000.0, index=range(len(df))))
    return df.set_index("date")


def _cache_for(dfs):
    return {f"S{i}": df for i, df in enumerate(dfs)}


class TestSurvivorSlugs:
    def test_returns_stage5_rows_sorted(self, monkeypatch):
        import pandas as pd
        c = pd.DataFrame({
            "strategy_id": ["z_b", "a_x", "m_y"],
            "stage": ["stage5", "stage5", "stage2"],
        })
        monkeypatch.setattr(pf, "build_catalog_rows", lambda: c)
        assert pf.survivor_slugs() == ["a_x", "z_b"]

    def test_empty_when_no_stage_column(self, monkeypatch):
        monkeypatch.setattr(pf, "build_catalog_rows",
                            lambda: pd.DataFrame({"strategy_id": ["a"]}))
        assert pf.survivor_slugs() == []

    def test_empty_when_empty_catalog(self, monkeypatch):
        monkeypatch.setattr(pf, "build_catalog_rows",
                            lambda: pd.DataFrame())
        assert pf.survivor_slugs() == []


class TestCombineSurvivors:
    def test_union_is_side_both_with_or_signals(self, monkeypatch):
        rules = [_long_rule("L"), _short_rule("S")]
        monkeypatch.setattr(pf, "load_rule_for",
                            lambda slug: (rules[int(slug)], "unit_tested"))
        monkeypatch.setattr(pf, "with_price_floor",
                            lambda r, floor: r)

        dates = pd.date_range("2026-01-01", periods=12, freq="D")
        # Uptrend: long rule crosses up mid-window.
        closes = [10, 10, 11, 12, 13, 14, 14, 14, 13, 12, 11, 10]
        df = _ohlcv(dates, closes)
        rule = pf.combine_survivors(["0", "1"])
        assert rule.side == "both"
        le = rule.entries(df)
        se = rule.short_entries(df)
        assert le.dtype == bool
        assert se.dtype == bool
        # Union fires somewhere on both sides (uptrend buys, RSI<30 may also).
        assert le.any()

    def test_union_preserves_each_sides_signals(self, monkeypatch):
        rules = [_long_rule("L"), _short_rule("S")]
        monkeypatch.setattr(pf, "load_rule_for",
                            lambda slug: (rules[int(slug)], "unit_tested"))
        monkeypatch.setattr(pf, "with_price_floor",
                            lambda r, floor: r)

        dates = pd.date_range("2026-01-01", periods=10, freq="D")
        df = _ohlcv(dates, [10, 10, 10, 10, 3, 3, 3, 3, 12, 12])
        rule = pf.combine_survivors(["0", "1"])
        se = rule.short_entries(df)
        sx = rule.short_exits(df)
        # The crash into 3 must fire a short entry; the pop to 12 must cover.
        assert se.any()
        assert sx.any()
        # A lone long rule must NOT contribute to the short side.
        rules2 = [_long_rule("L")]
        monkeypatch.setattr(pf, "load_rule_for",
                            lambda slug: (rules2[int(slug)], "unit_tested"))
        lonerule = pf.combine_survivors(["0"])
        assert not lonerule.short_entries(df).any()


class TestPortfolioConfig:
    def test_hrp_sizing_and_capital(self):
        cfg = pf.portfolio_config()
        assert cfg.sizing.mode == "hrp"
        # fraction < 1.0 is the regression guard from the 2026-09-08 full
        # run: 1.0 gives the first trade the whole budget in _hrp_size's
        # n=1 case, so the book could never hold a second name (serial
        # 1-name book, defeating Phase 8's multi-name intent).
        assert cfg.sizing.fraction == 0.4
        assert 0 < cfg.sizing.fraction < 1
        assert cfg.limits.max_concurrent == 8
        assert cfg.limits.capital is not None
        assert 0 < cfg.limits.capital
        # Same campaign cost model as Stage 3/5: sqrt_law impact.
        assert cfg.costs.impact_model == "sqrt_law"
        assert cfg.costs.commission_bps == 1.0


class TestRunCombined:
    def test_simulate_called_with_combined_rule_and_config(self, monkeypatch):
        captured = {}

        def fake_simulate(rule, cache, notional=None, config=None):
            captured["rule"] = rule
            captured["config"] = config
            return pd.DataFrame(columns=ev_trades.TRADE_COLS)

        monkeypatch.setattr(ev_trades, "simulate", fake_simulate)
        monkeypatch.setattr(pf, "survivor_slugs", lambda: ["a", "b"])
        monkeypatch.setattr(pf, "load_rule_for",
                            lambda slug: (_long_rule(slug), "unit_tested"))
        monkeypatch.setattr(pf, "with_price_floor", lambda r, floor: r)

        cfg = pf.portfolio_config()
        trades, summary = pf.run_combined({"S0": _ohlcv(pd.date_range("2025-01-01", periods=5), [1, 2, 3, 4, 5])},
                                          config=cfg)
        assert captured["rule"].side == "both"
        assert captured["rule"].name == "a+b"
        assert captured["config"] is cfg

    def test_default_survivors_and_config(self, monkeypatch):
        called = {}

        def fake_simulate(rule, cache, notional=None, config=None):
            called["cfg"] = config
            return pd.DataFrame(columns=ev_trades.TRADE_COLS)

        monkeypatch.setattr(ev_trades, "simulate", fake_simulate)
        monkeypatch.setattr(pf, "survivor_slugs", lambda: ["solo"])
        monkeypatch.setattr(pf, "load_rule_for",
                            lambda slug: (_long_rule(slug), "unit_tested"))
        monkeypatch.setattr(pf, "with_price_floor", lambda r, floor: r)
        cache = {"S0": _ohlcv(pd.date_range("2025-01-01", periods=5), [1, 2, 3, 4, 5])}
        trades, summary = pf.run_combined(cache)
        assert called["cfg"].sizing.mode == "hrp"
        assert summary["strategies"] == ["solo"]


class TestRunCombinedOpen:
    def test_returns_trades_open_and_summary(self, monkeypatch):
        captured = {}
        open_df = pd.DataFrame({
            "symbol": ["S0"], "side": ["long"],
            "entry_signal_date": pd.Timestamp("2026-01-03"),
            "entry_date": pd.Timestamp("2026-01-04"),
            "entry_price": [10.0], "weight": [0.4], "size": [40000.0]})

        def fake_simulate_open(rule, cache, notional=None, config=None):
            captured["rule"] = rule
            captured["config"] = config
            return pd.DataFrame(columns=ev_trades.TRADE_COLS), open_df

        monkeypatch.setattr(ev_trades, "simulate_open", fake_simulate_open)
        monkeypatch.setattr(pf, "survivor_slugs", lambda: ["a", "b"])
        monkeypatch.setattr(pf, "load_rule_for",
                            lambda slug: (_long_rule(slug), "unit_tested"))
        monkeypatch.setattr(pf, "with_price_floor", lambda r, floor: r)

        cfg = pf.portfolio_config()
        trades, open_out, summary = pf.run_combined_open(
            {"S0": _ohlcv(pd.date_range("2026-01-01", periods=6), [10] * 6)},
            config=cfg)
        assert captured["rule"].name == "a+b"
        assert captured["config"] is cfg
        assert trades.empty
        assert list(open_out["symbol"]) == ["S0"]
        assert open_out["weight"].iloc[0] == 0.4
        assert summary["strategies"] == ["a", "b"]

    def test_default_survivors(self, monkeypatch):
        called = {}

        def fake_simulate_open(rule, cache, notional=None, config=None):
            called["cfg"] = config
            return (pd.DataFrame(columns=ev_trades.TRADE_COLS),
                    pd.DataFrame(columns=ev_trades.OPEN_COLS))

        monkeypatch.setattr(ev_trades, "simulate_open", fake_simulate_open)
        monkeypatch.setattr(pf, "survivor_slugs", lambda: ["solo"])
        monkeypatch.setattr(pf, "load_rule_for",
                            lambda slug: (_long_rule(slug), "unit_tested"))
        monkeypatch.setattr(pf, "with_price_floor", lambda r, floor: r)
        trades, open_out, summary = pf.run_combined_open(
            {"S0": _ohlcv(pd.date_range("2026-01-01", periods=5), [1, 2, 3, 4, 5])})
        assert open_out.empty
        assert called["cfg"].sizing.mode == "hrp"


class TestDailyOpenBookPersistence:
    def test_daily_run_writes_current_open_book(self, monkeypatch, tmp_path):
        open_df = pd.DataFrame({
            "symbol": ["AAPL"], "side": ["long"],
            "entry_signal_date": pd.Timestamp("2026-09-11"),
            "entry_date": pd.Timestamp("2026-09-14"),
            "entry_price": [210.5], "weight": [0.25], "size": [250000.0]})
        trades = pd.DataFrame(columns=ev_trades.TRADE_COLS)
        summary = {"n_trades": 0, "summary_reason": "no realized trades",
                   "strategies": ["a"]}
        dates = pd.date_range("2025-09-01", periods=10)
        cache = {"S0": _ohlcv(dates, [10.0] * 10)}

        monkeypatch.setattr(pf, "survivor_slugs", lambda: ["a"])
        monkeypatch.setattr(pf, "_load_live_cache",
                            lambda symbols=None, lookback_days=252: cache)
        monkeypatch.setattr(pf, "run_combined_open",
                            lambda c, slugs=None, config=None, notional=None:
                            (trades, open_df, summary))
        monkeypatch.setattr(pf, "DAILY_ARTIFACT_DIR", str(tmp_path))

        out_trades, out_summary = pf.run_daily_paper_trade(
            confirm=True, register=False, write=False)
        parquet_path = tmp_path / pf.OPEN_BOOK_FILE
        assert parquet_path.exists()
        saved = pd.read_parquet(parquet_path)
        assert list(saved["symbol"]) == ["AAPL"]
        assert saved["weight"].iloc[0] == 0.25
        state_path = tmp_path / pf.OPEN_BOOK_STATE
        assert state_path.exists()
        import json
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["n_open"] == 1
        assert state["data_edge"] == dates[-1].strftime("%Y-%m-%d")
        assert state["strategies"] == ["a"]
        assert out_trades is trades
        assert out_summary is summary

    def test_daily_empty_book_writes_empty_state(self, monkeypatch, tmp_path):
        trades = pd.DataFrame(columns=ev_trades.TRADE_COLS)
        open_df = pd.DataFrame(columns=ev_trades.OPEN_COLS)
        summary = {"n_trades": 0, "summary_reason": "no realized trades",
                   "strategies": ["a"]}
        cache = {"S0": _ohlcv(pd.date_range("2025-09-01", periods=10), [10.0] * 10)}
        monkeypatch.setattr(pf, "survivor_slugs", lambda: ["a"])
        monkeypatch.setattr(pf, "_load_live_cache",
                            lambda symbols=None, lookback_days=252: cache)
        monkeypatch.setattr(pf, "run_combined_open",
                            lambda c, slugs=None, config=None, notional=None:
                            (trades, open_df, summary))
        monkeypatch.setattr(pf, "DAILY_ARTIFACT_DIR", str(tmp_path))
        pf.run_daily_paper_trade(confirm=True, register=False, write=False)
        assert (tmp_path / pf.OPEN_BOOK_FILE).exists()
        import json
        state = json.loads((tmp_path / pf.OPEN_BOOK_STATE).read_text(encoding="utf-8"))
        assert state["n_open"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-q"])