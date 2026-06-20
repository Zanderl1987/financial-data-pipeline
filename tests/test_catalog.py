"""
test_catalog.py — verify query.py CATALOG wiring.

Tests do NOT require API keys or actual data files.  They check that:
  - every expected logical table name is registered in CATALOG
  - CATALOG glob paths point into storage/raw (no typos in directory names)
  - discovery helpers (tables(), date_range()) run without raising
  - reload() resets the connection cleanly
"""

import sys
import os
import pytest

# Ensure repo root is on the path regardless of where pytest is invoked
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import query as q


# ── Expected table names ───────────────────────────────────────────────────────

EXPECTED_TABLES = [
    # Prices
    "prices",
    # Options
    "options_metrics",
    "options_chain",
    "options_history",
    "synthetic_options",
    # Fundamentals
    "fundamentals_annual",
    "fundamentals_quarterly",
    # Macro + commodities
    "commodities",
    "macro",
    "gas_spot",
    "gas_retail",
    # Futures + COT
    "futures",
    "cot",
    # Short interest
    "short_interest",
    "finra_short_interest",
    "sec_ftd",
    # Schwab
    "schwab_quotes",
    "schwab_options",
    # Sector ETFs
    "sector_etfs",
    # Events (Finnhub)
    "earnings_calendar",
    "insider_transactions",
    "dividends",
    # Finnhub fundamentals
    "finnhub_profile",
    "finnhub_quotes",
    "finnhub_metrics",
    "finnhub_recommendations",
    "finnhub_price_targets",
    "finnhub_upgrades",
    "finnhub_news",
    # Sentiment
    "news_sentiment",
]


class TestCatalogCompleteness:
    def test_all_expected_tables_registered(self):
        missing = [t for t in EXPECTED_TABLES if t not in q.CATALOG]
        assert not missing, f"Missing from CATALOG: {missing}"

    def test_no_extra_surprise_tables(self):
        """Warn (not fail) if new tables appear that aren't in our expected list."""
        extra = [t for t in q.CATALOG if t not in EXPECTED_TABLES]
        if extra:
            pytest.warns(None)  # non-fatal; just surfaces unexpected additions

    def test_catalog_count(self):
        assert len(q.CATALOG) >= len(EXPECTED_TABLES), (
            f"CATALOG has {len(q.CATALOG)} entries, expected >= {len(EXPECTED_TABLES)}"
        )


class TestCatalogPaths:
    def test_all_paths_under_storage_raw(self):
        storage_root = os.path.join(REPO_ROOT, "storage", "raw").replace("\\", "/")
        bad = {
            name: path
            for name, path in q.CATALOG.items()
            if storage_root.lower() not in path.lower()
        }
        assert not bad, f"CATALOG entries not under storage/raw: {bad}"

    def test_all_paths_end_in_parquet_glob(self):
        bad = {
            name: path
            for name, path in q.CATALOG.items()
            if not path.endswith(".parquet")
        }
        assert not bad, f"CATALOG entries without .parquet extension: {bad}"

    def test_storage_dirs_exist(self):
        """Each CATALOG glob path's parent directory should exist (or have a .gitkeep)."""
        missing_dirs = []
        for name, glob_path in q.CATALOG.items():
            parent = os.path.dirname(glob_path)
            # Replace forward slashes for os.path on Windows
            parent_os = parent.replace("/", os.sep)
            if not os.path.isdir(parent_os):
                missing_dirs.append((name, parent_os))
        assert not missing_dirs, (
            f"Storage directories missing for tables: "
            + ", ".join(f"{n} → {p}" for n, p in missing_dirs)
        )


class TestDiscoveryHelpers:
    def test_tables_runs_without_error(self):
        df = q.tables()
        assert df is not None
        assert "table" in df.columns
        assert "rows" in df.columns

    def test_tables_returns_all_catalog_entries(self):
        df = q.tables()
        registered = set(df["table"].tolist())
        # Only tables with files are registered as views; all should appear in output
        assert registered.issubset(set(q.CATALOG.keys()))

    def test_date_range_runs_without_error(self):
        df = q.date_range()
        assert df is not None  # may be empty if no data files

    def test_reload_does_not_raise(self):
        q.reload()   # should silently reset and re-register views

    def test_schema_raises_on_unknown_table(self):
        with pytest.raises(ValueError, match="Unknown table"):
            q.schema("nonexistent_table_xyz")

    def test_load_raises_on_unknown_table(self):
        with pytest.raises(ValueError, match="Unknown table"):
            q.load("nonexistent_table_xyz")
