#!/usr/bin/env python3
"""
Data Validation Layer — checks Parquet outputs for schema correctness,
null rates, date sanity, row count plausibility, and value ranges.

Usage
-----
    # Full system health check (all tables with data on disk):
    python validate.py

    # Single table:
    python validate.py --table prices

    # Show all tables including those with no data yet:
    python validate.py --all

    # From inside a pipeline, right before writing:
    from validate import validate_df
    result = validate_df("prices", df)
    if not result.passed:
        print(result)
    df.to_parquet(path, compression="snappy")

    # Programmatic full check:
    from validate import validate_all
    summary = validate_all()
    print(summary[summary["status"] == "FAIL"])
"""

import argparse
import datetime
import glob as _glob_mod
import os
import sys
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)
import query as q


# ── Severity ──────────────────────────────────────────────────────────────────

class Severity(Enum):
    OK      = "OK"
    WARNING = "WARN"
    ERROR   = "ERROR"


# ── Per-check result ──────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name:     str
    severity: Severity
    message:  str

    @property
    def passed(self) -> bool:
        return self.severity != Severity.ERROR

    def __str__(self) -> str:
        icon = {"OK": "+", "WARN": "!", "ERROR": "X"}[self.severity.value]
        return f"  [{self.severity.value:5s}] {icon} {self.name}: {self.message}"


# ── Aggregate result for one table ────────────────────────────────────────────

@dataclass
class ValidationResult:
    table:  str
    checks: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def errors(self) -> list:
        return [c for c in self.checks if c.severity == Severity.ERROR]

    @property
    def warnings(self) -> list:
        return [c for c in self.checks if c.severity == Severity.WARNING]

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        e, w = len(self.errors), len(self.warnings)
        lines = [f"\n{'='*60}", f"  {status}  {self.table}  -- {e} error(s), {w} warning(s)", "=" * 60]
        lines += [str(c) for c in self.checks]
        return "\n".join(lines)


# ── Schema registry ───────────────────────────────────────────────────────────
# required      — columns that MUST be present                  → ERROR if missing
# critical_nn   — subset that MUST NOT be >50% null            → ERROR if mostly null
# date_col      — column for future-date check (None = skip)
# value_ranges  — {col: (lo, hi)}                              → WARN if violated

