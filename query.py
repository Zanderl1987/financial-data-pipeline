"""
DuckDB query layer over the financial-data-pipeline Parquet store.

Usage
-----
    import query as q

    # Load a table (returns a pandas DataFrame)
    df = q.load("prices", symbol="NVDA", start="2025-01-01")
    df = q.load("prices", symbol=["NVDA", "AAPL"])
    df = q.load("macro", series_id="DGS10", start="2024-01-01")
    df = q.load("earnings_calendar", start="2026-06-01")
    df = q.load("fundamentals_annual", symbol="AAPL", metric="revenue")

    # Raw SQL — table names match the keys in CATALOG
    df = q.sql(\"\"\"
        SELECT s.symbol, s.date, s.strike_price, s.theo_price, f.value AS revenue
        FROM synthetic_options s
        JOIN fundamentals_annual f
          ON s.symbol = f.symbol AND f.metric = 'revenue'
        WHERE s.vol_method = 'cc' AND s.model = 'bsm' AND s.symbol = 'NVDA'
    \"\"\")

    # Discovery
    q.tables()                      # all tables with row counts
    q.schema("prices")              # column names and types
    q.symbols("prices")             # available tickers in a table
    q.date_range()                  # min/max dates across all tables
    q.date_range("fundamentals_annual")  # min/max for one table
    q.reload()                      # refresh views after a pipeline run

    # High-level analytics (separate module)
    from analytics import yoy_growth, valuation, upcoming_earnings
    yoy_growth(["AAPL", "MSFT", "NVDA"])
    upcoming_earnings(days_ahead=14)

Run directly to see a full summary:
    python query.py
"""

import glob as _glob_mod
import os
import duckdb
import pandas as pd

_STORAGE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage", "raw")
_CURATED_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage", "curated")
_ICEBERG_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage", "iceberg")

# When True (default), a table's view reads its deduplicated curated snapshot
# (storage/curated/<table>/<table>.parquet) if one exists, falling back to the
# raw glob otherwise. Set q.USE_CURATED = False then q.reload() to force raw.
USE_CURATED = True


def _glob(relative: str) -> str:
    return os.path.join(_STORAGE_ROOT, relative).replace("\\", "/")


def _iceberg_glob(relative: str) -> str:
    """Iceberg tables live under storage/iceberg/, not storage/raw/ — _glob()'s root."""
    return os.path.join(_ICEBERG_ROOT, relative).replace("\\", "/")


def _curated_file(table: str) -> str:
    return os.path.join(_CURATED_ROOT, table, f"{table}.parquet").replace("\\", "/")


# ---------------------------------------------------------------------------
# Table catalog — maps logical names to Parquet glob patterns
# ---------------------------------------------------------------------------

