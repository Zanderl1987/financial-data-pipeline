"""
tests/test_sp500_membership_pipeline.py -- sp500_membership_pipeline.py's
point-in-time reconstruction (reconstruct_membership), pure and network-free.
"""

import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from sp500_membership_pipeline import _norm_ticker, reconstruct_membership


def _changes(rows):
    """rows: list of (date, add_ticker, rem_ticker), already newest-first --
    matches fetch_changes()'s own sort order."""
    df = pd.DataFrame(rows, columns=["date", "add_ticker", "rem_ticker"])
    df["date"] = pd.to_datetime(df["date"])
    return df


class TestNormTicker:
    def test_dots_become_dashes(self):
        assert _norm_ticker("BRK.B") == "BRK-B"

    def test_nan_and_none_become_none(self):
        assert _norm_ticker(None) is None
        assert _norm_ticker(float("nan")) is None


class TestReconstructMembership:
    def test_worked_example_from_docstring(self):
        """A add/rem swap, a chained swap, a straight add, and a straight
        removal -- traced by hand in the module docstring's design notes."""
        current = {"A", "B", "C"}
        changes = _changes([
            ("2020-01-01", "A", "X"),
            ("2010-01-01", "X", "Y"),
            ("2005-01-01", "B", None),
            ("2000-01-01", None, "Z"),
        ])
        out = reconstruct_membership(current, changes)
        by_sym = {r.symbol: (r.start_date, r.end_date) for r in out.itertuples()}

        assert by_sym["A"][0] == pd.Timestamp("2020-01-01") and pd.isna(by_sym["A"][1])
        assert by_sym["X"] == (pd.Timestamp("2010-01-01"),
                               pd.Timestamp("2020-01-01"))
        assert by_sym["B"][0] == pd.Timestamp("2005-01-01") and pd.isna(by_sym["B"][1])
        # never mentioned in the log at all -> current, left-censored
        assert pd.isna(by_sym["C"][0]) and pd.isna(by_sym["C"][1])
        # left-censored, later removed
        assert pd.isna(by_sym["Y"][0]) and by_sym["Y"][1] == pd.Timestamp("2010-01-01")
        assert pd.isna(by_sym["Z"][0]) and by_sym["Z"][1] == pd.Timestamp("2000-01-01")
        assert len(out) == 6

    def test_as_of_membership_matches_hand_trace(self):
        """The as-of query this feeds (historical_constituents) applied
        directly against the reconstructed intervals, at each of the four
        distinct membership regimes the worked example produces."""
        current = {"A", "B", "C"}
        changes = _changes([
            ("2020-01-01", "A", "X"),
            ("2010-01-01", "X", "Y"),
            ("2005-01-01", "B", None),
            ("2000-01-01", None, "Z"),
        ])
        out = reconstruct_membership(current, changes)

        def members_as_of(d):
            d = pd.Timestamp(d)
            mask = ((out["start_date"].isna() | (out["start_date"] <= d))
                    & (out["end_date"].isna() | (out["end_date"] > d)))
            return set(out.loc[mask, "symbol"])

        assert members_as_of("1995-01-01") == {"C", "Y", "Z"}
        assert members_as_of("2007-01-01") == {"B", "C", "Y"}
        assert members_as_of("2015-01-01") == {"X", "B", "C"}
        assert members_as_of("2025-01-01") == {"A", "B", "C"}

    def test_re_added_symbol_gets_two_intervals(self):
        """A symbol removed then later re-added is two separate intervals,
        not one merged span -- real S&P 500 history has names that bounce
        in and out."""
        current = {"A"}
        changes = _changes([
            ("2020-01-01", "A", "B"),   # A re-added, B leaves
            ("2015-01-01", "B", "A"),   # A left, B added
        ])
        out = reconstruct_membership(current, changes)
        a_rows = out[out["symbol"] == "A"].sort_values("start_date",
                                                        na_position="first")
        assert len(a_rows) == 2
        first, second = a_rows.iloc[0], a_rows.iloc[1]
        assert pd.isna(first["start_date"]) and first["end_date"] == pd.Timestamp("2015-01-01")
        assert second["start_date"] == pd.Timestamp("2020-01-01") and pd.isna(second["end_date"])

    def test_current_member_closed_with_no_reopen_gets_repaired(self):
        """A real, confirmed source-data gap: a symbol still a CURRENT
        member whose only log appearance is a removal with no later
        re-addition (MCK/T live, 2026-09-06). Must not silently drop the
        symbol from the open/current result, and must not invent an
        unknown real re-entry date -- tile a second interval onto the
        recorded closure date instead."""
        current = {"MCK", "OTHER"}
        changes = _changes([
            ("1994-09-30", "NCC", "MCK"),   # MCK's only appearance anywhere
        ])
        out = reconstruct_membership(current, changes)
        mck_rows = out[out["symbol"] == "MCK"].sort_values(
            "start_date", na_position="first")
        assert len(mck_rows) == 2
        closed, reopened = mck_rows.iloc[0], mck_rows.iloc[1]
        assert pd.isna(closed["start_date"])
        assert closed["end_date"] == pd.Timestamp("1994-09-30")
        assert reopened["start_date"] == pd.Timestamp("1994-09-30")
        assert pd.isna(reopened["end_date"])
        # the boundary date itself resolves to "member" via the reopened
        # interval (tiles exactly, no coverage gap) -- verified the same
        # way historical_constituents()'s as-of query would read it.
        d = pd.Timestamp("1994-09-30")
        covers = ((mck_rows["start_date"].isna() | (mck_rows["start_date"] <= d))
                 & (mck_rows["end_date"].isna() | (mck_rows["end_date"] > d)))
        assert covers.any()
        # a normal current member untouched by any gap keeps exactly one row
        assert len(out[out["symbol"] == "OTHER"]) == 1

    def test_non_current_add_with_no_later_removal_is_dropped_not_guessed(self):
        """Fixed 2026-09-06 (code review caught it): a non-current symbol
        whose addition is logged but whose eventual removal never is (real
        example: NCC/National City, added 1994, acquired by PNC 2008, its
        own removal never logged) must NOT default to an open/current-ish
        interval -- that would silently claim it as a member for its whole
        real, multi-decade absence. It should be dropped entirely rather
        than assigned a guessed end date."""
        current = {"A"}
        changes = _changes([
            ("1994-09-30", "NCC", None),   # NCC's only log appearance, ever
        ])
        out = reconstruct_membership(current, changes)
        assert "NCC" not in set(out["symbol"])
        # A (the only current ticker) still gets its normal left-censored row
        assert len(out) == 1
        assert out.iloc[0]["symbol"] == "A"
        assert pd.isna(out.iloc[0]["start_date"]) and pd.isna(out.iloc[0]["end_date"])

    def test_non_current_add_later_resolved_by_a_removal_still_works(self):
        """Contrast case: a non-current symbol's add DOES get resolved by a
        later (chronologically earlier, since the log walks newest-first)
        removal event -- must still produce a normal, real closed interval,
        not be dropped."""
        current = {"A"}
        changes = _changes([
            ("2020-01-01", "A", "X"),    # X removed when A (re)joins
            ("2010-01-01", "X", None),   # X added in 2010
        ])
        out = reconstruct_membership(current, changes)
        x_row = out[out["symbol"] == "X"].iloc[0]
        assert x_row["start_date"] == pd.Timestamp("2010-01-01")
        assert x_row["end_date"] == pd.Timestamp("2020-01-01")

    def test_empty_changes_leaves_current_fully_left_censored(self):
        out = reconstruct_membership({"A", "B"}, _changes([]))
        assert len(out) == 2
        assert out["start_date"].isna().all()
        assert out["end_date"].isna().all()
