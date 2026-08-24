"""
test_pipelines.py — smoke-test every pipeline module.

Confirms:
  - each pipeline file imports without error
  - each defines a main() callable
  - key shared utilities (get_dji_symbols fallback, RateLimiter) work offline
  - FINRA settlement-date generator returns plausible dates
  - SEC FTD URL template formats correctly
"""

import sys
import os
import datetime
import importlib
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

PIPELINE_MODULES = [
    "price_history_pipeline",
    "synthetic_options_pipeline",
    "yahoo_options_pipeline",
    "commodity_macro_pipeline",
    "futures_pipeline",
    "gas_price_pipeline",
    "options_chain_pipeline",
    "fundamentals_pipeline",
    "finnhub_pipeline",
    "finnhub_events_pipeline",
    "dividend_pipeline",
    "sector_etf_pipeline",
    "schwab_quotes_pipeline",
    "schwab_options_pipeline",
    "news_sentiment_pipeline",
    "short_interest_pipeline",
    "coingecko_pipeline",
    "forex_pipeline",
    "bea_pipeline",
    "oecd_pipeline",
    "congressional_trades_pipeline",
    "patents_pipeline",
    "ecb_pipeline",
    "fama_french_pipeline",
    "shiller_pipeline",
    "cboe_pipeline",
    "fdic_pipeline",
    "fear_greed_pipeline",
    "nasdaq_data_link_pipeline",
    "fed_soma_pipeline",
    "fed_sentiment_pipeline",
    "real_estate_pipeline",
    "shipping_pipeline",
    "piracy_pipeline",
    "yfinance_pipeline",
    "tradingview_pipeline",
    "sec_filings_pipeline",
    "schwab_intraday_pipeline",
    "schwab_movers_pipeline",
    "schwab_portfolio_pipeline",
    "signal_scan",
    "signal_monitor",
    "fred_macro_pipeline",
    "fred_rates_gdp_pipeline",
    "alpha_vantage_fundamentals_pipeline",
    "coingecko_expansion_pipeline",
    "sec_edgar_pipeline",
    "bls_expansion_pipeline",
    "bls_oes_qcew_pipeline",
    "eia_expansion_pipeline",
    "eia_petng_prices_pipeline",
    "eia_hourly_grid_pipeline",
    "index_constituents_pipeline",
    "securities_reference_pipeline",
    "fund_holdings_pipeline",
    "etf_holdings_pipeline",
    "openfigi_pipeline",
    "finnhub_expansion_pipeline",
    "finnhub_fundamentals_pipeline",
    "tiingo_corporate_actions_pipeline",
    "tiingo_fundamentals_pipeline",
    "treasury_fiscal_pipeline",
    "omkar_commodity_pipeline",
    "dark_pool_pipeline",
    "retail_sentiment_pipeline",
    "insider_sentiment_pipeline",
    "indeed_hiringlab_pipeline",
    "usda_pipeline",
]


class TestPipelineImports:
    @pytest.mark.parametrize("module_name", PIPELINE_MODULES)
    def test_module_imports(self, module_name):
        """Every pipeline module must be importable (catches syntax errors + bad imports).
        Pipelines with optional deps (schwabdev, anthropic) are skipped when not installed."""
        try:
            mod = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            pytest.skip(f"Optional dependency not installed: {e}")
        assert mod is not None

    @pytest.mark.parametrize("module_name", PIPELINE_MODULES)
    def test_module_has_main(self, module_name):
        """Every pipeline module must expose a callable main()."""
        try:
            mod = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            pytest.skip(f"Optional dependency not installed: {e}")
        assert hasattr(mod, "main"), f"{module_name} is missing main()"
        assert callable(mod.main), f"{module_name}.main is not callable"


class TestSharedUtilities:
    def test_dji_fallback_symbols_length(self):
        """Fallback symbol list should have exactly 30 DJI components."""
        from finnhub_pipeline import FALLBACK_SYMBOLS
        assert len(FALLBACK_SYMBOLS) == 30

    def test_rate_limiter_instantiation(self):
        from finnhub_pipeline import RateLimiter
        limiter = RateLimiter(interval=0.1)
        assert limiter.interval == 0.1

    def test_get_dji_symbols_returns_list(self):
        """get_dji_symbols() must return a non-empty list (Wikipedia or fallback)."""
        from finnhub_pipeline import get_dji_symbols
        symbols = get_dji_symbols()
        assert isinstance(symbols, list)
        assert len(symbols) > 0


class TestShortInterestHelpers:
    def test_settlement_date_candidates(self):
        from short_interest_pipeline import _candidate_settlement_dates
        dates = _candidate_settlement_dates(n=6)
        assert len(dates) == 6
        # All dates should be parseable YYYYMMDD strings
        for d in dates:
            dt = datetime.datetime.strptime(d, "%Y%m%d")
            # Candidates can be slightly in the future (end-of-month settlement dates
            # not yet published). Allow up to 15 days forward.
            assert dt.date() <= datetime.date.today() + datetime.timedelta(days=15)
        # Should be in descending order
        assert dates == sorted(dates, reverse=True)

    def test_sec_ftd_url_format(self):
        from short_interest_pipeline import SEC_FTD_URL
        url = SEC_FTD_URL.format(year=2024, month=3, half="a")
        assert "202403a" in url
        assert url.startswith("https://www.sec.gov")
        assert url.endswith(".zip")

    def test_yf_field_map_coverage(self):
        from short_interest_pipeline import _YF_FIELDS
        expected_output_cols = {
            "shares_short", "shares_short_prior_month", "short_pct_float",
            "days_to_cover", "float_shares", "shares_outstanding", "filing_date_unix",
        }
        assert set(_YF_FIELDS.values()) == expected_output_cols


class TestCatalogStorageDirs:
    """Every CATALOG entry's parent directory must exist (enforced by .gitkeep files)."""

    def test_short_interest_dirs_exist(self):
        for sub in ("short_interest", "finra_short_interest", "sec_ftd"):
            path = os.path.join(REPO_ROOT, "storage", "raw", sub)
            assert os.path.isdir(path), f"Missing storage dir: {path}"

    def test_schwab_dirs_exist(self):
        for sub in ("schwab/quotes", "schwab/options", "schwab/intraday",
                    "schwab/movers", "schwab/positions", "schwab/transactions"):
            path = os.path.join(REPO_ROOT, "storage", "raw", *sub.split("/"))
            assert os.path.isdir(path), f"Missing storage dir: {path}"

    def test_finnhub_sentiment_dir_exists(self):
        path = os.path.join(REPO_ROOT, "storage", "raw", "finnhub", "news_sentiment")
        assert os.path.isdir(path), f"Missing storage dir: {path}"

    def test_sector_etfs_dir_exists(self):
        path = os.path.join(REPO_ROOT, "storage", "raw", "sector_etfs")
        assert os.path.isdir(path), f"Missing storage dir: {path}"
