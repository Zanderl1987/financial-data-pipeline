"""
Tests for evaluation.stats.circular_shift_trades -- the TV catalog's primary
endpoint since the 2026-10-28 amendment.

The defect it replaces: permutation_trades relocates entries but keeps the exit
FLAGS, and a rule whose exits are derived from its own entries (stop, target,
max-bars walk) then leaves relocated entries with no exit until the real path's
next one. The tests below pin the property that matters -- the null holds the
strategy's time in market -- on exactly that kind of rule.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation import stats as ev_stats
from evaluation.contracts import TradeRule


def _walk_exit_cache(n_syms=4, n=600, hold=5, seed=0, n_setups=40):
    """Random-walk prices; entries on random setup days; exits placed `hold`
    bars after each entry the strategy would actually take -- a max-bars exit
    walked from the strategy's own entries, the shape 13 of 18 ports have."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2000-01-03", periods=n)
    cache = {}
    for i in range(n_syms):
        close = 50 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, size=n)))
        ent = np.zeros(n, dtype=bool)
        ent[rng.choice(n - hold - 2, size=n_setups, replace=False)] = True
        ex = np.zeros(n, dtype=bool)
        j = 0
        while j < n:
            if ent[j]:
                k = min(j + hold, n - 1)
                ex[k] = True
                j = k + 1
            else:
                j += 1
        cache[f"S{i}"] = pd.DataFrame({"close": close, "ent": ent, "ex": ex}, index=idx)
    return cache


def _rule(name="walk"):
    return TradeRule(name=name, entries=lambda d: d["ent"], exits=lambda d: d["ex"])


class TestShiftFlags:
    def test_rolls_all_four_arrays_together(self):
        le = np.array([1, 0, 0, 0, 0], dtype=bool)
        lx = np.array([0, 0, 1, 0, 0], dtype=bool)
        se = np.array([0, 1, 0, 0, 0], dtype=bool)
        sx = np.array([0, 0, 0, 1, 0], dtype=bool)
        close = np.full(5, 10.0)
        ple, plx, pse, psx = ev_stats._shift_flags(le, lx, se, sx, close, 2)
        assert ple.tolist() == np.roll(le, 2).tolist()
        assert plx.tolist() == np.roll(lx, 2).tolist()
        assert pse.tolist() == np.roll(se, 2).tolist()
        assert psx.tolist() == np.roll(sx, 2).tolist()

    def test_floor_drops_shifted_entries_only(self):
        le = np.array([1, 0, 0, 0], dtype=bool)
        lx = np.array([0, 1, 0, 0], dtype=bool)
        se = np.array([0, 0, 1, 0], dtype=bool)
        sx = np.array([0, 0, 0, 1], dtype=bool)
        close = np.array([10.0, 3.0, np.nan, 10.0])
        # offset 1: long entry lands on bar 1 ($3), short entry on bar 3 ($10)
        ple, plx, pse, psx = ev_stats._shift_flags(le, lx, se, sx, close, 1,
                                                   entry_floor=5.0)
        assert not ple.any(), "entry shifted onto a $3 bar must be dropped"
        assert pse.tolist() == [False, False, False, True]
        assert plx.tolist() == np.roll(lx, 1).tolist(), "exits are never gated"
        # NaN close is not >= floor
        ple2, *_ = ev_stats._shift_flags(le, lx, se, sx, close, 2, entry_floor=5.0)
        assert not ple2.any()


