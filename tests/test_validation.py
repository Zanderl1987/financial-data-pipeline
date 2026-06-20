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