SCHEMAS: dict[str, dict] = {
    "prices": {
        "required":    ["symbol", "date", "open", "high", "low", "close", "volume"],
        "critical_nn": ["symbol", "date", "close"],
        "date_col":    "date",
    },
    "options_metrics": {
        "required":    ["symbol", "date"],
        "critical_nn": ["symbol", "date"],
        "date_col":    "date",
    },
    "options_chain": {
        "required":    ["symbol", "date"],
        "critical_nn": ["symbol", "date"],
        "date_col":    "date",
    },
    "options_history": {
        "required":    ["symbol", "date"],
        "critical_nn": ["symbol", "date"],
        "date_col":    "date",
    },
    "synthetic_options": {
        "required":    ["symbol", "date", "strike_price", "expiration_date", "bsm_price"],
        "critical_nn": ["symbol", "date", "bsm_price"],
        "date_col":    "date",
    },
    "fundamentals_annual": {
        "required":    ["symbol", "metric", "value", "period"],
        "critical_nn": ["symbol", "metric"],
        "date_col":    "period",
    },
    "fundamentals_quarterly": {
        "required":    ["symbol", "metric", "value", "period"],
        "critical_nn": ["symbol", "metric"],
        "date_col":    "period",
    },
    "commodities": {
        "required":    ["series_id", "date", "value"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    "macro": {
        "required":    ["series_id", "date", "value"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    "gas_spot": {
        "required":    ["series_id", "date", "value"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    "gas_retail": {
        "required":    ["series_id", "date", "value"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    "futures": {
        "required":    ["symbol", "date", "open", "high", "low", "close"],
        "critical_nn": ["symbol", "date", "close"],
        "date_col":    "date",
    },
    "cot": {
        "required":    ["date"],
        "critical_nn": ["date"],
        "date_col":    "date",
    },
    "earnings_calendar": {
        "required":    ["symbol", "date"],
        "critical_nn": ["symbol", "date"],
        "date_col":    "date",
    },
    "insider_transactions": {
        "required":    ["symbol", "date"],
        "critical_nn": ["symbol", "date"],
        "date_col":    "date",
    },
    "sector_etfs": {
        "required":    ["symbol", "date", "open", "high", "low", "close", "volume", "sector"],
        "critical_nn": ["symbol", "date", "close"],
        "date_col":    "date",
    },
    "short_interest": {
        "required":    ["symbol", "shares_short", "short_pct_float"],
        "critical_nn": ["symbol"],
        "date_col":    None,
    },
    "finra_short_interest": {
        "required":    ["symbol", "settlement_date", "shares_short"],
        "critical_nn": ["symbol", "settlement_date", "shares_short"],
        "date_col":    "settlement_date",
    },
    "sec_ftd": {
        "required":    ["symbol", "settlement_date", "shares_failed"],
        "critical_nn": ["symbol", "settlement_date", "shares_failed"],
        "date_col":    "settlement_date",
    },
    "schwab_quotes": {
        "required":    ["symbol", "last", "bid", "ask"],
        "critical_nn": ["symbol", "last"],
        "date_col":    None,
    },
    "schwab_options": {
        "required":    ["symbol", "put_call", "expiration_date", "strike"],
        "critical_nn": ["symbol", "expiration_date", "strike"],
        "date_col":    "expiration_date",
    },
    "news_sentiment": {
        "required":    ["symbol", "sentiment", "score"],
        "critical_nn": ["symbol", "sentiment", "score"],
        "date_col":    "date",
        "value_ranges": {"score": (-1.0, 1.0), "confidence": (0.0, 1.0)},
    },
    "dividends": {
        "required":    ["symbol", "ex_date", "amount"],
        "critical_nn": ["symbol", "ex_date"],
        "date_col":    "ex_date",
    },
    "finnhub_profile":         {"required": ["symbol", "fetched_at"], "critical_nn": ["symbol"], "date_col": None},
    "finnhub_quotes":          {"required": ["symbol", "fetched_at"], "critical_nn": ["symbol"], "date_col": None},
    "finnhub_metrics":         {"required": ["symbol", "fetched_at"], "critical_nn": ["symbol"], "date_col": None},
    "finnhub_recommendations": {"required": ["symbol", "fetched_at"], "critical_nn": ["symbol"], "date_col": None},
    "finnhub_price_targets":   {"required": ["symbol", "fetched_at"], "critical_nn": ["symbol"], "date_col": None},
    "finnhub_upgrades":        {"required": ["symbol", "fetched_at"], "critical_nn": ["symbol"], "date_col": None},
    "finnhub_news":            {"required": ["symbol", "fetched_at"], "critical_nn": ["symbol"], "date_col": None},
    # ── BLS labor market ─────────────────────────────────────────────────────
    "bls_cpi": {
        "required":    ["series_id", "date", "value"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    "bls_ppi": {
        "required":    ["series_id", "date", "value"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    "bls_employment": {
        "required":    ["series_id", "date", "value"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    "bls_jolts": {
        "required":    ["series_id", "date", "value"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    "bls_unemployment": {
        "required":    ["series_id", "date", "value"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    # ── US Treasury fiscal data ──────────────────────────────────────────────
    "treasury_debt": {
        "required":    ["date", "fetched_at"],
        "critical_nn": ["date"],
        "date_col":    "date",
    },
    "treasury_auctions": {
        "required":    ["date", "fetched_at"],
        "critical_nn": ["date"],
        "date_col":    "date",
    },
    # ── World Bank global macro ──────────────────────────────────────────────
    "world_bank": {
        "required":    ["country_code", "indicator", "date", "value"],
        "critical_nn": ["country_code", "indicator", "date", "value"],
        "date_col":    "date",
    },
    # ── SimFin financial statements ──────────────────────────────────────────
    "simfin_income": {
        "required":    ["symbol", "period_type", "fetched_at"],
        "critical_nn": ["symbol", "period_type"],
        "date_col":    None,
    },
    "simfin_balance": {
        "required":    ["symbol", "period_type", "fetched_at"],
        "critical_nn": ["symbol", "period_type"],
        "date_col":    None,
    },
    "simfin_cashflow": {
        "required":    ["symbol", "period_type", "fetched_at"],
        "critical_nn": ["symbol", "period_type"],
        "date_col":    None,
    },
    # ── Tiingo ───────────────────────────────────────────────────────────────
    "tiingo_prices": {
        "required":    ["symbol", "date", "open", "high", "low", "close", "volume"],
        "critical_nn": ["symbol", "date", "close"],
        "date_col":    "date",
    },
    "tiingo_news": {
        "required":    ["article_id", "date", "title"],
        "critical_nn": ["article_id", "date"],
        "date_col":    "date",
    },
    # ── Alpha Vantage ────────────────────────────────────────────────────────
    "alpha_vantage_technical": {
        "required":    ["symbol", "date", "indicator"],
        "critical_nn": ["symbol", "date", "indicator"],
        "date_col":    "date",
    },
    "alpha_vantage_forex": {
        "required":    ["pair", "date", "open", "high", "low", "close"],
        "critical_nn": ["pair", "date", "close"],
        "date_col":    "date",
    },
    # ── Institutional holdings ───────────────────────────────────────────────
    "institutional_holdings": {
        "required":    ["institution", "filed_date", "company_name", "value_usd"],
        "critical_nn": ["institution", "filed_date"],
        "date_col":    "filed_date",
    },
    # ── IPO calendar ─────────────────────────────────────────────────────────
    "ipo_calendar": {
        "required":    ["date", "fetched_at"],
        "critical_nn": ["date"],
        "date_col":    "date",
        "value_ranges": {"price_range_low": (0, 10000), "price_range_high": (0, 10000)},
    },
}


# ── Individual check functions ─────────────────────────────────────────────────

def _check_not_empty(df: pd.DataFrame) -> CheckResult:
    if len(df) == 0:
        return CheckResult("not_empty", Severity.ERROR, "DataFrame has 0 rows")
    return CheckResult("not_empty", Severity.OK, f"{len(df):,} rows")


def _check_required_cols(df: pd.DataFrame, schema: dict) -> CheckResult:
    required = schema.get("required", [])
    missing = [c for c in required if c not in df.columns]
    if missing:
        return CheckResult("required_cols", Severity.ERROR, f"Missing columns: {missing}")
    return CheckResult("required_cols", Severity.OK, f"All {len(required)} required columns present")


def _check_null_rates(df: pd.DataFrame, schema: dict) -> list:
    results = []
    for col in schema.get("critical_nn", []):
        if col not in df.columns:
            continue
        null_pct = df[col].isna().mean()
        if null_pct > 0.5:
            results.append(CheckResult(
                f"nulls:{col}", Severity.ERROR,
                f"{col} is {null_pct:.0%} null (critical column)"
            ))
        elif null_pct > 0.05:
            results.append(CheckResult(
                f"nulls:{col}", Severity.WARNING,
                f"{col} has {null_pct:.1%} nulls"
            ))
        else:
            results.append(CheckResult(f"nulls:{col}", Severity.OK, f"{col}: {null_pct:.1%} null"))
    return results


def _check_future_dates(df: pd.DataFrame, schema: dict) -> CheckResult:
    date_col = schema.get("date_col")
    if not date_col or date_col not in df.columns:
        return CheckResult("future_dates", Severity.OK, "no date column to check")
    today = datetime.date.today()
    try:
        dates = pd.to_datetime(df[date_col], errors="coerce")
        future = int((dates.dt.date > today).sum())
        if future > 0:
            pct = future / len(df)
            return CheckResult(
                "future_dates", Severity.WARNING,
                f"{future} rows ({pct:.1%}) have {date_col} > today"
            )
    except Exception:
        pass
    return CheckResult("future_dates", Severity.OK, f"{date_col}: no future dates")


def _check_value_ranges(df: pd.DataFrame, schema: dict) -> list:
    results = []
    for col, (lo, hi) in schema.get("value_ranges", {}).items():
        if col not in df.columns:
            continue
        numeric = pd.to_numeric(df[col], errors="coerce")
        out_of_range = int(((numeric < lo) | (numeric > hi)).sum())
        if out_of_range > 0:
            results.append(CheckResult(
                f"range:{col}", Severity.WARNING,
                f"{out_of_range} values outside [{lo}, {hi}]"
            ))
        else:
            results.append(CheckResult(f"range:{col}", Severity.OK, f"all values in [{lo}, {hi}]"))
    return results


def _check_row_count(table: str, df: pd.DataFrame) -> CheckResult:
    """Warn if the new DataFrame is less than 50% the size of the most recent snapshot."""
    glob_path = q.CATALOG.get(table, "")
    existing = sorted(_glob_mod.glob(glob_path.replace("/", os.sep), recursive=True))
    if not existing:
        return CheckResult("row_count", Severity.OK, f"{len(df):,} rows (no prior snapshot to compare)")
    try:
        prev = pd.read_parquet(existing[-1])
        prev_n = len(prev)
        new_n  = len(df)
        if prev_n == 0:
            return CheckResult("row_count", Severity.OK, f"{new_n:,} rows (prior snapshot was empty)")
        ratio = new_n / prev_n
        if ratio < 0.5:
            return CheckResult(
                "row_count", Severity.WARNING,
                f"{new_n:,} rows — {ratio:.0%} of prior snapshot ({prev_n:,}) — possible data loss"
            )
        return CheckResult(
            "row_count", Severity.OK,
            f"{new_n:,} rows ({ratio:.0%} vs prior {prev_n:,})"
        )
    except Exception as exc:
        return CheckResult("row_count", Severity.WARNING, f"{len(df):,} rows (prior load failed: {exc})")


def _check_fetched_at(df: pd.DataFrame, max_age_hours: float = 2.0) -> CheckResult:
    if "fetched_at" not in df.columns:
        return CheckResult("fetched_at", Severity.OK, "no fetched_at column")
    try:
        ts = pd.to_datetime(df["fetched_at"], utc=True, errors="coerce").max()
        if pd.isna(ts):
            return CheckResult("fetched_at", Severity.WARNING, "fetched_at is all NaT")
        now = datetime.datetime.now(datetime.timezone.utc)
        age_h = (now - ts).total_seconds() / 3600
        if age_h > max_age_hours:
            return CheckResult(
                "fetched_at", Severity.WARNING,
                f"newest fetched_at is {age_h:.1f}h ago (threshold {max_age_hours}h)"
            )
        return CheckResult("fetched_at", Severity.OK, f"newest fetched_at is {age_h:.1f}h ago")
    except Exception as exc:
        return CheckResult("fetched_at", Severity.WARNING, f"fetched_at parse error: {exc}")


# ── Public API ─────────────────────────────────────────────────────────────────

def validate_df(
    table: str,
    df: pd.DataFrame,
    check_freshness: bool = True,
    max_age_hours: float = 2.0,
) -> ValidationResult:
    """
    Validate a freshly-fetched DataFrame before writing to Parquet.

    Call inside a pipeline right before df.to_parquet(...):

        result = validate_df("prices", df)
        if not result.passed:
            print(result)
        df.to_parquet(path, compression="snappy")

    Parameters
    ----------
    table           : CATALOG table name
    df              : DataFrame to validate
    check_freshness : warn when fetched_at is older than max_age_hours
    max_age_hours   : freshness threshold in hours (default 2)
    """
    if table not in SCHEMAS:
        return ValidationResult(table, [
            CheckResult("schema", Severity.WARNING, f"No schema defined for '{table}' — skipping validation")
        ])
    schema = SCHEMAS[table]
    checks = []
    checks.append(_check_not_empty(df))
    checks.append(_check_required_cols(df, schema))
    checks.extend(_check_null_rates(df, schema))
    checks.append(_check_future_dates(df, schema))
    checks.extend(_check_value_ranges(df, schema))
    checks.append(_check_row_count(table, df))
    if check_freshness:
        checks.append(_check_fetched_at(df, max_age_hours))
    return ValidationResult(table, checks)


def validate_table(table: str) -> ValidationResult:
    """
    Load the latest snapshot of a table from disk and validate it.

    Skips the freshness check (historical files are expected to be old).
    Returns a warning result if no files exist yet.
    """
    if table not in q.CATALOG:
        return ValidationResult(table, [
            CheckResult("catalog", Severity.ERROR, f"'{table}' not in CATALOG")
        ])
    glob_path = q.CATALOG[table]
    files = sorted(_glob_mod.glob(glob_path.replace("/", os.sep), recursive=True))
    if not files:
        return ValidationResult(table, [
            CheckResult("files", Severity.WARNING, "No parquet files on disk yet")
        ])
    try:
        df = pd.read_parquet(files[-1])
    except Exception as exc:
        return ValidationResult(table, [
            CheckResult("read", Severity.ERROR, f"Failed to read {os.path.basename(files[-1])}: {exc}")
        ])
    return validate_df(table, df, check_freshness=False)


def validate_all() -> pd.DataFrame:
    """
    Run validate_table() on every CATALOG entry and return a summary DataFrame.

    Columns: table | status | errors | warnings | rows | latest_file
    Status values: PASS, FAIL, NO DATA
    """
    rows = []
    for table in sorted(q.CATALOG):
        glob_path = q.CATALOG[table]
        files = sorted(_glob_mod.glob(glob_path.replace("/", os.sep), recursive=True))
        if not files:
            rows.append({
                "table": table, "status": "NO DATA",
                "errors": 0, "warnings": 0, "rows": 0, "latest_file": "",
            })
            continue
        result = validate_table(table)
        try:
            n_rows = len(pd.read_parquet(files[-1]))
        except Exception:
            n_rows = -1
        rows.append({
            "table":       table,
            "status":      "PASS" if result.passed else "FAIL",
            "errors":      len(result.errors),
            "warnings":    len(result.warnings),
            "rows":        n_rows,
            "latest_file": os.path.basename(files[-1]),
        })
    return pd.DataFrame(rows)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate financial pipeline Parquet outputs")
    parser.add_argument("--table", help="Validate a single table by name")
    parser.add_argument("--all",   action="store_true", help="Include tables with no data")
    args = parser.parse_args()

    if args.table:
        res = validate_table(args.table)
        print(res)
        sys.exit(0 if res.passed else 1)

    summary = validate_all()
    print("\n=== Pipeline Validation Report ===\n")
    visible = summary if args.all else summary[summary["status"] != "NO DATA"]
    if visible.empty:
        print("No tables with data on disk. Run a pipeline first.")
    else:
        print(visible.to_string(index=False))

    fail_count   = int((summary["status"] == "FAIL").sum())
    nodata_count = int((summary["status"] == "NO DATA").sum())
    pass_count   = int((summary["status"] == "PASS").sum())
    print(f"\nSummary: {pass_count} PASS  |  {fail_count} FAIL  |  {nodata_count} NO DATA")
    sys.exit(0 if fail_count == 0 else 1)
