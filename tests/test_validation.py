"""
test_validation.py — verify the data validation layer.

No data files or API keys needed.  Tests use synthetic DataFrames.
Confirms:
  - SCHEMAS covers every CATALOG table
  - individual check functions return the right severity
  - validate_df / validate_table / validate_all behave correctly
"""

import sys
import os
import datetime
import pytest
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import validate as v
import query as q


# ── Schema completeness ────────────────────────────────────────────────────────

class TestSchemaRegistry:
    def test_all_catalog_tables_have_schema(self):
        missing = [t for t in q.CATALOG if t not in v.SCHEMAS]
        assert not missing, f"Tables in CATALOG without a validation schema: {missing}"

    def test_all_schemas_have_required_keys(self):
        for table, schema in v.SCHEMAS.items():
            assert "required"    in schema, f"{table}: missing 'required'"
            assert "critical_nn" in schema, f"{table}: missing 'critical_nn'"
            assert "date_col"    in schema, f"{table}: missing 'date_col'"

    def test_critical_nn_is_subset_of_required(self):
        for table, schema in v.SCHEMAS.items():
            extra = set(schema["critical_nn"]) - set(schema["required"])
            assert not extra, f"{table}: critical_nn has cols not in required: {extra}"


# ── Check-function unit tests ─────────────────────────────────────────────────

class TestIndividualChecks:
    def _prices_df(self, rows: int = 50) -> pd.DataFrame:
        today = datetime.date.today().isoformat()
        return pd.DataFrame({
            "symbol":     ["AAPL"] * rows,
            "date":       [today] * rows,
            "open":       [150.0] * rows,
            "high":       [155.0] * rows,
            "low":        [149.0] * rows,
            "close":      [152.0] * rows,
            "volume":     [1_000_000] * rows,
            "fetched_at": [datetime.datetime.utcnow().isoformat()] * rows,
        })

    def test_valid_df_passes(self):
        df = self._prices_df()
        result = v.validate_df("prices", df, check_freshness=False)
        assert result.passed
        assert len(result.errors) == 0

    def test_empty_df_is_error(self):
        df = pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])
        result = v.validate_df("prices", df, check_freshness=False)
        assert not result.passed
        assert any(c.name == "not_empty" for c in result.errors)

    def test_missing_required_column_is_error(self):
        df = self._prices_df().drop(columns=["close"])
        result = v.validate_df("prices", df, check_freshness=False)
        assert not result.passed
        req = next(c for c in result.checks if c.name == "required_cols")
        assert not req.passed

    def test_mostly_null_critical_column_is_error(self):
        df = self._prices_df()
        df["close"] = None
        result = v.validate_df("prices", df, check_freshness=False)
        assert not result.passed
        null_check = next((c for c in result.checks if c.name == "nulls:close"), None)
        assert null_check is not None and not null_check.passed

    def test_minority_nulls_in_critical_col_is_warning(self):
        df = self._prices_df(rows=100)
        df.loc[:9, "close"] = None  # 10% null
        result = v.validate_df("prices", df, check_freshness=False)
        null_check = next((c for c in result.checks if c.name == "nulls:close"), None)
        assert null_check is not None
        assert null_check.severity == v.Severity.WARNING

    def test_future_dates_are_warning_not_error(self):
        df = self._prices_df()
        future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        df["date"] = future
        result = v.validate_df("prices", df, check_freshness=False)
        future_check = next((c for c in result.checks if c.name == "future_dates"), None)
        assert future_check is not None
        assert future_check.severity == v.Severity.WARNING
        # A single WARNING should not fail the whole result
        assert result.passed

    def test_value_range_violation_is_warning(self):
        df = pd.DataFrame({
            "symbol":     ["AAPL"] * 10,
            "sentiment":  ["bullish"] * 10,
            "score":      [2.5] * 10,   # out of [-1, 1]
            "confidence": [0.9] * 10,
            "key_topics": ["earnings"] * 10,
            "date":       [datetime.date.today().isoformat()] * 10,
            "fetched_at": [datetime.datetime.utcnow().isoformat()] * 10,
        })
        result = v.validate_df("news_sentiment", df, check_freshness=False)
        range_check = next((c for c in result.checks if "range:score" in c.name), None)
        assert range_check is not None
        assert range_check.severity == v.Severity.WARNING

    def test_confidence_range_check_present_for_news_sentiment(self):
        df = pd.DataFrame({
            "symbol":     ["AAPL"] * 5,
            "sentiment":  ["neutral"] * 5,
            "score":      [0.0] * 5,
            "confidence": [0.8] * 5,
            "key_topics": [""] * 5,
            "date":       [datetime.date.today().isoformat()] * 5,
            "fetched_at": [datetime.datetime.utcnow().isoformat()] * 5,
        })
        result = v.validate_df("news_sentiment", df, check_freshness=False)
        assert any("range:confidence" in c.name for c in result.checks)

    def test_unknown_table_returns_warning_not_error(self):
        df = pd.DataFrame({"col": [1, 2, 3]})
        result = v.validate_df("nonexistent_xyz", df)
        assert len(result.checks) == 1
        assert result.checks[0].severity == v.Severity.WARNING
        assert result.passed  # schema-missing is a warning, not a pipeline failure