CATALOG: dict[str, str] = {
    # ── Equity prices ───────────────────────────────────────────────────────
    "prices":                  _glob("prices/**/*.parquet"),
    # ── Options ─────────────────────────────────────────────────────────────
    "options_metrics":         _glob("options/metrics/**/*.parquet"),
    "options_chain":           _glob("options/chain/**/*.parquet"),
    "options_history":         _glob("options_history/**/*.parquet"),
    "synthetic_options":       _glob("synthetic_options/**/*.parquet"),
    # ── Fundamentals — SEC EDGAR ─────────────────────────────────────────────
    "fundamentals_annual":     _glob("fundamentals/annual/**/*.parquet"),
    "fundamentals_quarterly":  _glob("fundamentals/quarterly/**/*.parquet"),
    # ── SimFin financial statements ──────────────────────────────────────────
    "simfin_income":           _glob("simfin/income/**/*.parquet"),
    "simfin_balance":          _glob("simfin/balance/**/*.parquet"),
    "simfin_cashflow":         _glob("simfin/cashflow/**/*.parquet"),
    # ── Macro + commodities (FRED) ──────────────────────────────────────────
    "commodities":             _glob("commodities/**/*.parquet"),
    "macro":                   _glob("macro/**/*.parquet"),
    # ── BLS labor market ─────────────────────────────────────────────────────
    "bls_cpi":                 _glob("bls/cpi/**/*.parquet"),
    "bls_ppi":                 _glob("bls/ppi/**/*.parquet"),
    "bls_employment":          _glob("bls/employment/**/*.parquet"),
    "bls_jolts":               _glob("bls/jolts/**/*.parquet"),
    "bls_unemployment":        _glob("bls/unemployment/**/*.parquet"),
    # ── US Treasury fiscal data ──────────────────────────────────────────────
    "treasury_debt":           _glob("treasury/debt/**/*.parquet"),
    "treasury_auctions":       _glob("treasury/auctions/**/*.parquet"),
    # ── World Bank global macro ──────────────────────────────────────────────
    "world_bank":              _glob("world_bank/**/*.parquet"),
    # ── Gas prices ───────────────────────────────────────────────────────────
    "gas_spot":                _glob("gas_prices/spot/**/*.parquet"),
    "gas_retail":              _glob("gas_prices/retail/**/*.parquet"),
    # ── Futures + COT ───────────────────────────────────────────────────────
    "futures":                 _glob("futures/**/*.parquet"),
    "cot":                     _glob("cot/**/*.parquet"),
    # ── Finnhub events ──────────────────────────────────────────────────────
    "earnings_calendar":       _glob("finnhub/earnings_calendar/**/*.parquet"),
    "insider_transactions":    _glob("finnhub/insider_transactions/**/*.parquet"),
    "ipo_calendar":            _glob("finnhub/ipo_calendar/**/*.parquet"),
    # ── Sector ETFs ─────────────────────────────────────────────────────────
    "sector_etfs":             _glob("sector_etfs/**/*.parquet"),
    # ── Short interest ───────────────────────────────────────────────────────
    "short_interest":          _glob("short_interest/**/*.parquet"),
    "finra_short_interest":    _glob("finra_short_interest/**/*.parquet"),
    "sec_ftd":                 _glob("sec_ftd/**/*.parquet"),
    # ── Schwab real-time ────────────────────────────────────────────────────
    "schwab_quotes":           _glob("schwab/quotes/**/*.parquet"),
    "schwab_options":          _glob("schwab/options/**/*.parquet"),
    # ── Tiingo prices + news ─────────────────────────────────────────────────
    "tiingo_prices":           _glob("tiingo/prices/**/*.parquet"),
    "tiingo_news":             _glob("tiingo/news/**/*.parquet"),
    # ── Alpha Vantage technical + forex ──────────────────────────────────────
    "alpha_vantage_technical": _glob("alpha_vantage/technical/**/*.parquet"),
    "alpha_vantage_forex":     _glob("alpha_vantage/forex/**/*.parquet"),
    # ── Institutional holdings (13F) ─────────────────────────────────────────
    "institutional_holdings":  _glob("institutional/**/*.parquet"),
    # ── IMF Primary Commodity Prices ─────────────────────────────────────────
    "imf_commodities":         _glob("imf/**/*.parquet"),
    # ── Metals spot prices ───────────────────────────────────────────────────
    "metals_spot":             _glob("metals/**/*.parquet"),
    # ── FAO global food & agriculture statistics ──────────────────────────────
    "fao_production":          _glob("fao/production/**/*.parquet"),
    "fao_prices":              _glob("fao/prices/**/*.parquet"),
    # ── World Bank Pink Sheet commodity prices ────────────────────────────────
    "wb_commodities":          _glob("worldbank_pink/**/*.parquet"),
    # ── NOAA climate ─────────────────────────────────────────────────────────
    "noaa_climate":            _glob("climate/**/*.parquet"),
    # ── USDA NASS crop production + fertilizer prices ─────────────────────────
    "usda_crops":              _glob("usda/crops/**/*.parquet"),
    "usda_fertilizers":        _glob("usda/fertilizers/**/*.parquet"),
    # ── US Census international trade (HS chapters) ───────────────────────────
    "us_imports_hs":           _glob("trade/imports/**/*.parquet"),
    "us_exports_hs":           _glob("trade/exports/**/*.parquet"),
    # ── EIA petroleum inventories, natural gas storage, crude production ──────
    "eia_petroleum_stocks":    _glob("eia/petroleum_stocks/**/*.parquet"),
    "eia_natgas_storage":      _glob("eia/natgas_storage/**/*.parquet"),
    "eia_crude_production":    _glob("eia/crude_production/**/*.parquet"),
    "eia_refinery_activity":   _glob("eia/refinery_activity/**/*.parquet"),
    "eia_crude_trade":         _glob("eia/crude_trade/**/*.parquet"),
    "eia_hourly_grid":         _glob("eia/hourly_grid/**/*.parquet"),
    # ── Index constituents (Iceberg) ──────────────────────────────────────────
    "index_members":           _iceberg_glob("constituents/index_members/**/*.parquet"),
    "securities":              _iceberg_glob("constituents/securities/**/*.parquet"),
    "fund_holdings":           _iceberg_glob("constituents/fund_holdings/**/*.parquet"),
    "identifier_map":          _iceberg_glob("constituents/identifier_map/**/*.parquet"),
    # ── TSA checkpoint travel volumes ──────────────────────────────────────────
    "tsa_checkpoint":          _glob("tsa/**/*.parquet"),
    # ── CoinGecko cryptocurrency ──────────────────────────────────────────────
    "crypto_market":           _glob("crypto/market/**/*.parquet"),
    "crypto_history":          _glob("crypto/history/**/*.parquet"),
    # ── Forex rates (Frankfurter / ECB) ──────────────────────────────────────
    "forex_rates":             _glob("forex/**/*.parquet"),
    # ── BEA national accounts ─────────────────────────────────────────────────
    "bea_gdp":                 _glob("bea/gdp/**/*.parquet"),
    "bea_income":              _glob("bea/income/**/*.parquet"),
    "bea_profits":             _glob("bea/profits/**/*.parquet"),
    # ── OECD macro indicators ─────────────────────────────────────────────────
    "oecd_macro":              _glob("oecd/**/*.parquet"),
    # ── Congressional stock trade disclosures ─────────────────────────────────
    "congressional_trades":    _glob("congressional_trades/**/*.parquet"),
    # ── USPTO PatentsView ─────────────────────────────────────────────────────
    "patents":                 _glob("patents/**/*.parquet"),
    # ── ECB policy rates, Euribor, yield curve, HICP ─────────────────────────
    "ecb_rates":               _glob("ecb/**/*.parquet"),
    # ── USGS critical mineral statistics ─────────────────────────────────────
    "usgs_minerals":           _glob("usgs_minerals/**/*.parquet"),
    # ── Omkar Cloud commodity spot prices ────────────────────────────────────
    "omkar_commodity":         _glob("omkar_commodity/**/*.parquet"),
    # ── UN Comtrade international trade flows ─────────────────────────────────
    "comtrade_trade":          _glob("comtrade/**/*.parquet"),
    # ── Fama-French factor returns + industry portfolios ──────────────────────
    "ff_factors":              _glob("fama_french/factors/**/*.parquet"),
    "ff_industry":             _glob("fama_french/industry/**/*.parquet"),
    # ── Shiller long-run valuation (CAPE, P/E, dividends back to 1871) ───────
    "shiller_cape":            _glob("shiller/**/*.parquet"),
    # ── CBOE volatility indices (VIX, VIX9D, VIX3M, VIX6M, VVIX, SKEW) ─────
    "cboe_volatility":         _glob("cboe/**/*.parquet"),
    # ── FDIC bank financials, institutions, failures ──────────────────────────
    "fdic_institutions":       _glob("fdic/institutions/**/*.parquet"),
    "fdic_financials":         _glob("fdic/financials/**/*.parquet"),
    "fdic_failures":           _glob("fdic/failures/**/*.parquet"),
    # ── Crypto Fear & Greed Index (Alternative.me) ────────────────────────────
    "fear_greed":              _glob("fear_greed/**/*.parquet"),
    # ── Nasdaq Data Link: S&P 500 valuation + Treasury yield curve ────────────
    "market_valuation":        _glob("nasdaq_data_link/valuation/**/*.parquet"),
    "treasury_yield_curve":    _glob("nasdaq_data_link/yield_curve/**/*.parquet"),
    # ── NY Fed SOMA balance sheet holdings (Treasuries + Agency MBS) ─────────
    "fed_soma":                _glob("fed_soma/**/*.parquet"),
    # ── Alternative / attention data ─────────────────────────────────────────
    "open_meteo_weather":      _glob("open_meteo/**/*.parquet"),
    "wikipedia_pageviews":     _glob("wikipedia/**/*.parquet"),
    "openfda_approvals":       _glob("openfda/approvals/**/*.parquet"),
    "openfda_recalls":         _glob("openfda/recalls/**/*.parquet"),
    # These pipelines write multiple tables into one storage directory, so the
    # glob must match on filename prefix — a bare **/*.parquet would union
    # mismatched schemas across sibling tables.
    "treasury_tic_holders":    _glob("treasury_tic/**/treasury_tic_holders_*.parquet"),
    "treasury_tic_slt":        _glob("treasury_tic/**/treasury_tic_slt_*.parquet"),
    "google_trends_economic":  _glob("google_trends/**/google_trends_economic_*.parquet"),
    "google_trends_market":    _glob("google_trends/**/google_trends_market_*.parquet"),
    "google_trends_sector":    _glob("google_trends/**/google_trends_sector_*.parquet"),
    "reddit_posts":            _glob("reddit/**/reddit_posts_*.parquet"),
    "reddit_mentions":         _glob("reddit/**/reddit_mentions_*.parquet"),
    "ais_positions":           _glob("ais/positions/**/*.parquet"),
    "ais_zone_summary":        _glob("ais/zone_summary/**/*.parquet"),
    # ── Stock Analysis (scraped, no API key) ─────────────────────────────────
    "sa_movers":              _glob("stockanalysis/movers/**/*.parquet"),
    "sa_ipos":                _glob("stockanalysis/ipos/history/**/*.parquet"),
    "sa_ipo_calendar":        _glob("stockanalysis/ipos/calendar/**/*.parquet"),
    "sa_ipo_stats":           _glob("stockanalysis/ipos/stats/**/*.parquet"),
    "sa_corporate_actions":   _glob("stockanalysis/corporate_actions/**/*.parquet"),
    "sa_stock_list":          _glob("stockanalysis/stocks/**/*.parquet"),
    "sa_etf_list":            _glob("stockanalysis/etfs/**/*.parquet"),
    "sa_income":              _glob("stockanalysis/financials/income/**/*.parquet"),
    "sa_balance":             _glob("stockanalysis/financials/balance/**/*.parquet"),
    "sa_cashflow":            _glob("stockanalysis/financials/cashflow/**/*.parquet"),
    "sa_ratios":              _glob("stockanalysis/financials/ratios/**/*.parquet"),
    # ── Finviz market data (scraped, no API key) ─────────────────────────────
    "finviz_movers":           _glob("finviz/movers/**/*.parquet"),
    "finviz_screener":         _glob("finviz/screener/**/*.parquet"),
    "finviz_financials":       _glob("finviz/financials/**/*.parquet"),
    "finviz_insider":          _glob("finviz/insider/**/*.parquet"),
    "finviz_sector_perf":      _glob("finviz/groups/sector/**/*.parquet"),
    "finviz_industry_perf":    _glob("finviz/groups/industry/**/*.parquet"),
    "finviz_country_perf":     _glob("finviz/groups/country/**/*.parquet"),
    "finviz_group_valuation":  _glob("finviz/groups/valuation/**/*.parquet"),
    # ── News sentiment ────────────────────────────────────────────────────────
    "news_sentiment":          _glob("finnhub/news_sentiment/**/*.parquet"),
    # ── Fed sentiment (RSS speeches/statements + Claude hawkish/dovish) ──────
    "fed_speeches":            _glob("fed/speeches/**/*.parquet"),
    "fed_sentiment":           _glob("fed/sentiment/**/*.parquet"),
    # ── Real estate (FHFA HPI + Zillow ZHVI/ZORI) ────────────────────────────
    "fhfa_hpi":                _glob("fhfa/hpi/**/*.parquet"),
    "zillow_zhvi":             _glob("zillow/zhvi/**/*.parquet"),
    "zillow_zori":             _glob("zillow/zori/**/*.parquet"),
    # ── Shipping / logistics (NY Fed GSCPI + FRED freight PPI) ───────────────
    # ── Shipping / logistics (Iceberg) ──────────────────────────────────────
    "shipping_gscpi":         _iceberg_glob("shipping/gscpi/**/*.parquet"),
    "shipping_freight_ppi":   _iceberg_glob("shipping/freight_ppi/**/*.parquet"),
    # ── Dividends ────────────────────────────────────────────────────────────
    "dividends":               _glob("finnhub/dividends/**/*.parquet"),
    # ── Finnhub fundamentals + market data ───────────────────────────────────
    "finnhub_profile":         _glob("finnhub/profile/**/*.parquet"),
    "finnhub_quotes":          _glob("finnhub/quotes/**/*.parquet"),
    "finnhub_metrics":         _glob("finnhub/metrics/**/*.parquet"),
    "finnhub_recommendations": _glob("finnhub/recommendations/**/*.parquet"),
    "finnhub_price_targets":   _glob("finnhub/price_targets/**/*.parquet"),
    "finnhub_upgrades":        _glob("finnhub/upgrades/**/*.parquet"),
    "finnhub_news":            _glob("finnhub/news/**/*.parquet"),
    # ── Yahoo Finance deep market history (indices, futures, FX, rates) ──────
    "market_history":          _glob("yfinance/**/*.parquet"),
    # ── TradingView technical-rating snapshots ───────────────────────────────
    "tv_ratings":              _glob("tradingview/**/*.parquet"),
    # ── SEC EDGAR filing index (8-K, 10-K/Q, S-1, 13D/G, proxies) ────────────
    "sec_filings":             _glob("sec_filings/**/*.parquet"),
    # ── Schwab intraday bars, movers, and portfolio mirror ───────────────────
    "schwab_intraday":         _glob("schwab/intraday/**/*.parquet"),
    "schwab_movers":           _glob("schwab/movers/**/*.parquet"),
    "schwab_positions":        _glob("schwab/positions/**/*.parquet"),
    "schwab_transactions":     _glob("schwab/transactions/**/*.parquet"),
    # ── Signal health monitor (maintained backtest performance tracking) ─────
    "signal_health":           _glob("signal_monitor/**/*.parquet"),
    # ── FRED macro indicators ──────────────────────────────────────────────────
    "fred_macro_housing":     _glob("fred_macro/housing/**/*.parquet"),
    "fred_macro_sentiment":   _glob("fred_macro/sentiment/**/*.parquet"),
    "fred_macro_industrial":  _glob("fred_macro/industrial/**/*.parquet"),
    "fred_macro_consumer":    _glob("fred_macro/consumer/**/*.parquet"),
    "fred_macro_trade":       _glob("fred_macro/trade/**/*.parquet"),
    # ── FRED rates & GDP ───────────────────────────────────────────────────────
    "fred_rates_gdp_interest_rates":  _glob("fred_rates_gdp/interest_rates/**/*.parquet"),
    "fred_rates_gdp_money_supply":    _glob("fred_rates_gdp/money_supply/**/*.parquet"),
    "fred_rates_gdp_gdp":             _glob("fred_rates_gdp/gdp/**/*.parquet"),
    "fred_rates_gdp_inflation":       _glob("fred_rates_gdp/inflation/**/*.parquet"),
    "fred_rates_gdp_mortgage":        _glob("fred_rates_gdp/mortgage/**/*.parquet"),
    "fred_rates_gdp_commodities":     _glob("fred_rates_gdp/commodities/**/*.parquet"),
    "fred_rates_gdp_exchange_rates":  _glob("fred_rates_gdp/exchange_rates/**/*.parquet"),
    "fred_rates_gdp_markets":         _glob("fred_rates_gdp/markets/**/*.parquet"),
    "fred_rates_gdp_federal_debt":    _glob("fred_rates_gdp/federal_debt/**/*.parquet"),
    # ── Alpha Vantage fundamentals ────────────────────────────────────────────
    "alpha_vantage_overview":              _glob("alpha_vantage/overview/**/*.parquet"),
    "alpha_vantage_income_statement":      _glob("alpha_vantage/income_statement/**/*.parquet"),
    "alpha_vantage_balance_sheet":         _glob("alpha_vantage/balance_sheet/**/*.parquet"),
    "alpha_vantage_cash_flow":             _glob("alpha_vantage/cash_flow/**/*.parquet"),
    "alpha_vantage_earnings":              _glob("alpha_vantage/earnings/**/*.parquet"),
    "alpha_vantage_earnings_calendar":     _glob("alpha_vantage/earnings_calendar/**/*.parquet"),
    "alpha_vantage_dividends":             _glob("alpha_vantage/dividends/**/*.parquet"),
    "alpha_vantage_insider_transactions":  _glob("alpha_vantage/insider_transactions/**/*.parquet"),
    "alpha_vantage_news_sentiment":        _glob("alpha_vantage/news_sentiment/**/*.parquet"),
    "alpha_vantage_top_gainers_losers":    _glob("alpha_vantage/top_gainers_losers/**/*.parquet"),
    # ── CoinGecko (extended) ──────────────────────────────────────────────────
    "coingecko_global_market":   _glob("coingecko/global_market/**/*.parquet"),
    "coingecko_coins_markets":   _glob("coingecko/coins_markets/**/*.parquet"),
    "coingecko_trending":        _glob("coingecko/trending/**/*.parquet"),
    "coingecko_categories":      _glob("coingecko/categories/**/*.parquet"),
    "coingecko_derivatives":     _glob("coingecko/derivatives/**/*.parquet"),
    "coingecko_exchange_rates":  _glob("coingecko/exchange_rates/**/*.parquet"),
    # ── SEC EDGAR filings & fundamentals ──────────────────────────────────────
    "sec_edgar_submissions":        _glob("sec_edgar/submissions/**/*.parquet"),
    "sec_edgar_xbrl_fundamentals":  _glob("sec_edgar/xbrl_fundamentals/**/*.parquet"),
    "sec_edgar_efts_search":        _glob("sec_edgar/efts_search/**/*.parquet"),
    # ── BLS labor market (extended) ───────────────────────────────────────────
    "bls_import_export_prices":  _glob("bls/import_export/**/*.parquet"),
    "bls_eci":                   _glob("bls/eci/**/*.parquet"),
    "bls_productivity":          _glob("bls/productivity/**/*.parquet"),
    "bls_oes":               _glob("bls/oes/**/*.parquet"),
    "bls_qcew":              _glob("bls/qcew/**/*.parquet"),
    "bls_ecec":              _glob("bls/ecec/**/*.parquet"),
    "bls_cps_demographics":  _glob("bls/cps_demographics/**/*.parquet"),
    # ── EIA energy data (extended) ────────────────────────────────────────────
    "eia_electricity_generation":  _glob("eia/electricity_generation/**/*.parquet"),
    "eia_electricity_sales":       _glob("eia/electricity_sales/**/*.parquet"),
    "eia_nuclear_outages":         _glob("eia/nuclear_outages/**/*.parquet"),
    "eia_coal_production":         _glob("eia/coal_production/**/*.parquet"),
    "eia_coal_trade":              _glob("eia/coal_trade/**/*.parquet"),
    "eia_international":           _glob("eia/international/**/*.parquet"),
    "eia_seds":                    _glob("eia/seds/**/*.parquet"),
    "eia_petroleum_spot_prices":    _glob("eia/petroleum_spot_prices/**/*.parquet"),
    "eia_petroleum_futures":        _glob("eia/petroleum_futures/**/*.parquet"),
    "eia_refiner_margins":          _glob("eia/refiner_margins/**/*.parquet"),
    "eia_petroleum_supply_demand":  _glob("eia/petroleum_supply_demand/**/*.parquet"),
    "eia_natural_gas_consumption":  _glob("eia/natural_gas_consumption/**/*.parquet"),
    "eia_natural_gas_prices":       _glob("eia/natural_gas_prices/**/*.parquet"),
    "eia_natural_gas_production":   _glob("eia/natural_gas_production/**/*.parquet"),
    "eia_lng_flows":                _glob("eia/lng_flows/**/*.parquet"),
    # ── Finnhub fundamentals + market data (extended) ─────────────────────────
    "finnhub_esg":                    _glob("finnhub/esg/**/*.parquet"),
    "finnhub_congressional_trading":  _glob("finnhub/congressional_trading/**/*.parquet"),
    "finnhub_supply_chain":           _glob("finnhub/supply_chain/**/*.parquet"),
    "finnhub_insider_sentiment":      _glob("finnhub/insider_sentiment/**/*.parquet"),
    "finnhub_social_sentiment":       _glob("finnhub/social_sentiment/**/*.parquet"),
    "finnhub_sec_filings":            _glob("finnhub/sec_filings/**/*.parquet"),
    "finnhub_earnings_quality":       _glob("finnhub/earnings_quality/**/*.parquet"),
    "finnhub_lobbying":               _glob("finnhub/lobbying/**/*.parquet"),
    "finnhub_usa_spending":           _glob("finnhub/usa_spending/**/*.parquet"),
    "finnhub_uspto_patents":          _glob("finnhub/uspto_patents/**/*.parquet"),
    "finnhub_visa_applications":      _glob("finnhub/visa_applications/**/*.parquet"),
    "finnhub_economic_calendar":      _glob("finnhub/economic_calendar/**/*.parquet"),
    "finnhub_earnings_history":        _glob("finnhub/earnings_history/**/*.parquet"),
    "finnhub_eps_estimates":           _glob("finnhub/eps_estimates/**/*.parquet"),
    "finnhub_revenue_estimates":       _glob("finnhub/revenue_estimates/**/*.parquet"),
    "finnhub_ownership":               _glob("finnhub/ownership/**/*.parquet"),
    "finnhub_splits":                  _glob("finnhub/splits/**/*.parquet"),
    "finnhub_peers":                   _glob("finnhub/peers/**/*.parquet"),
    "finnhub_executives":              _glob("finnhub/executives/**/*.parquet"),
    "finnhub_filing_sentiment":        _glob("finnhub/filing_sentiment/**/*.parquet"),
    "finnhub_transcripts":             _glob("finnhub/transcripts/**/*.parquet"),
    "finnhub_company_news_sentiment":  _glob("finnhub/company_news_sentiment/**/*.parquet"),
    # ── Tiingo corporate actions + fundamentals ───────────────────────────────
    "tiingo_corporate_actions_dividends":  _glob("tiingo/corporate_actions_dividends/**/*.parquet"),
    "tiingo_corporate_actions_splits":     _glob("tiingo/corporate_actions_splits/**/*.parquet"),
    "tiingo_corporate_actions_yield":      _glob("tiingo/corporate_actions_yield/**/*.parquet"),
    "tiingo_fundamentals_daily":       _glob("tiingo/fundamentals_daily/**/*.parquet"),
    "tiingo_fundamentals_statements":  _glob("tiingo/fundamentals_statements/**/*.parquet"),
    # ── US Treasury fiscal data (extended) ────────────────────────────────────
    "treasury_debt_to_penny":          _glob("treasury/debt_to_penny/**/*.parquet"),
    "treasury_avg_interest_rates":     _glob("treasury/avg_interest_rates/**/*.parquet"),
    "treasury_interest_expense":       _glob("treasury/interest_expense/**/*.parquet"),
    "treasury_auctions_detail":        _glob("treasury/auctions_detail/**/*.parquet"),
    "treasury_exchange_rates":         _glob("treasury/exchange_rates/**/*.parquet"),
    "treasury_savings_bonds":          _glob("treasury/savings_bonds/**/*.parquet"),
    "treasury_mts_receipts_outlays":   _glob("treasury/mts_receipts_outlays/**/*.parquet"),
    "treasury_mts_outlays_by_agency":  _glob("treasury/mts_outlays_by_agency/**/*.parquet"),
    "treasury_dts_operating_cash":     _glob("treasury/dts_operating_cash/**/*.parquet"),
    "treasury_mts_budget_comparison":  _glob("treasury/mts_budget_comparison/**/*.parquet"),
    # ── SEC EDGAR raw filing text (10-K/10-Q from TeraflopAI/SEC-EDGAR) ─────────
    "sec_edgar_text":          _glob("sec_edgar_text/**/*.parquet"),
    # ── CFPB consumer finance complaints ───────────────────────────────────────
    "cfpb_complaints":        _glob("cfpb_complaints/**/*.parquet"),
    # ── Redfin housing market tracker (national / metro / state) ──────────────
    "redfin_market_tracker":  _glob("redfin/market_tracker/**/*.parquet"),
    # ── AQR factor library (VME, QMJ, TSMOM monthly factors) ──────────────────
    "aqr_factors":            _glob("aqr/factors/**/*.parquet"),
    # ── ETF holdings with quant scores (SecuritiesDB, keyless) ─────────────────
    "etf_holdings":           _glob("etf_holdings/**/*.parquet"),
}