class TestCircularShiftTrades:
    def test_null_holds_time_in_market_on_walk_derived_exits(self):
        cache = _walk_exit_cache()
        r = ev_stats.circular_shift_trades(_rule(), cache, n_perm=60, seed=0)
        lo, hi = ev_stats.SHIFT_EXPOSURE_BOUNDS
        assert lo <= r["null_exposure_ratio"] <= hi
        assert r["exposure_mismatch"] is False

    def test_old_null_breaks_exposure_on_the_same_rule(self):
        """The defect this endpoint replaces, pinned so it can't be mistaken for
        a fixture artifact: relocating entries under fixed walk-derived exits
        makes the null hold far longer than the strategy."""
        from evaluation import trades as tr
        cache = _walk_exit_cache()
        rule = _rule()
        obs_days = tr.simulate(rule, cache)["days_held"].sum()
        flags = []
        for i, (sym, df) in enumerate(cache.items()):
            le, lx, se, sx = tr.rule_flags(rule, df)
            flags.append((i, sym, df.index, df["close"].to_numpy(float), None,
                          le, lx, se, sx))
        days = []
        for s in range(10):
            d = 0.0
            for i, sym, index, close, vol, le, lx, se, sx in flags:
                rng = np.random.default_rng((s, i))
                ple = np.zeros(len(index), dtype=bool)
                ple[rng.choice(len(index), size=int(np.sum(le)), replace=False)] = True
                for row in tr.simulate_symbol(index, close, ple, lx, se, sx, sym,
                                              rule.notional):
                    d += row["days_held"]
            days.append(d)
        assert np.median(days) / obs_days > 1.5

    def test_floor_is_applied_to_the_null(self):
        """Half of every series sits below $5: the observed rule only enters
        above it, so a floor-respecting null drops entries but never trades a
        sub-$5 day."""
        cache = _walk_exit_cache(seed=3)
        for sym, df in cache.items():
            df.loc[df.index[: len(df) // 2], "close"] = 2.0
            df["ent"] = df["ent"] & (df["close"] >= 5.0)
        gated = ev_stats.circular_shift_trades(_rule(), cache, n_perm=40, seed=0,
                                               entry_floor=5.0)
        ungated = ev_stats.circular_shift_trades(_rule(), cache, n_perm=40, seed=0)
        assert gated["null_exposure_ratio"] < ungated["null_exposure_ratio"]

    def test_detects_real_timing(self):
        """Entries peek two bars ahead (allowed in a fixture, never in a port):
        the strategy must beat its own pattern placed at random times."""
        rng = np.random.default_rng(5)
        idx = pd.bdate_range("2000-01-03", periods=500)
        cache = {}
        for i in range(3):
            close = pd.Series(50 * np.exp(np.cumsum(rng.normal(0, 0.01, 500))), index=idx)
            up = (close.shift(-2) > close.shift(-1)).fillna(False).to_numpy()
            ex = np.roll(up, 1)
            cache[f"T{i}"] = pd.DataFrame({"close": close, "ent": up, "ex": ex}, index=idx)
        r = ev_stats.circular_shift_trades(_rule("peek"), cache, n_perm=99, seed=0)
        assert r["pnl_p"] <= 0.02

    def test_random_rule_is_not_significant(self):
        cache = _walk_exit_cache(seed=9)
        r = ev_stats.circular_shift_trades(_rule(), cache, n_perm=99, seed=0)
        assert r["pnl_p"] > 0.01

    def test_parallel_matches_sequential_and_is_deterministic(self):
        cache = _walk_exit_cache(n_syms=5, seed=11)
        seq = ev_stats.circular_shift_trades(_rule(), cache, n_perm=30, seed=7)
        again = ev_stats.circular_shift_trades(_rule(), cache, n_perm=30, seed=7)
        par = ev_stats.circular_shift_trades(_rule(), cache, n_perm=30, seed=7,
                                             workers=3)
        assert seq == again == par

    def test_no_trades_reason(self):
        idx = pd.bdate_range("2024-01-02", periods=30)
        cache = {"AAA": pd.DataFrame({"close": np.full(30, 100.0),
                                      "ent": False, "ex": False}, index=idx)}
        r = ev_stats.circular_shift_trades(_rule("never"), cache, n_perm=20)
        assert r["pnl_p"] is None and "no realized trades" in r["perm_reason"]

    def test_portfolio_config_refused(self):
        from evaluation import execution as ev_execution
        cfg = ev_execution.ExecutionConfig(
            name="capped", limits=ev_execution.PortfolioLimits(capital=100_000.0))
        with pytest.raises(ValueError, match="fixed-notional"):
            ev_stats.circular_shift_trades(_rule(), _walk_exit_cache(), n_perm=20,
                                           config=cfg)
