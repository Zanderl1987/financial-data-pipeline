"""
test_pipelines.py — smoke-test every pipeline module.

Confirms:
  - each pipeline file imports without error
  - each defines a main() callable
  - key shared utilities (get_dji_symbols fallback, RateLimiter) work offline
  - the DJIA constituent list is correct and not duplicated across pipelines
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
    "company_profile_pipeline",
    "dividend_pipeline",
    "sector_etf_pipeline",
    "schwab_quotes_pipeline",
    "schwab_options_pipeline",
    "massive_option_listings_pipeline",
    "news_sentiment_pipeline",
    "short_interest_pipeline",
    "coingecko_pipeline",
    "forex_pipeline",
    "bea_pipeline",
    "oecd_pipeline",
    "congressional_trades_pipeline",
    "california_disclosures_pipeline",
    "patents_pipeline",
    "ecb_pipeline",
    "fama_french_pipeline",
    "shiller_pipeline",
    "cboe_pipeline",
    "cboe_strategy_pipeline",
    "policy_rates_pipeline",
    "delisting_reference_pipeline",
    "fdic_pipeline",
    "fear_greed_pipeline",
    "nasdaq_data_link_pipeline",
    "treasury_curve_pipeline",
    "usaspending_pipeline",
    "lda_lobbying_pipeline",
    "defillama_pipeline",
    "fed_soma_pipeline",
    "fed_sentiment_pipeline",
    "real_estate_pipeline",
    "shipping_pipeline",
    "piracy_pipeline",
    "jmic_pipeline",
    "yfinance_pipeline",
    "tradingview_pipeline",
    "ibkr_borrow_fee_pipeline",
    "sec_filings_pipeline",
    "schwab_intraday_pipeline",
    "schwab_movers_pipeline",
    "schwab_portfolio_pipeline",
    "signal_scan",
    "signal_monitor",
    "fred_macro_pipeline",
    "fred_credit_pipeline",
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
    "sp500_membership_pipeline",
    "securities_reference_pipeline",
    "fund_holdings_pipeline",
    "etf_holdings_pipeline",
    "ssga_holdings_pipeline",
    "proshares_holdings_pipeline",
    "vaneck_holdings_pipeline",
    "globalx_holdings_pipeline",
    "invesco_holdings_pipeline",
    "amplify_holdings_pipeline",
    "firsttrust_holdings_pipeline",
    "direxion_holdings_pipeline",
    "robo_holdings_pipeline",
    "vanguard_bond_holdings_pipeline",
    "schwab_holdings_pipeline",
    "gold_bar_holdings_pipeline",
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
    "fao_pipeline",
    "plastics_pipeline",
    "cfpb_complaints_pipeline",
    "usgs_helium_mcs_pipeline",
    "usgs_ds140_pipeline",
    "gem_trackers_pipeline",
    "epa_fuel_pipeline",
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
        from dji_symbols import FALLBACK_DJI_SYMBOLS
        assert len(FALLBACK_DJI_SYMBOLS) == 30

    def test_rate_limiter_instantiation(self):
        from finnhub_pipeline import RateLimiter
        limiter = RateLimiter(interval=0.1)
        assert limiter.interval == 0.1

    def test_get_dji_symbols_returns_list(self):
        """get_dji_symbols() must return a non-empty list (Wikipedia or fallback)."""
        from dji_symbols import get_dji_symbols
        symbols = get_dji_symbols()
        assert isinstance(symbols, list)
        assert len(symbols) > 0


class TestDJISymbols:
    """
    Regression cover for the 2026-10-05 consolidation. The seven prior copies of
    get_dji_symbols() silently never scraped anything (the components table had
    moved off the page they pointed at), so every run used a hardcoded list that
    had gone stale in both directions. These assert the membership facts that
    were wrong, so a future bad fallback fails loudly instead of silently
    skewing DJIA-keyed analysis.
    """

    def test_pipelines_share_one_implementation(self):
        """All pipelines must re-export the SAME function, not private copies."""
        import dji_symbols
        for mod_name in (
            "alpha_vantage_fundamentals_pipeline",
            "finnhub_pipeline",
            "fundamentals_pipeline",
            "options_chain_pipeline",
            "price_history_pipeline",
            "sec_edgar_pipeline",
        ):
            mod = importlib.import_module(mod_name)
            assert mod.get_dji_symbols is dji_symbols.get_dji_symbols, (
                f"{mod_name} has its own copy of get_dji_symbols -- import the "
                f"shared one from dji_symbols instead"
            )

    def test_no_module_defines_its_own_get_dji_symbols(self):
        """dji_symbols must be the only definition site in the repo."""
        repo_py = [
            os.path.join(REPO_ROOT, f)
            for f in os.listdir(REPO_ROOT)
            if f.endswith(".py")
        ]
        offenders = [
            os.path.basename(p)
            for p in repo_py
            if os.path.basename(p) != "dji_symbols.py"
            and "def get_dji_symbols(" in open(p, encoding="utf-8").read()
        ]
        assert not offenders, (
            f"duplicate get_dji_symbols definitions in {offenders} -- the whole "
            f"point of dji_symbols.py is that there is exactly one"
        )

    def test_fallback_excludes_dow(self):
        """
        Dow Inc. LEFT the DJIA on 2024-11-08 (Sherwin-Williams replaced it).
        It is an S&P 500 / Russell 3000 member, NOT a DJIA constituent -- so it
        must stay out of a DJIA universe. Guards against a well-meaning
        "DOW is missing, add it" edit reintroducing the pre-2024 membership.
        """
        from dji_symbols import FALLBACK_DJI_SYMBOLS
        assert "DOW" not in FALLBACK_DJI_SYMBOLS

    def test_fallback_has_verizon_out_alphabet_in(self):
        """
        Alphabet replaced Verizon in the DJIA on 2026-06-29. The pre-fix
        fallback still carried VZ and omitted GOOGL -- both wrong, and both
        invisible to a count check since the list was still exactly 30 names.
        """
        from dji_symbols import FALLBACK_DJI_SYMBOLS
        assert "VZ" not in FALLBACK_DJI_SYMBOLS, "VZ left the DJIA 2026-06-29"
        assert "GOOGL" in FALLBACK_DJI_SYMBOLS, "GOOGL joined the DJIA 2026-06-29"

    def test_fallback_is_deduplicated_and_uppercase(self):
        from dji_symbols import FALLBACK_DJI_SYMBOLS
        assert len(set(FALLBACK_DJI_SYMBOLS)) == len(FALLBACK_DJI_SYMBOLS)
        assert all(s == s.upper() for s in FALLBACK_DJI_SYMBOLS)

    def test_parse_symbols_reads_link_templates(self):
        """The Symbol column arrives as {{NYSE link|XXX}} -- must be unwrapped."""
        from dji_symbols import _parse_symbols
        tmpl = "| pad%d\n| NYSE\n| {{NYSE link|SYM%d}}\n|-\n"
        rows = [
            "| Alpha\n| NYSE\n| {{NYSE link|MMM}}\n|-\n",
            "| Beta\n| NASDAQ\n| {{NASDAQ link|GOOGL}}\n|-\n",
        ] + [tmpl % (i, i) for i in range(28)]
        wikitext = '{| id="constituents"\n|-\n! Company\n! Exchange\n! Symbol\n'
        wikitext += "".join(rows) + "|}"
        symbols = _parse_symbols(wikitext)
        assert symbols[:2] == ["MMM", "GOOGL"]
        assert len(symbols) == 30

    def test_parse_symbols_rejects_absurd_counts(self):
        """A wrong-table parse must raise, not return a bogus universe."""
        from dji_symbols import _parse_symbols
        rows = "\n".join(
            f"| pad{i}\n| X\n| {{NYSE link|S{i}}}\n" for i in range(80)
        ) + "\n|}"
        wikitext = '{| id="constituents"\n|-\n! Symbol\n' + rows
        with pytest.raises(ValueError, match="expected about 30"):
            _parse_symbols(wikitext)

    def test_parse_symbols_requires_constituents_table(self):
        from dji_symbols import _parse_symbols
        with pytest.raises(ValueError, match="constituents"):
            _parse_symbols('{| class="wikitable"\n| Symbol\n| {{NYSE link|AAA}}\n|}')

    def test_fallback_used_when_fetch_fails(self, monkeypatch, capsys):
        """A fetch failure must degrade to the verified list and SAY SO."""
        import dji_symbols

        def boom(page):
            raise RuntimeError("simulated network failure")

        monkeypatch.setattr(dji_symbols, "_fetch_wikitext", boom)
        symbols = dji_symbols.get_dji_symbols()
        assert symbols == dji_symbols.FALLBACK_DJI_SYMBOLS
        assert "NOT using live index membership" in capsys.readouterr().out

    def test_strict_mode_raises_instead_of_falling_back(self, monkeypatch):
        """strict=True must not silently analyze a stale universe."""
        import dji_symbols

        def boom(page):
            raise RuntimeError("simulated network failure")

        monkeypatch.setattr(dji_symbols, "_fetch_wikitext", boom)
        with pytest.raises(dji_symbols.DJISymbolError):
            dji_symbols.get_dji_symbols(strict=True)

    def test_returned_list_is_not_the_shared_fallback_object(self, monkeypatch):
        """
        Callers may sort/mutate what they get back. Returning the module-level
        list itself would let one pipeline's sort corrupt every later caller.
        """
        import dji_symbols

        def boom(page):
            raise RuntimeError("simulated network failure")

        monkeypatch.setattr(dji_symbols, "_fetch_wikitext", boom)
        symbols = dji_symbols.get_dji_symbols()
        original_first = dji_symbols.FALLBACK_DJI_SYMBOLS[0]
        symbols.sort()
        # Mutating the result must not have reordered the constant.
        assert dji_symbols.FALLBACK_DJI_SYMBOLS[0] == original_first


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
