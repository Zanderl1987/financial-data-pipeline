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
from evaluation.universe import (
    clean_symbols, exchange_listed_symbols, flag_price_jumps,
    historical_constituents, point_in_time_eligible,
)


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


class TestFlagPriceJumps:
    def _register(self, con, name, df):
        con.register(name, df)
        return name

    def test_flags_unadjusted_split_style_jump(self):
        con = q._con()
        dates = pd.bdate_range("2024-01-02", periods=10)
        # CLEAN behaves normally; JUMPY has a >3x single-day ratio (like an
        # unadjusted split), GRADUAL drifts a lot but never jumps in one day.
        df = pd.concat([
            pd.DataFrame({"symbol": "CLEAN", "date": dates.strftime("%Y-%m-%d"),
                         "close": [10 + 0.1 * i for i in range(10)]}),
            pd.DataFrame({"symbol": "JUMPY", "date": dates.strftime("%Y-%m-%d"),
                         "close": [10, 10.5, 11, 400, 405, 410, 415, 420, 425, 430]}),
            pd.DataFrame({"symbol": "GRADUAL", "date": dates.strftime("%Y-%m-%d"),
                         "close": [10 * (1.15 ** i) for i in range(10)]}),
        ], ignore_index=True)
        name = "test_price_jumps"
        self._register(con, name, df)
        try:
            flagged = flag_price_jumps(["CLEAN", "JUMPY", "GRADUAL"], price_table=name)
        finally:
            con.unregister(name)
        assert set(flagged["symbol"]) == {"JUMPY"}

    def test_clean_symbols_excludes_flagged(self):
        con = q._con()
        dates = pd.bdate_range("2024-01-02", periods=5)
        df = pd.concat([
            pd.DataFrame({"symbol": "CLEAN", "date": dates.strftime("%Y-%m-%d"),
                         "close": [10, 10.1, 10.2, 10.3, 10.4]}),
            pd.DataFrame({"symbol": "JUMPY", "date": dates.strftime("%Y-%m-%d"),
                         "close": [10, 10.5, 500, 505, 510]}),
        ], ignore_index=True)
        name = "test_clean_symbols"
        self._register(con, name, df)
        try:
            out = clean_symbols(["CLEAN", "JUMPY"], price_table=name)
        finally:
            con.unregister(name)
        assert out == ["CLEAN"]

    def test_empty_symbols_returns_empty_frame(self):
        out = flag_price_jumps([], price_table="prices")
        assert out.empty
        assert list(out.columns) == ["symbol", "max_abs_log_ret", "min_close"]


class TestHistoricalConstituents:
    def _register(self, con, name, df):
        con.register(name, df)
        return name

    def test_open_start_and_open_end_both_count_as_member(self):
        con = q._con()
        df = pd.DataFrame([
            {"symbol": "ALWAYS", "start_date": None, "end_date": None},
            {"symbol": "LEFT_EARLY", "start_date": None,
             "end_date": "2010-01-01"},
            {"symbol": "JOINED_LATE", "start_date": "2020-01-01",
             "end_date": None},
            {"symbol": "MID_ONLY", "start_date": "2005-01-01",
             "end_date": "2015-01-01"},
        ])
        name = "test_sp500_membership_a"
        self._register(con, name, df)
        try:
            out_2007 = historical_constituents("2007-01-01", table=name)
            out_2022 = historical_constituents("2022-01-01", table=name)
        finally:
            con.unregister(name)
        # LEFT_EARLY hasn't left yet as of 2007 (leaves 2010) -- it belongs
        # in the 2007 set despite its name, which describes 2022's view.
        assert out_2007 == ["ALWAYS", "LEFT_EARLY", "MID_ONLY"]
        assert out_2022 == ["ALWAYS", "JOINED_LATE"]

    def test_end_date_boundary_is_exclusive(self):
        con = q._con()
        df = pd.DataFrame([
            {"symbol": "LEAVING", "start_date": None, "end_date": "2020-06-01"},
        ])
        name = "test_sp500_membership_b"
        self._register(con, name, df)
        try:
            still_in = historical_constituents("2020-05-31", table=name)
            already_out = historical_constituents("2020-06-01", table=name)
        finally:
            con.unregister(name)
        assert still_in == ["LEAVING"]
        assert already_out == []

    def test_unsupported_index_raises(self):
        with pytest.raises(ValueError, match="SPX"):
            historical_constituents("2020-01-01", index="NDX")