# ---------------------------------------------------------------------------
# Analytics views — cross-table joins built on top of the base catalog
# ---------------------------------------------------------------------------
ANALYTICS_VIEWS: dict[str, str] = {
    # Securities enriched with OpenFIGI identifiers
    "securities_full": """
        SELECT
            s.symbol,
            s.company_name,
            s.asset_type,
            s.sector,
            s.industry,
            s.exchange,
            s.currency,
            s.country,
            s.market_cap,
            s.shares_outstanding,
            s.ipo_date,
            s.cik,
            s.is_sp500,
            s.is_nasdaq100,
            s.is_dji30,
            s.is_russell3000,
            s.is_russell2000,
            s.is_wilshire5000,
            i.figi,
            i.composite_figi,
            i.cusip,
            i.isin,
            i.sedol,
            i.security_type AS openfigi_security_type
        FROM securities s
        LEFT JOIN identifier_map i ON s.symbol = i.ticker
    """,

    # Index constituents enriched with reference data + FIGI
    "index_holdings": """
        SELECT
            im.snapshot_date,
            im.index_code,
            im.index_name,
            im.ticker,
            im.company_name,
            im.gics_sector,
            im.gics_sub_industry,
            im.weight_pct,
            im.shares_outstanding,
            im.market_cap AS index_market_cap,
            s.sector,
            s.industry,
            s.asset_type,
            s.exchange,
            s.country,
            s.is_sp500,
            s.is_nasdaq100,
            s.is_dji30,
            i.figi,
            i.composite_figi
        FROM index_members im
        LEFT JOIN securities s ON im.ticker = s.symbol
        LEFT JOIN identifier_map i ON im.ticker = i.ticker
    """,

    # Fund holdings enriched with issuer reference data + FIGI
    "fund_holdings_full": """
        SELECT
            fh.snapshot_date,
            fh.fund_ticker,
            fh.fund_name,
            fh.holding_ticker,
            fh.holding_name,
            fh.weight_pct,
            fh.market_value_usd,
            fh.shares_held,
            fh.asset_category,
            fh.sector AS fund_sector,
            fh.country AS fund_country,
            s.sector,
            s.industry,
            s.asset_type,
            s.exchange,
            s.country,
            s.market_cap,
            i.figi,
            i.composite_figi,
            fh.filing_date,
            fh.reporting_period_end
        FROM fund_holdings fh
        LEFT JOIN securities s ON fh.holding_ticker = s.symbol
        LEFT JOIN identifier_map i ON fh.holding_ticker = i.ticker
    """,

    # Institutional holdings enriched with issuer reference data
    "institutional_holdings_enriched": """
        SELECT
            ih.institution,
            ih.cik,
            ih.filed_date,
            ih.company_name,
            ih.cusip,
            ih.value_usd,
            ih.shares,
            ih.share_type,
            ih.put_call,
            ih.investment_discretion,
            s.symbol,
            s.sector,
            s.industry,
            s.market_cap,
            s.exchange,
            s.country
        FROM institutional_holdings ih
        LEFT JOIN securities s ON ih.company_name = s.company_name
    """,

    # Insider transactions enriched with issuer reference data
    "insider_by_sector": """
        SELECT
            it.symbol,
            it.name,
            it.share,
            it.change,
            it.filing_date,
            it.transaction_date,
            it.transaction_code,
            it.transaction_price,
            s.sector,
            s.industry,
            s.company_name,
            s.market_cap,
            s.exchange,
            s.country
        FROM insider_transactions it
        LEFT JOIN securities s ON it.symbol = s.symbol
    """,

    # Earnings calendar with index membership flags
    "earnings_with_index": """
        SELECT
            ec.symbol,
            ec.date,
            ec.hour,
            ec.quarter,
            ec.year,
            ec.eps_estimate,
            ec.eps_actual,
            ec.revenue_estimate,
            ec.revenue_actual,
            s.company_name,
            s.sector,
            s.industry,
            s.market_cap,
            s.is_sp500,
            s.is_nasdaq100,
            s.is_dji30,
            s.is_russell3000
        FROM earnings_calendar ec
        LEFT JOIN securities s ON ec.symbol = s.symbol
    """,

    # Prices enriched with security reference data
    "prices_enriched": """
        SELECT
            p.symbol,
            p.date,
            p.open,
            p.high,
            p.low,
            p.close,
            p.volume,
            p.pct_change,
            p.vwap,
            s.company_name,
            s.sector,
            s.industry,
            s.asset_type,
            s.exchange,
            s.country,
            s.market_cap,
            s.is_sp500,
            s.is_nasdaq100,
            s.is_dji30
        FROM prices p
        LEFT JOIN securities s ON p.symbol = s.symbol
    """,
}


