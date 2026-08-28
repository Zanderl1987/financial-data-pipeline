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
    "bls_avg_price",
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
    # Yahoo Finance Russell 3000 universe (split-adjusted equity OHLCV)
    "yfinance_universe_prices",
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
    # Plastics production (OWID)
    "plastics_production",
    # CFPB consumer complaints
    "cfpb_complaints",
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
    "eia_hourly_grid",
    "index_members",
    "securities",
    "fund_holdings",
    "etf_holdings",
    "identifier_map",
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
    # USGS MCS helium (+ rare gases) + DS-140 historical statistics
    "usgs_mcs_helium",
    "usgs_ds140_helium",
    # Omkar Cloud commodity spot prices
    # Unwired 2026-07-26: requires OMKAR_API_KEY, never set, pipeline never run.
    # "omkar_commodity",
    # UN Comtrade trade flows
    "comtrade_trade",
    # GEM tracker summary tables
    "gem_coal_summary",
    "gem_coal_mine_summary",
    "gem_steel_summary",
    "gem_cement_summary",
    "gem_oilgas_summary",
    "gem_lng_summary",
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
    # Unwired 2026-07-26: requires ANTHROPIC_API_KEY, never set, pipeline never run.
    # "fed_speeches",
    # "fed_sentiment",
    # Real estate (FHFA HPI + Zillow ZHVI/ZORI)
    "fhfa_hpi",
    "zillow_zhvi",
    "zillow_zori",
    # Shipping / logistics (NY Fed GSCPI + FRED freight PPI)
    "shipping_gscpi",
    "shipping_freight_ppi",
    # Piracy incidents (ICC IMB live-map archive + Wikipedia Somali hijacking log)
    "piracy_incidents",
    "somali_hijackings",
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
    "fred_macro_housing",
    "fred_macro_sentiment",
    "fred_macro_industrial",
    "fred_macro_consumer",
    "fred_macro_trade",
    "fred_rates_gdp_interest_rates",
    "fred_rates_gdp_money_supply",
    "fred_rates_gdp_gdp",
    "fred_rates_gdp_inflation",
    "fred_rates_gdp_mortgage",
    "fred_rates_gdp_commodities",
    "fred_rates_gdp_exchange_rates",
    "fred_rates_gdp_markets",
    "fred_rates_gdp_federal_debt",
    "fred_rates_gdp_labor",
    "alpha_vantage_overview",
    "alpha_vantage_income_statement",
    "alpha_vantage_balance_sheet",
    "alpha_vantage_cash_flow",
    "alpha_vantage_earnings",
    "alpha_vantage_earnings_calendar",
    "alpha_vantage_dividends",
    "alpha_vantage_insider_transactions",
    "alpha_vantage_news_sentiment",
    "alpha_vantage_top_gainers_losers",
    "coingecko_global_market",
    "coingecko_coins_markets",
    "coingecko_trending",
    "coingecko_categories",
    "coingecko_derivatives",
    "coingecko_exchange_rates",
    "sec_edgar_submissions",
    "sec_edgar_xbrl_fundamentals",
    "sec_edgar_efts_search",
    "bls_import_export_prices",
    "bls_eci",
    "bls_productivity",
    "bls_oes",
    "bls_qcew",
    "bls_ecec",
    "bls_cps_demographics",
    "eia_electricity_generation",
    "eia_electricity_sales",
    "eia_nuclear_outages",
    "eia_coal_production",
    "eia_coal_trade",
    "eia_international",
    "eia_seds",
    "eia_petroleum_spot_prices",
    "eia_petroleum_futures",
    "eia_refiner_margins",
    "eia_petroleum_supply_demand",
    "eia_natural_gas_consumption",
    "eia_natural_gas_prices",
    "eia_natural_gas_production",
    "eia_lng_flows",
    "finnhub_esg",
    # Unwired 2026-08-28: free-tier 403, superseded by congressional_trades
    # "finnhub_congressional_trading",
    "finnhub_supply_chain",
    "finnhub_insider_sentiment",
    "finnhub_social_sentiment",
    "finnhub_sec_filings",
    "finnhub_earnings_quality",
    "finnhub_lobbying",
    "finnhub_usa_spending",
    "finnhub_uspto_patents",
    "finnhub_visa_applications",
    "finnhub_economic_calendar",
    "usaspending_award_counts",
    "usaspending_top_awards",
    "lda_lobbying_filings",
    "defillama_protocols",
    "defillama_fees",
    "defillama_stablecoins",
    "finnhub_earnings_history",
    "finnhub_eps_estimates",
    "finnhub_revenue_estimates",
    "finnhub_ownership",
    "finnhub_splits",
    "finnhub_peers",
    "finnhub_executives",
    "finnhub_filing_sentiment",
    "finnhub_transcripts",
    "finnhub_company_news_sentiment",
    "tiingo_corporate_actions_dividends",
    "tiingo_corporate_actions_splits",
    "tiingo_corporate_actions_yield",
    "tiingo_fundamentals_daily",
    "tiingo_fundamentals_statements",
    "treasury_debt_to_penny",
    "treasury_avg_interest_rates",
    "treasury_interest_expense",
    "treasury_auctions_detail",
    "treasury_exchange_rates",
    "treasury_savings_bonds",
    "treasury_mts_receipts_outlays",
    "treasury_mts_outlays_by_agency",
    "treasury_dts_operating_cash",
    "treasury_mts_budget_comparison",
    "dark_pool_volume",
    "retail_sentiment",
    "retail_sentiment_daily",
    "insider_sentiment",
    "indeed_job_postings_national",
    "indeed_job_postings_sector",
    "indeed_job_postings_state",
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
        iceberg_root = os.path.join(REPO_ROOT, "storage", "iceberg").replace("\\", "/")
        bad = {
            name: path
            for name, path in q.CATALOG.items()
            if storage_root.lower() not in path.lower()
            and iceberg_root.lower() not in path.lower()
        }
        assert not bad, f"CATALOG entries not under storage/raw or storage/iceberg: {bad}"

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

    # These CATALOG tables only populate via alpha_vantage_fundamentals_pipeline.py
    # --backfill, which the pipeline's own docstring calls "uncapped -- vastly
    # exceeds daily quota, expect multi-day runtime". Deliberately not run yet
    # (2026-07-26) since it would compete with earnings_sentiment_tool's shared
    # Alpha Vantage per-IP daily budget. Remove from this set once backfilled.
    NOT_YET_BACKFILLED = {
        "alpha_vantage_income_statement",
        "alpha_vantage_balance_sheet",
        "alpha_vantage_cash_flow",
    }

    def test_storage_dirs_exist(self):
        """Each CATALOG glob path's base (non-wildcard) directory should exist."""
        missing_dirs = []
        for name, glob_path in q.CATALOG.items():
            if name in self.NOT_YET_BACKFILLED:
                continue
            # Strip wildcard segments — base dir is everything before the first *
            normalized = glob_path.replace("/", os.sep)
            base = normalized.split("*")[0].rstrip(os.sep)
            if not os.path.isdir(base):
                missing_dirs.append((name, base))
        assert not missing_dirs, (
            f"Storage directories missing for tables: "
            + ", ".join(f"{n} → {p}" for n, p in missing_dirs)
        )