# ── validate_table and validate_all ───────────────────────────────────────────

class TestValidateTable:
    def test_unknown_table_is_error(self):
        result = v.validate_table("nonexistent_xyz")
        assert not result.passed
        assert any(c.severity == v.Severity.ERROR for c in result.checks)

    def test_table_with_no_files_returns_warning(self):
        result = v.validate_table("cot")
        assert isinstance(result, v.ValidationResult)
        # Either a WARNING (no files) or a PASS/FAIL if data happens to exist
        # — should never raise regardless
        assert all(isinstance(c, v.CheckResult) for c in result.checks)


class TestValidateAll:
    def test_returns_dataframe(self):
        summary = v.validate_all()
        assert isinstance(summary, pd.DataFrame)

    def test_has_expected_columns(self):
        summary = v.validate_all()
        for col in ("table", "status", "errors", "warnings", "rows", "latest_file"):
            assert col in summary.columns, f"Missing column: {col}"

    def test_covers_all_catalog_tables(self):
        summary = v.validate_all()
        in_summary = set(summary["table"])
        for table in q.CATALOG:
            assert table in in_summary, f"{table} missing from validate_all() output"

    def test_status_values_are_valid(self):
        summary = v.validate_all()
        valid = {"PASS", "FAIL", "NO DATA"}
        bad = set(summary["status"]) - valid
        assert not bad, f"Unexpected status values: {bad}"

    def test_no_data_tables_have_zero_rows(self):
        summary = v.validate_all()
        no_data = summary[summary["status"] == "NO DATA"]
        assert (no_data["rows"] == 0).all()

    def test_no_data_tables_have_empty_latest_file(self):
        summary = v.validate_all()
        no_data = summary[summary["status"] == "NO DATA"]
        assert (no_data["latest_file"] == "").all()


# -- positive_cols check (added 2026-08-29) -----------------------------------

class TestPositiveCols:
    """
    `close > 0` and friends. Exists because value_ranges uses `< lo`, so a
    bound of (0, hi) silently accepts an exact 0 -- and 0 is the common
    corruption in price data.
    """

    def test_flags_zero_and_negative(self):
        df = pd.DataFrame({"close": [10.0, 0.0, -5.0, 3.0]})
        res = v._check_positive(df, {"positive_cols": ["close"]})
        assert len(res) == 1
        assert res[0].severity == v.Severity.WARNING
        assert "1 zero" in res[0].message
        assert "1 negative" in res[0].message

    def test_value_ranges_alone_would_miss_the_zero(self):
        # The reason this check exists at all -- guards against someone
        # "simplifying" it back into value_ranges.
        df = pd.DataFrame({"close": [10.0, 0.0]})
        rng = v._check_value_ranges(df, {"value_ranges": {"close": (0, 1000)}})
        assert rng[0].severity == v.Severity.OK,             "value_ranges(0, hi) is expected to accept 0 -- if this now fails, "             "positive_cols may be redundant"
        pos = v._check_positive(df, {"positive_cols": ["close"]})
        assert pos[0].severity == v.Severity.WARNING

    def test_all_positive_passes(self):
        df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
        res = v._check_positive(df, {"positive_cols": ["close"]})
        assert res[0].severity == v.Severity.OK

    def test_nulls_are_ignored(self):
        df = pd.DataFrame({"close": [1.0, None, 2.0]})
        res = v._check_positive(df, {"positive_cols": ["close"]})
        assert res[0].severity == v.Severity.OK

    def test_missing_column_is_skipped_not_failed(self):
        df = pd.DataFrame({"close": [1.0]})
        res = v._check_positive(df, {"positive_cols": ["close", "nonexistent"]})
        assert len(res) == 1

    def test_no_positive_cols_key_is_a_no_op(self):
        assert v._check_positive(pd.DataFrame({"close": [-1.0]}), {}) == []

    def test_equity_price_tables_declare_it(self):
        for table in ("prices", "tiingo_prices", "yfinance_universe_prices",
                      "schwab_intraday", "sector_etfs"):
            assert "close" in v.SCHEMAS[table].get("positive_cols", []),                 f"{table} should require a positive close"

    def test_instruments_that_can_legitimately_go_negative_are_excluded(self):
        # WTI settled at -$37.63 on 2020-04-20 and that print is really in
        # both tables; an option can expire worthless at 0. Flagging these
        # would be a false positive, so they must stay opted out.
        for table in ("futures", "market_history", "options_history"):
            assert "positive_cols" not in v.SCHEMAS.get(table, {}),                 f"{table} can legitimately hold non-positive prices"

    def test_positive_cols_are_declared_columns(self):
        for table, schema in v.SCHEMAS.items():
            for col in schema.get("positive_cols", []):
                assert col in schema["required"],                     f"{table}: positive_cols has '{col}' which is not in required"