_CON: duckdb.DuckDBPyConnection | None = None


def _con() -> duckdb.DuckDBPyConnection:
    global _CON
    if _CON is None:
        _CON = duckdb.connect()
        _register_views(_CON)
    return _CON


def _register_views(con: duckdb.DuckDBPyConnection) -> None:
    """
    Register a DuckDB view for every catalog entry that has data.

    Prefers the deduplicated curated snapshot (storage/curated/<table>/...) when
    one exists and USE_CURATED is True; otherwise reads the raw dated-file glob.
    Curated reads are clean of the cross-run row duplication inherent in the raw
    layer — see curated.py.
    """
    for name, glob_path in CATALOG.items():
        curated = _curated_file(name)
        if USE_CURATED and os.path.exists(curated.replace("/", os.sep)):
            con.execute(f"""
                CREATE OR REPLACE VIEW {name} AS
                SELECT * FROM read_parquet('{curated}')
            """)
            continue
        if not _glob_mod.glob(glob_path.replace("/", os.sep), recursive=True):
            continue
        # union_by_name tolerates schema drift across incremental files
        # hive_partitioning reads year=/month= directory structure as virtual columns
        con.execute(f"""
            CREATE OR REPLACE VIEW {name} AS
            SELECT * FROM read_parquet('{glob_path}', union_by_name=True, hive_partitioning=True)
        """)

    # Analytics cross-table views
    for name, sql_text in ANALYTICS_VIEWS.items():
        try:
            con.execute(f"CREATE OR REPLACE VIEW {name} AS {sql_text}")
        except Exception as e:
            # Base views may not exist yet — skip silently
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reload() -> None:
    """Re-register all views. Call after a pipeline run drops new files."""
    global _CON
    _CON = None
    _con()


