"""
test_curated.py — deduplication correctness for the curated layer.

Pure-logic tests on synthetic DataFrames (no disk data required), plus a
data-guarded integrity check that curated reads carry no duplicate keys.
"""

import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import curated
import query as q


class TestKeyedDedup:
    def test_keeps_latest_fetched_per_key(self):
        # prices key = [symbol, date]; two fetches of the same bar, newer wins
        df = pd.DataFrame({
            "symbol": ["AAPL", "AAPL", "MSFT"],
            "date":   ["2024-01-02", "2024-01-02", "2024-01-02"],
            "close":  [100.0, 101.5, 50.0],      # 101.5 is the corrected value
            "fetched_at": ["2024-01-02T00:00Z", "2024-01-03T00:00Z", "2024-01-02T00:00Z"],
        })
        out = curated.dedup("prices", df)
        assert len(out) == 2
        aapl = out[out["symbol"] == "AAPL"].iloc[0]
        assert aapl["close"] == 101.5  # kept the later-fetched correction

    def test_no_duplicate_keys_remain(self):
        df = pd.DataFrame({
            "symbol": ["A"] * 5,
            "date":   ["2024-01-02"] * 5,
            "close":  [1, 2, 3, 4, 5],
            "fetched_at": pd.date_range("2024-01-02", periods=5, freq="D").astype(str),
        })
        out = curated.dedup("prices", df)
        assert len(out) == 1
        assert out.iloc[0]["close"] == 5


class TestSchwabOptionsSnapshotDate:
    """
    Schwab serves no options history, so schwab_options' entire history exists
    only because the daily job appends one snapshot per session. snapshot_date
    is the column that makes each session a distinct fact; drop it from the key
    and every day ever captured collapses into one row per contract.

    Added 2026-08-11 together with the column. Before that this table had no
    KEYS entry and the full-row fallback happened to preserve history because
    days_to_expiration and the quote fields move daily -- an accident, not a
    guarantee, and one the _compact_large_table() path (which this table's
    ~93k rows/day will require) does not support at all.
    """

    def _chain(self, snapshot_dates, bid=1.0):
        n = len(snapshot_dates)
        return pd.DataFrame({
            "symbol":          ["AAPL"] * n,
            "expiration_date": ["2026-09-18"] * n,
            "strike":          [200.0] * n,
            "put_call":        ["CALL"] * n,
            "snapshot_date":   list(snapshot_dates),
            "bid":             [bid] * n,
            "fetched_at":      [f"{d}T13:00:00" for d in snapshot_dates],
        })

    def test_separate_sessions_are_kept(self):
        # Identical quote on three days -- without snapshot_date in the key these
        # would collapse to one row and two sessions of history would vanish.
        out = curated.dedup("schwab_options", self._chain(
            ["2026-08-05", "2026-08-06", "2026-08-07"]
        ))
        assert len(out) == 3
        assert sorted(out["snapshot_date"]) == ["2026-08-05", "2026-08-06", "2026-08-07"]

    def test_intraday_refetch_collapses_to_the_latest(self):
        # A 429 retry re-fetches the same contract the same session. That is one
        # fact, not two, and the newest fetch wins.
        df = self._chain(["2026-08-05", "2026-08-05"])
        df.loc[0, "bid"] = 1.0
        df.loc[1, "bid"] = 1.25
        df.loc[0, "fetched_at"] = "2026-08-05T13:00:00"
        df.loc[1, "fetched_at"] = "2026-08-05T13:04:00"
        out = curated.dedup("schwab_options", df)
        assert len(out) == 1
        assert out.iloc[0]["bid"] == 1.25

    def test_snapshot_date_is_backfilled_from_fetched_at(self):
        # Raw parquet written before the column existed is never rewritten, so
        # dedup has to derive it -- otherwise the key is incomplete and the whole
        # table silently reverts to the full-row fallback on every rebuild.
        df = self._chain(["2026-08-05", "2026-08-06"]).drop(columns=["snapshot_date"])
        out = curated.dedup("schwab_options", df)
        assert len(out) == 2
        assert sorted(out["snapshot_date"]) == ["2026-08-05", "2026-08-06"]

    def test_key_is_used_not_the_full_row_fallback(self):
        df = self._chain(["2026-08-05"])
        assert curated._dedup_subset("schwab_options", df) == curated.KEYS["schwab_options"]


