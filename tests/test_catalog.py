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
    # Fundamentals — SEC EDGAR
    "fundamentals_annual",
    "fundamentals_quarterly",
    # SimFin financial statements
    "simfin_income",
    "simfin_balance",
    "simfin_cashflow",
    # Macro + commodities (FRED)
    "commodities",
    "macro",
    "gas_spot",
    "gas_retail",
    # BLS labor market
    "bls_cpi",
    "bls_ppi",
    "bls_employment",
    "bls_jolts",
    "bls_unemployment",
    # US Treasury fiscal data
    "treasury_debt",
    "treasury_auctions",
    # World Bank global macro
    "world_bank",
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
    # Tiingo prices + news
    "tiingo_prices",
    "tiingo_news",
    # Alpha Vantage
    "alpha_vantage_technical",
    "alpha_vantage_forex",
    # Institutional holdings (13F)
    "institutional_holdings",
    # Events (Finnhub)
    "earnings_calendar",
    "insider_transactions",
    "ipo_calendar",
    "dividends",
    # Finnhub fundamentals
    "finnhub_profile",
    "finnhub_quotes",
    "finnhub_metrics",
    "finnhub_recommendations",
    "finnhub_price_targets",
    "finnhub_upgrades",
    "finnhub_news",
    # Yahoo Finance deep market history
    "market_history",
    # TradingView technical-rating snapshots
    "tv_ratings",
    # SEC EDGAR filing index
    "sec_filings",
    # Schwab intraday, movers, portfolio mirror
    "schwab_intraday",
    "schwab_movers",
    "schwab_positions",
    "schwab_transactions",
    # Sentiment
    "news_sentiment",
    # IMF, metals, commodities expansions
    "imf_commodities",
    "metals_spot",
    # FAO global food & agriculture
    "fao_production",
    "fao_prices",
    # World Bank Pink Sheet
    "wb_commodities",
    # NOAA climate
    "noaa_climate",
    # USDA NASS
    "usda_crops",
    "usda_fertilizers",
    # US Census trade
    "us_imports_hs",
    "us_exports_hs",
    # EIA energy
    "eia_petroleum_stocks",
    "eia_natgas_storage",
    "eia_crude_production",
    # CoinGecko cryptocurrency
    "crypto_market",
    "crypto_history",
    # Forex rates
    "forex_rates",
    # BEA national accounts
    "bea_gdp",
    "bea_income",
    "bea_profits",
    # OECD macro
    "oecd_macro",
    # Congressional trade disclosures
    "congressional_trades",
    # USPTO patents
    "patents",
    # ECB rates
    "ecb_rates",
    # USGS critical minerals
    "usgs_minerals",
    # UN Comtrade trade flows
    "comtrade_trade",
    # Fama-French factor returns + industry portfolios
    "ff_factors",
    "ff_industry",
    # Shiller long-run CAPE valuation
    "shiller_cape",
    # CBOE volatility indices
    "cboe_volatility",
    # FDIC banking data
    "fdic_institutions",
    "fdic_financials",
    "fdic_failures",
    # Fear & Greed Index
    "fear_greed",
    # Nasdaq Data Link
    "market_valuation",
    "treasury_yield_curve",
    # NY Fed SOMA balance sheet
    "fed_soma",
    # Fed sentiment (RSS speeches/statements + Claude hawkish/dovish)
    "fed_speeches",
    "fed_sentiment",
    # Real estate (FHFA HPI + Zillow ZHVI/ZORI)
    "fhfa_hpi",
    "zillow_zhvi",
    "zillow_zori",
    # Shipping / logistics (NY Fed GSCPI + FRED freight PPI)
    "shipping_gscpi",
    "shipping_freight_ppi",
    # Signal health monitor (maintained backtest performance tracking)
    "signal_health",
    # EIA refinery + crude trade (oil/transportation depth batch)
    "eia_refinery_activity",
    "eia_crude_trade",
    # TSA checkpoint travel volumes
    "tsa_checkpoint",
    # Open-Meteo weather
    "open_meteo_weather",
    # Wikipedia pageviews
    "wikipedia_pageviews",
    # openFDA drug approvals + recalls
    "openfda_approvals",
    "openfda_recalls",
    # Treasury TIC foreign holdings
    "treasury_tic_holders",
    "treasury_tic_slt",
    # Google Trends
    "google_trends_economic",
    "google_trends_market",
    "google_trends_sector",
    # Reddit finance posts + ticker mentions
    "reddit_posts",
    "reddit_mentions",
    # AIS vessel positions
    "ais_positions",
    "ais_zone_summary",
    # StockAnalysis.com scrapes
    "sa_movers",
    "sa_ipos",
    "sa_ipo_calendar",
    "sa_ipo_stats",
    "sa_corporate_actions",
    "sa_stock_list",
    "sa_etf_list",
    "sa_income",
    "sa_balance",
    "sa_cashflow",
    "sa_ratios",
    # Finviz scrapes
    "finviz_movers",
    "finviz_screener",
    "finviz_financials",
    "finviz_insider",
    "finviz_sector_perf",
    "finviz_industry_perf",
    "finviz_country_perf",
    "finviz_group_valuation",
]


class TestCatalogCompleteness:
    def test_all_expected_tables_registered(self):
        missing = [t for t in EXPECTED_TABLES if t not in q.CATALOG]
        assert not missing, f"Missing from CATALOG: {missing}"

    def test_no_extra_surprise_tables(self):
        """Fail if new tables appear that aren't in our expected list.

        Keeping EXPECTED_TABLES current is part of the new-pipeline wiring
        checklist (CLAUDE.md); this guard is what catches a skipped step.
        (The old ``pytest.warns(None)`` no-op became a TypeError on pytest 8,
        which is how 35 unlisted tables went unnoticed until 2026-07-12.)
        """
        extra = [t for t in q.CATALOG if t not in EXPECTED_TABLES]
        assert not extra, f"CATALOG tables missing from EXPECTED_TABLES: {extra}"

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

    def test_no_glob_collisions(self):
        """No two tables may share one glob — colliding globs union mismatched
        schemas (e.g. reddit_posts and reddit_mentions) into both views."""
        seen: dict[str, str] = {}
        collisions = []
        for name, path in q.CATALOG.items():
            if path in seen:
                collisions.append((seen[path], name, path))
            seen[path] = name
        assert not collisions, f"CATALOG glob collisions: {collisions}"

    def test_storage_dirs_exist(self):
        """Each CATALOG glob path's base (non-wildcard) directory should exist."""
        missing_dirs = []
        for name, glob_path in q.CATALOG.items():
            # Strip wildcard segments — base dir is everything before the first *
            normalized = glob_path.replace("/", os.sep)
            base = normalized.split("*")[0].rstrip(os.sep)
            if not os.path.isdir(base):
                missing_dirs.append((name, base))
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