class TestPilotIcebergViews:
    """Pilot tables should be backed by iceberg_scan when an Iceberg mirror
    exists, and fall back to the curated parquet otherwise."""

    def test_pilot_tables_defined(self):
        assert q.PILOT_ICEBERG_TABLES == {
            "prices", "macro", "fundamentals_annual", "fundamentals_quarterly",
            "fao_prices", "fao_production", "plastics_production",
            "usda_crops", "usda_fertilizers", "bls_avg_price",
        }

    def test_iceberg_metadata_wins_over_curated(self, monkeypatch, tmp_path):
        import pandas as pd
        import pyarrow.parquet as pq
        import iceberg_pilot

        # Point the pilot warehouse at a temp dir and build a real mini table.
        monkeypatch.setattr(iceberg_pilot, "ICEBERG_WAREHOUSE", tmp_path)
        monkeypatch.setattr(iceberg_pilot, "PILOT_CATALOG_DB", tmp_path / "pilot_catalog.db")
        df = pd.DataFrame({
            "symbol": ["AAPL", "MSFT"],
            "date": ["2024-01-02", "2024-01-02"],
            "close": [100.0, 50.0],
        })
        pq_path = tmp_path / "prices.parquet"
        df.to_parquet(pq_path, index=False)
        iceberg_pilot.replace_from_parquet("pilot.prices", str(pq_path))

        q.reload()
        con = q._con()
        sql = con.execute(
            "SELECT sql FROM duckdb_views() WHERE view_name='prices'"
        ).fetchone()[0]
        assert "iceberg_scan" in sql
        # and the view reads the mirrored rows through the real catalog
        n = q.sql("SELECT COUNT(*) AS n FROM prices").iloc[0]["n"]
        assert n == 2

    def test_curated_fallback_when_no_iceberg(self, monkeypatch, tmp_path):
        monkeypatch.setattr(q, "_pilot_iceberg_metadata", lambda t: None)
        # point curated root at an empty temp dir so the fallback is the raw glob
        monkeypatch.setattr(q, "_CURATED_ROOT", tmp_path)
        q.reload()
        con = q._con()
        sql = con.execute(
            "SELECT sql FROM duckdb_views() WHERE view_name='prices'"
        ).fetchone()
        # prices' raw glob exists in storage/raw -> view registered via parquet
        assert sql is not None
        assert "iceberg_scan" not in sql[0]