def sql(query: str) -> pd.DataFrame:
    """Execute raw SQL against the registered views. Returns a DataFrame."""
    return _con().execute(query).df()


def load(
    table: str,
    symbol: "str | list[str] | None" = None,
    series_id: "str | list[str] | None" = None,
    metric: "str | None" = None,
    start: "str | None" = None,
    end: "str | None" = None,
    columns: "list[str] | None" = None,
    limit: "int | None" = None,
) -> pd.DataFrame:
    """
    Load a table with optional push-down filters. Returns a DataFrame.

    Parameters
    ----------
    table     : table name — one of the keys in CATALOG
    symbol    : str or list  — filter WHERE symbol = / IN (...)
    series_id : str or list  — filter WHERE series_id = / IN (...)
    metric    : str          — filter WHERE metric = '...'  (fundamentals, macro)
    start     : 'YYYY-MM-DD' — filter WHERE date >= start
    end       : 'YYYY-MM-DD' — filter WHERE date <= end
    columns   : list of column names to SELECT (default: all)
    limit     : int          — LIMIT N rows (default: no limit)
    """
    all_tables = set(CATALOG) | set(ANALYTICS_VIEWS)
    if table not in all_tables:
        raise ValueError(f"Unknown table '{table}'. Available: {sorted(all_tables)}")

    select = ", ".join(columns) if columns else "*"
    clauses: list[str] = []

    if symbol is not None:
        if isinstance(symbol, str):
            clauses.append(f"symbol = '{symbol}'")
        else:
            quoted = ", ".join(f"'{s}'" for s in symbol)
            clauses.append(f"symbol IN ({quoted})")

    if series_id is not None:
        if isinstance(series_id, str):
            clauses.append(f"series_id = '{series_id}'")
        else:
            quoted = ", ".join(f"'{s}'" for s in series_id)
            clauses.append(f"series_id IN ({quoted})")

    if metric is not None:
        clauses.append(f"metric = '{metric}'")

    if start is not None:
        clauses.append(f"date >= '{start}'")
    if end is not None:
        clauses.append(f"date <= '{end}'")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = f"LIMIT {limit}" if limit else ""
    try:
        return sql(f"SELECT {select} FROM {table} {where} {limit_clause}".strip())
    except duckdb.CatalogException:
        # View not registered — table exists in CATALOG but has no files on disk yet
        return pd.DataFrame()