class TestFullRowFallback:
    def test_unkeyed_table_drops_exact_duplicates(self):
        # 'finnhub_news' is not in KEYS -> full-row dedup
        df = pd.DataFrame({
            "headline": ["x", "x", "y"],
            "url":      ["u1", "u1", "u2"],
            "fetched_at": ["t1", "t2", "t1"],   # bookkeeping ignored in comparison
        })
        out = curated.dedup("finnhub_news", df)
        assert len(out) == 2  # the two identical x/u1 rows collapse

    def test_partial_key_falls_back_to_full_row(self):
        # gas_retail key needs duoarea/product/price_type; omit them entirely.
        # A too-coarse partial key (date only) would wrongly merge distinct rows,
        # so the safe behaviour is full-row dedup => all 3 distinct rows survive.
        df = pd.DataFrame({
            "date": ["2024-01-02"] * 3,
            "price_usd_gallon": [3.10, 3.45, 3.90],
            "fetched_at": ["t1", "t1", "t1"],
        })
        out = curated.dedup("gas_retail", df)
        assert len(out) == 3


class TestDedupSubset:
    def test_full_key_used_when_all_present(self):
        df = pd.DataFrame({"symbol": ["A"], "date": ["2024-01-02"], "close": [1.0]})
        assert curated._dedup_subset("prices", df) == ["symbol", "date"]

    def test_bookkeeping_excluded_in_fallback(self):
        df = pd.DataFrame({"a": [1], "b": [2], "fetched_at": ["t"], "year": [2024], "month": [1]})
        subset = curated._dedup_subset("finnhub_news", df)
        assert "fetched_at" not in subset and "year" not in subset and "month" not in subset
        assert set(subset) == {"a", "b"}

    def test_empty_frame_returns_unchanged(self):
        assert curated.dedup("prices", pd.DataFrame()).empty


class TestIdempotentOutput:
    def test_dedup_is_idempotent(self):
        df = pd.DataFrame({
            "symbol": ["A", "A", "B"],
            "date":   ["2024-01-02", "2024-01-02", "2024-01-02"],
            "close":  [1.0, 2.0, 3.0],
            "fetched_at": ["t1", "t2", "t1"],
        })
        once = curated.dedup("prices", df)
        twice = curated.dedup("prices", once)
        pd.testing.assert_frame_equal(once.reset_index(drop=True), twice.reset_index(drop=True))


@pytest.mark.parametrize("table", ["prices", "macro", "fundamentals_annual"])
def test_curated_views_have_no_duplicate_keys(table):
    """If a curated snapshot exists, its natural key must be unique (NULL-safe)."""
    key = curated.KEYS.get(table)
    if not key or q.load(table, limit=1).empty:
        pytest.skip(f"{table}: no data or no key")
    cols = q.schema(table)["column_name"].tolist()
    if not all(c in cols for c in key):
        pytest.skip(f"{table}: key columns absent")
    keycsv = ",".join(key)
    dups = q.sql(
        f"SELECT COUNT(*) c FROM (SELECT {keycsv}, COUNT(*) n "
        f"FROM {table} GROUP BY {keycsv} HAVING COUNT(*) > 1)"
    ).iloc[0, 0]
    assert dups == 0, f"{table} has {dups} duplicated keys after curation"


