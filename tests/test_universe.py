"""
test_universe.py -- evaluation/universe.py: point-in-time universe
construction for full-universe factor evaluation.
"""

import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import query as q
from evaluation.universe import exchange_listed_symbols, point_in_time_eligible


class TestExchangeListedSymbols:
    def test_excludes_otc_by_default(self):
        listed = exchange_listed_symbols()
        everyone = exchange_listed_symbols(exclude_otc=False)
        assert len(listed) < len(everyone)

    def test_returns_sorted_unique_list(self):
        syms = exchange_listed_symbols()
        assert syms == sorted(set(syms))
        assert len(syms) > 0


class TestPointInTimeEligible:
    def _register(self, con, name, df):
        con.register(name, df)
        return name

    def test_excludes_low_volume_and_flips_once_liquid(self):
        con = q._con()
        dates = pd.bdate_range("2024-01-02", periods=60)
        # thin volume for the first 40 rows, then a sustained spike
        volume = [1_000] * 40 + [10_000_000] * 20
        df = pd.DataFrame({
            "symbol": "TEST", "date": dates.strftime("%Y-%m-%d"),
            "close": 10.0, "volume": volume,
        })
        name = "test_pit_prices_a"
        self._register(con, name, df)
        try:
            out = point_in_time_eligible(["TEST"], min_dollar_volume=1_000_000,
                                         price_table=name).sort_values("date")
        finally:
            con.unregister(name)
        out = out.reset_index(drop=True)
        assert len(out) == 60
        assert not out.iloc[:39]["eligible"].any()
        assert bool(out.iloc[-1]["eligible"])

    def test_no_lookahead(self):
        """Eligibility on an early date must not depend on rows that come
        after it -- otherwise a future volume spike would retroactively make
        history look more liquid than it actually was (exactly the bias this
        module exists to avoid)."""
        con = q._con()
        dates = pd.bdate_range("2024-01-02", periods=30)
        df_full = pd.DataFrame({
            "symbol": "TEST", "date": dates.strftime("%Y-%m-%d"),
            "close": 10.0,
            "volume": [1_000] * 25 + [10_000_000] * 5,
        })
        df_short = df_full.iloc[:5].copy()

        name_full, name_short = "test_pit_prices_full", "test_pit_prices_short"
        self._register(con, name_full, df_full)
        self._register(con, name_short, df_short)
        try:
            out_full = point_in_time_eligible(["TEST"], min_dollar_volume=1_000_000,
                                              price_table=name_full).sort_values("date")
            out_short = point_in_time_eligible(["TEST"], min_dollar_volume=1_000_000,
                                               price_table=name_short).sort_values("date")
        finally:
            con.unregister(name_full)
            con.unregister(name_short)
        assert bool(out_full.iloc[0]["eligible"]) == bool(out_short.iloc[0]["eligible"])

    def test_empty_symbols_returns_empty_frame(self):
        out = point_in_time_eligible([], min_dollar_volume=1_000_000)
        assert out.empty
        assert list(out.columns) == ["symbol", "date", "eligible"]