class TestNoOrphanedTables:
    """
    A table registered in CATALOG but not produced by any run_all.py
    PipelineSpec never gets refreshed by a full run. It keeps serving whatever
    was last written by hand, and nothing reports a failure -- validate.py
    still PASSes it, because the data is there, just old.

    That is not hypothetical: on 2026-08-11 six live, healthy sources
    (wb_commodities, imf_commodities, metals_spot, fao_prices, fao_production,
    options_history) were found frozen between 2026-06-17 and 2026-07-02 for
    exactly this reason. All six were reachable and returned fresh data the
    moment they were run by hand. The wiring checklist in CLAUDE.md lists the
    run_all.py registration; nothing enforced it.
    """

    # Tables written by analytics/tooling rather than a source pipeline.
    # Anything added here needs a reason, not just a green suite.
    NOT_PIPELINE_PRODUCED = {
        # written by event_backtest.py as a price cache, not fetched
        "yfinance_universe_prices",
    }

    def test_every_catalog_table_has_a_producing_pipeline(self):
        import run_all

        produced = set()
        for spec in run_all.PIPELINES:
            produced.update(spec.tables or [])

        orphans = set(q.CATALOG) - produced - self.NOT_PIPELINE_PRODUCED
        assert not orphans, (
            "CATALOG tables with no run_all.py PipelineSpec -- a full run will "
            "never refresh them and they will silently go stale: "
            f"{sorted(orphans)}"
        )

    def test_pipeline_specs_only_claim_real_tables(self):
        """The inverse typo: a spec claiming a table that isn't in CATALOG."""
        import run_all

        known = set(q.CATALOG) | set(q.ANALYTICS_VIEWS)
        unknown = {
            (spec.name, t)
            for spec in run_all.PIPELINES
            for t in (spec.tables or [])
            if t not in known
        }
        assert not unknown, f"PipelineSpec.tables entries missing from CATALOG: {sorted(unknown)}"

    def test_every_spec_points_at_a_file_that_exists(self):
        import run_all

        missing = [
            (spec.name, spec.file)
            for spec in run_all.PIPELINES
            if not os.path.exists(os.path.join(REPO_ROOT, spec.file))
        ]
        assert not missing, f"PipelineSpec.file does not exist: {missing}"