class TestPeriodEndNormalization:
    """period_end is a natural-key column stored inconsistently across snapshots
    (some write bare '2017-09-30', others timestamp '2017-09-30 00:00:00'), which
    used to create phantom duplicates that made downstream drop_duplicates/ASOF
    joins order-sensitive. curated must normalize to date-only before dedup."""

    def _frame(self):
        # same fact (cik/metric/period_end/form/unit) fetched in two snapshots
        return pd.DataFrame({
            "cik":           ["123", "123", "123", "123"],
            "metric":        ["revenue"] * 4,
            "period_end":    ["2017-09-30", "2017-09-30 00:00:00",
                              "2018-09-29", "2018-09-29 00:00:00"],
            "fiscal_period": ["FY"] * 4,
            "form":          ["10-K"] * 4,
            "unit":          ["USD"] * 4,
            "value":         [100.0, 101.5, 200.0, 202.0],  # later fetch is correction
            "fetched_at":    ["2026-06-15T00:00:00", "2026-08-04T00:00:00",
                              "2026-06-15T00:00:00", "2026-08-04T00:00:00"],
        })

    def test_normalizes_period_end_format_before_dedup(self):
        out = curated.dedup("fundamentals_annual", self._frame())
        # both format variants of the same date must collapse to ONE fact
        assert len(out) == 2
        assert sorted(out["value"].tolist()) == [101.5, 202.0]  # newest fetch wins

    def test_output_period_end_is_date_only(self):
        out = curated.dedup("fundamentals_annual", self._frame())
        assert not out["period_end"].astype(str).str.contains(" ").any()

    def test_non_fundamentals_table_untouched(self):
        # prices' natural key has no period_end — normalization is a no-op
        df = pd.DataFrame({
            "symbol": ["AAPL"], "date": ["2024-01-02"], "close": [1.0],
            "fetched_at": ["t"],
        })
        out = curated.dedup("prices", df)
        assert out.iloc[0]["close"] == 1.0


# -- price sanity filter (added 2026-08-29) -----------------------------------

class TestPriceSanity:
    """
    Non-positive prices are dropped at curation. See curated._PRICE_SANITY --
    `prices` carried 173,178 such rows (161,783 negative) from Schwab's deep
    history, which is what made event_study()'s baseline return 3.36e+22.
    """

    def _df(self):
        return pd.DataFrame({
            "symbol": ["A", "B", "C", "D", "E"],
            "open":   [10.0, -1.0,  5.0, None,  2.0],
            "high":   [11.0,  1.0,  5.0,  3.0,  2.0],
            "low":    [ 9.0,  1.0,  0.0,  3.0,  2.0],
            "close":  [10.5,  1.0,  5.0,  3.0,  0.0],
        })

    def test_drops_negative_and_zero_keeps_null(self):
        out = curated._drop_nonpositive_prices("prices", self._df())
        # B negative open, C zero low, E zero close -> dropped.
        # D has a NULL open but is otherwise valid -> kept.
        assert list(out["symbol"]) == ["A", "D"]

    def test_untouched_when_table_not_listed(self):
        df = self._df()
        assert len(curated._drop_nonpositive_prices("some_other_table", df)) == len(df)

    def test_instruments_that_can_go_negative_are_excluded(self):
        # WTI settled at -$37.63 on 2020-04-20 and that print is really in both
        # tables; options expire worthless at 0. Filtering them would destroy
        # real data, so they must stay out of _PRICE_SANITY.
        for table in ("futures", "market_history", "options_history"):
            assert table not in curated._PRICE_SANITY

    def test_sql_predicate_matches_the_pandas_filter(self):
        # prices goes through the DuckDB path (_LARGE_TABLES), every other
        # table through the pandas path -- they must agree.
        pred = curated._sanity_sql_predicate("prices")
        for col in curated._PRICE_SANITY["prices"]:
            assert f"({col} IS NULL OR {col} > 0)" in pred
        assert curated._sanity_sql_predicate("futures") == ""

    def test_prices_uses_the_duckdb_path(self):
        # If prices ever leaves _LARGE_TABLES the SQL predicate stops being
        # exercised, so this guards the assumption above.
        assert "prices" in curated._LARGE_TABLES

    def test_dedup_applies_the_filter(self):
        df = self._df()
        df["fetched_at"] = "2026-08-29T00:00:00"
        out = curated.dedup("sector_etfs", df)
        assert list(out["symbol"]) == ["A", "D"]

