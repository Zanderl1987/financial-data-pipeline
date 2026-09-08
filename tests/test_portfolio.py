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


if __name__ == "__main__":
    pytest.main([__file__, "-q"])