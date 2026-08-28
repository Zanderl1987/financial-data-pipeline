"""
Tests for the pure/deterministic helpers in strategies/stage3.py -- the
Stage 3 (development test) runner for the TV strategy catalog campaign.
Does not exercise run_strategy()/run_all() (those need live price data and
real compute); covers the split logic, price-floor gate, and descriptive
stats that were previously untested.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.stage3 import (  # noqa: E402
    MANUAL_OVERRIDE_EXCLUDE,
    _is_holdout_symbol,
    _max_drawdown_pct,
    _profit_factor,
    _trade_sharpe,
    admitted_slugs,
    dev_holdout_symbols,
    with_price_floor,
)
from evaluation.contracts import TradeRule  # noqa: E402


# ------------------------------------------------------------ symbol split

def test_is_holdout_symbol_deterministic():
    """Same symbol always classifies the same way -- the split must be
    reproducible across runs/sessions, not seeded off anything time-based."""
    for _ in range(3):
        assert _is_holdout_symbol("AAPL") == _is_holdout_symbol("AAPL")


def test_dev_holdout_symbols_partitions_without_overlap():
    symbols = [f"SYM{i}" for i in range(200)]
    dev, holdout = dev_holdout_symbols(symbols)
    assert set(dev) & set(holdout) == set()
    assert set(dev) | set(holdout) == set(symbols)
    assert all(_is_holdout_symbol(s) for s in holdout)
    assert all(not _is_holdout_symbol(s) for s in dev)


def test_dev_holdout_symbols_roughly_quarter_holdout():
    """sha256(symbol) % 4 == 0 should land near 25% over a large sample --
    not exact (hash isn't a stratified sampler), but should not be wildly off."""
    symbols = [f"SYM{i}" for i in range(2000)]
    dev, holdout = dev_holdout_symbols(symbols)
    frac = len(holdout) / len(symbols)
    assert 0.20 < frac < 0.30


# ------------------------------------------------------------ admission list

def test_manual_override_exclude_slugs_are_absent_from_admitted():
    """screen_source() only pattern-matches Pine syntax and can't catch a
    domain mismatch (session-gated intraday logic, missing provenance, an
    engine-incompatible mechanism) -- see
    storage/tv_scripts/STAGE2_TRANSLATION_EXCLUSIONS.md. These slugs must
    never resurface in the campaign's authoritative admitted list, which
    catalog.py and the FDR family size both depend on."""
    admitted = admitted_slugs()
    for slug in MANUAL_OVERRIDE_EXCLUDE:
        assert slug not in admitted, \
            f"{slug} should be excluded (reason: {MANUAL_OVERRIDE_EXCLUDE[slug]})"


# ------------------------------------------------------------ price floor gate

def _flat_frame(price: float, n: int = 5) -> pd.DataFrame:
    return pd.DataFrame({"close": [price] * n})


def test_with_price_floor_blocks_entries_below_floor():
    rule = TradeRule(
        name="always_enter",
        entries=lambda d: pd.Series([True] * len(d), index=d.index),
        exits=lambda d: pd.Series([False] * len(d), index=d.index),
        side="long",
    )
    gated = with_price_floor(rule, floor=5.0)
    below = gated.entries(_flat_frame(3.0))
    above = gated.entries(_flat_frame(6.0))
    assert not below.any()
    assert above.all()


def test_with_price_floor_gates_both_sides_when_side_is_both():
    rule = TradeRule(
        name="always_enter_both",
        entries=lambda d: pd.Series([True] * len(d), index=d.index),
        exits=lambda d: pd.Series([False] * len(d), index=d.index),
        side="both",
        short_entries=lambda d: pd.Series([True] * len(d), index=d.index),
        short_exits=lambda d: pd.Series([False] * len(d), index=d.index),
    )
    gated = with_price_floor(rule, floor=5.0)
    assert not gated.entries(_flat_frame(3.0)).any()
    assert not gated.short_entries(_flat_frame(3.0)).any()
    assert gated.short_entries(_flat_frame(6.0)).all()


def test_with_price_floor_leaves_original_rule_unmodified():
    """replace() must build a new TradeRule, not mutate the one passed in --
    run_strategy() reuses the same loaded rule object across cost scenarios."""
    orig_entries = lambda d: pd.Series([True] * len(d), index=d.index)  # noqa: E731
    rule = TradeRule(name="r", entries=orig_entries,
                     exits=lambda d: pd.Series([False] * len(d), index=d.index),
                     side="long")
    with_price_floor(rule, floor=5.0)
    assert rule.entries is orig_entries


# ------------------------------------------------------------ descriptive stats

def _trades_df(pnl_dollars, pnl_pct=None, exit_dates=None):
    n = len(pnl_dollars)
    return pd.DataFrame({
        "pnl_dollars": pnl_dollars,
        "pnl_pct": pnl_pct if pnl_pct is not None else pnl_dollars,
        "exit_date": exit_dates if exit_dates is not None else pd.date_range("2020-01-01", periods=n),
    })


def test_profit_factor_empty_trades_is_none():
    assert _profit_factor(pd.DataFrame(columns=["pnl_dollars"])) is None


def test_profit_factor_no_losses_is_none():
    """Division by zero losses is undefined, not infinite -- guard against a
    strategy with zero realized losing trades reporting a bogus number."""
    assert _profit_factor(_trades_df([10.0, 20.0])) is None


def test_profit_factor_basic_ratio():
    trades = _trades_df([100.0, -50.0, 50.0, -25.0])
    assert _profit_factor(trades) == pytest.approx(150.0 / 75.0)


def test_trade_sharpe_needs_at_least_two_trades():
    assert _trade_sharpe(_trades_df([10.0])) is None


def test_trade_sharpe_zero_variance_is_none():
    assert _trade_sharpe(_trades_df([5.0, 5.0, 5.0])) is None


def test_trade_sharpe_basic():
    pct = [1.0, 2.0, -1.0, 3.0]
    trades = _trades_df(pct, pnl_pct=pct)
    expected = np.mean(pct) / np.std(pct, ddof=1)
    assert _trade_sharpe(trades) == pytest.approx(expected, abs=1e-3)


def test_max_drawdown_suppressed_above_5000_trades():
    """Numerical-underflow artifact discovered during the Stage 3 smoke test:
    chaining tens of thousands of per-trade returns multiplicatively underflows
    toward -100% even with a tiny average edge decay -- suppressed as None
    above the threshold rather than reported as a real number."""
    n = 5001
    pct = [0.01] * n
    trades = _trades_df(pct, pnl_pct=pct)
    assert _max_drawdown_pct(trades) is None


def test_max_drawdown_computes_below_threshold():
    pct = [10.0, -20.0, 5.0]
    trades = _trades_df(pct, pnl_pct=pct)
    dd = _max_drawdown_pct(trades)
    assert dd is not None
    assert dd < 0


def test_max_drawdown_empty_is_none():
    assert _max_drawdown_pct(pd.DataFrame(columns=["pnl_pct", "exit_date"])) is None