def schema(table: str) -> pd.DataFrame:
    """Return column names and DuckDB types for a table."""
    all_tables = set(CATALOG) | set(ANALYTICS_VIEWS)
    if table not in all_tables:
        raise ValueError(f"Unknown table '{table}'. Available: {sorted(all_tables)}")
    return sql(f"DESCRIBE {table}")


def tables() -> pd.DataFrame:
    """List all catalog entries with row counts. 'no data' = no files on disk yet."""
    rows = []
    for name in list(CATALOG) + list(ANALYTICS_VIEWS):
        try:
            count = _con().execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            rows.append({"table": name, "rows": f"{count:,}", "type": "analytics" if name in ANALYTICS_VIEWS else "base"})
        except Exception:
            rows.append({"table": name, "rows": "no data", "type": "analytics" if name in ANALYTICS_VIEWS else "base"})
    return pd.DataFrame(rows)


def symbols(table: str) -> list[str]:
    """Return sorted list of distinct tickers available in a table."""
    all_tables = set(CATALOG) | set(ANALYTICS_VIEWS)
    if table not in all_tables:
        raise ValueError(f"Unknown table '{table}'. Available: {sorted(all_tables)}")
    try:
        return sql(f"SELECT DISTINCT symbol FROM {table} ORDER BY symbol")["symbol"].tolist()
    except Exception:
        return []


def date_range(table: "str | None" = None) -> pd.DataFrame:
    """
    Return min/max date for each table (or a single table if specified).
    Tables without a 'date' column are skipped.
    """
    targets = [table] if table else list(CATALOG.keys())
    rows = []
    for name in targets:
        try:
            r = _con().execute(
                f"SELECT MIN(date)::VARCHAR, MAX(date)::VARCHAR FROM {name}"
            ).fetchone()
            rows.append({"table": name, "min_date": r[0], "max_date": r[1]})
        except Exception:
            pass
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n=== Table Inventory ===")
    t = tables()
    print(t.to_string(index=False))

    print("\n=== Date Ranges ===")
    dr = date_range()
    if not dr.empty:
        print(dr.to_string(index=False))
    else:
        print("(no tables with date columns found)")