class TestScheduledJobSkipLists:
    """
    `run_all.py --skip` silently ignores a name that matches no pipeline, so a
    typo in a scheduled job's skip list is invisible: the job keeps passing and
    the pipeline it meant to exclude quietly runs anyway (burning a metered API
    key, or failing every night against a known-dead source and masking real
    failures underneath). Added 2026-08-11 with scripts/daily_stage1.ps1.
    """

    SCRIPTS = ["scripts/daily_pipelines.ps1"]

    def _skip_names(self, path):
        """Pull the names out of the `$skip = @( ... ) -join ","` block."""
        import re

        with open(path, encoding="utf-8") as f:
            body = f.read()
        m = re.search(r"\$skip\s*=\s*@\((.*?)\)\s*-join", body, re.S)
        assert m, f"{path}: no `$skip = @(...) -join` block found"
        # Strip PowerShell line comments before pulling quoted names, so a name
        # mentioned in an explanatory comment is not treated as a skip entry.
        block = re.sub(r"#[^\n]*", "", m.group(1))
        return re.findall(r'"([^"]+)"', block)

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_skip_entries_name_real_pipelines(self, script):
        import run_all

        names = {p.name for p in run_all.PIPELINES}
        skips = self._skip_names(os.path.join(REPO_ROOT, script))
        assert skips, f"{script}: parsed an empty skip list"
        unknown = [s for s in skips if s not in names]
        assert not unknown, (
            f"{script} skips names that match no PipelineSpec (silent no-op): {unknown}"
        )


class TestDocumentedScriptsAreTracked:
    """
    CLAUDE.md is the first thing a new session reads, so a script path in it is
    an instruction. On 2026-08-11 it was pointed at `scripts\\schwab_local_reauth.py`
    -- a file that existed on this machine but was never `git add`ed, so any
    other clone got "can't open file" while the tracked, tested equivalent sat
    unmentioned. Untracked is the dangerous case precisely because it looks fine
    locally; only git can tell you.
    """

    DOCS = ["CLAUDE.md", "docs/PROJECT_NOTES.md", "docs/AUTOMATION.md"]

    def _tracked_files(self):
        import subprocess

        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if out.returncode != 0:
            pytest.skip("not a git checkout")
        return {line.strip() for line in out.stdout.splitlines() if line.strip()}

    def _body(self, doc):
        path = os.path.join(REPO_ROOT, doc)
        if not os.path.exists(path):
            pytest.skip(f"{doc} absent")
        with open(path, encoding="utf-8") as f:
            return f.read()

    @pytest.mark.parametrize("doc", DOCS)
    def test_referenced_scripts_exist_and_are_tracked(self, doc):
        import re

        body = self._body(doc)
        # Match scripts/<name>.py or scripts\<name>.py however it is quoted.
        referenced = {
            m.replace("\\", "/")
            for m in re.findall(r"scripts[\\/][A-Za-z0-9_./\\-]+\.py", body)
        }
        assert referenced, f"{doc}: no script references found -- regex likely stale"

        tracked = self._tracked_files()
        broken = sorted(r for r in referenced if r not in tracked)
        assert not broken, (
            f"{doc} points at script(s) git does not track: {broken}. "
            f"Either `git add` them or point the doc at the tracked equivalent."
        )

    @pytest.mark.parametrize("doc", DOCS)
    def test_referenced_notes_exist_and_are_tracked(self, doc):
        """
        Same defect, one file type over. "see work-notes/financial-data-pipeline/SESSION_NOTES_<date>.md" is a
        promise that the detail is somewhere retrievable; an untracked notes
        file keeps that promise only on the machine that wrote it. Caught
        2026-08-11 with work-notes/financial-data-pipeline/SESSION_NOTES_2026-08-11.md, which four docs pointed at
        while it existed nowhere but this clone.
        """
        import re

        body = self._body(doc)
        refs = {m.replace("\\", "/") for m in re.findall(r"[A-Za-z0-9_./\\-]+\.md", body)}

        # Only in-repo references are ours to keep. A path whose first segment
        # is a directory this repo does not have is a pointer at a sibling repo
        # (e.g. earnings_sentiment_tool/EXPERT_BRIEF.md) and can never be
        # tracked here -- flagging those would train people to ignore this test.
        def in_repo(ref):
            head = ref.split("/")[0]
            return head == ref or os.path.isdir(os.path.join(REPO_ROOT, head))

        referenced = {r for r in refs if in_repo(r)}
        assert referenced, f"{doc}: no markdown references found -- regex likely stale"

        tracked = self._tracked_files()
        broken = sorted(r for r in referenced if r not in tracked)
        assert not broken, (
            f"{doc} points at markdown git does not track: {broken}. "
            f"`git add` them, or the reference is dead for everyone but this clone."
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
        # Tables may include analytics views; check against both catalogs
        assert registered.issubset(set(q.CATALOG.keys()) | set(q.ANALYTICS_VIEWS.keys()))

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
