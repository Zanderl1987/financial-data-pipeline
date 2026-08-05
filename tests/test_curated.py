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
