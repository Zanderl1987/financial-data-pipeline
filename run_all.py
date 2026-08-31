#!/usr/bin/env python3
"""
Unified Pipeline Runner — runs all financial data pipelines in dependency order.

Stages
------
  Stage 1  — Free/public sources (FRED, EIA, yfinance, Finnhub, SEC EDGAR, CFTC)
  Stage 2  — Schwab-authenticated (prices, ETFs, real-time quotes, options chains)
  Stage 3  — Derived (synthetic options uses Stage 2 prices; news sentiment uses Stage 1 news)

Usage
-----
  python run_all.py                        # incremental run (all stages)
  python run_all.py --backfill             # full available history
  python run_all.py --stage 1              # free/public sources only
  python run_all.py --only commodity_macro,gas_prices,finnhub
  python run_all.py --skip fundamentals,synthetic_options
  python run_all.py --dry-run              # print commands, don't execute
  python run_all.py --no-validate          # skip post-run validation
  python run_all.py --no-compact           # skip post-run curated compaction
  python run_all.py --no-hf-sync           # skip post-run HuggingFace dataset sync
"""

import argparse
import datetime
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field

from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

import curated
from validate import validate_table
from logging_utils import get_logger, log_pipeline_failure

load_dotenv()

log = get_logger("run_all")


# ── Pipeline registry ─────────────────────────────────────────────────────────

@dataclass
class PipelineSpec:
    name:             str
    file:             str
    desc:             str
    stage:            int
    tables:           list = field(default_factory=list)
    requires_env:     list = field(default_factory=list)
    backfill_args:    list = field(default_factory=list)
    incremental_args: list = field(default_factory=list)
    timeout:          int  = 600   # seconds; override for slow pipelines


PIPELINES: list[PipelineSpec] = [
    # ── Stage 1 — Free / public sources ────────────────────────────────────────
    PipelineSpec(
        name="commodity_macro",
        file="commodity_macro_pipeline.py",
        desc="FRED commodities, macro indicators, credit spreads",
        stage=1,
        tables=["commodities", "macro"],
        requires_env=["FRED_API_KEY"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="gas_prices",
        file="gas_price_pipeline.py",
        desc="EIA spot and retail gas/diesel prices",
        stage=1,
        tables=["gas_spot", "gas_retail"],
        requires_env=["EIA_API_KEY"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="futures",
        file="futures_pipeline.py",
        desc="yfinance futures OHLCV + CFTC COT positions",
        stage=1,
        tables=["futures", "cot"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="short_interest",
        file="short_interest_pipeline.py",
        desc="yfinance + FINRA Reg SHO + SEC fails-to-deliver",
        stage=1,
        tables=["short_interest", "finra_short_interest", "sec_ftd"],
        backfill_args=["--source", "all"],
        incremental_args=["--source", "all"],
    ),
    PipelineSpec(
        name="finnhub",
        file="finnhub_pipeline.py",
        desc="Finnhub profile, quotes, metrics, recommendations, news",
        stage=1,
        tables=[
            "finnhub_profile", "finnhub_quotes", "finnhub_metrics",
            "finnhub_recommendations", "finnhub_price_targets",
            "finnhub_upgrades", "finnhub_news",
        ],
        requires_env=["FINNHUB_API_KEY"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="finnhub_events",
        file="finnhub_events_pipeline.py",
        desc="Finnhub earnings calendar + insider transactions + IPO calendar",
        stage=1,
        tables=["earnings_calendar", "insider_transactions", "ipo_calendar"],
        requires_env=["FINNHUB_API_KEY"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="dividends",
        file="dividend_pipeline.py",
        desc="Finnhub per-symbol cash dividend history",
        stage=1,
        tables=["dividends"],
        requires_env=["FINNHUB_API_KEY"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="fundamentals",
        file="fundamentals_pipeline.py",
        desc="SEC EDGAR company fundamentals (DJI components)",
        stage=1,
        tables=["fundamentals_annual", "fundamentals_quarterly"],
        requires_env=["EDGAR_USER_AGENT"],
        backfill_args=["--quarters", "40"],   # ~10 years of quarterly data
        timeout=1800,                          # large download; allow 30 min
    ),
    PipelineSpec(
        name="bls",
        file="bls_pipeline.py",
        desc="BLS CPI, PPI, employment (nonfarm payrolls by sector), JOLTS, unemployment",
        stage=1,
        tables=["bls_cpi", "bls_ppi", "bls_avg_price", "bls_employment", "bls_jolts", "bls_unemployment"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="treasury",
        file="treasury_pipeline.py",
        desc="US Treasury fiscal data — debt, avg interest rates, auction results, DTS",
        stage=1,
        tables=["treasury_debt", "treasury_auctions"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="world_bank",
        file="world_bank_pipeline.py",
        desc="World Bank global macro — GDP, inflation, trade, labor for 30+ countries",
        stage=1,
        tables=["world_bank"],
        backfill_args=["--backfill"],
        timeout=900,
    ),
    # These five sources were live and healthy but had no spec here, so a full
    # run_all.py never touched them and they quietly froze between 2026-06-17
    # and 2026-07-02 (found 2026-08-11). See tests/test_catalog.py's
    # orphaned-table guard, which now makes that omission fail the suite.
    PipelineSpec(
        name="worldbank_pink_sheet",
        file="worldbank_pink_sheet.py",
        desc="World Bank Pink Sheet monthly commodity prices — 71 commodities back to 1960",
        stage=1,
        tables=["wb_commodities"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="imf_commodities",
        file="imf_commodities_pipeline.py",
        desc="IMF Primary Commodity Prices — 14 series across 4 categories",
        stage=1,
        tables=["imf_commodities"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="metals",
        file="metals_pipeline.py",
        desc="Metals spot prices (FRED) — copper, aluminum, nickel, lead, iron ore, tin",
        stage=1,
        tables=["metals_spot"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="fao",
        file="fao_pipeline.py",
        desc="FAO FAOSTAT producer prices + production; falls back to bulk ZIP when the REST API times out",
        stage=1,
        tables=["fao_prices", "fao_production"],
        backfill_args=["--backfill"],
        timeout=900,
    ),
    PipelineSpec(
        name="plastics",
        file="plastics_pipeline.py",
        desc="OWID global plastics production (keyless static historical series, 1950-2019)",
        stage=1,
        tables=["plastics_production"],
        backfill_args=["--backfill"],
        timeout=120,
    ),
    PipelineSpec(
        name="cfpb_complaints",
        file="cfpb_complaints_pipeline.py",
        desc="CFPB consumer complaint database -- full bulk CSV snapshot re-downloaded each run (~17M rows, ~1.4GB compressed)",
        stage=1,
        tables=["cfpb_complaints"],
        backfill_args=["--backfill"],
        timeout=1800,
    ),
    PipelineSpec(
        name="yahoo_options",
        file="yahoo_options_pipeline.py",
        desc="Yahoo per-contract options OHLCV history (NVDA/PLTR/MSFT/AAPL) — feeds put_call_ratio",
        # Stage 1, not 2: stage 2 is the Schwab-authenticated block, and this
        # source needs no credentials and no upstream table.
        stage=1,
        tables=["options_history"],
        # Phase 2 is one HTTP request per contract, so the incremental run is
        # bounded on both axes: only the last 5 sessions, only contracts with
        # real open interest. curated dedups on
        # (symbol, expiration_date, strike_price, contract_type, date), so
        # overlapping short pulls accumulate cleanly. Measured 2026-08-11:
        # ~8 min for AAPL alone at --min-oi 500 (1,158 of 2,694 contracts).
        incremental_args=["--range", "5d", "--min-oi", "500"],
        backfill_args=["--range", "max"],
        timeout=3600,
    ),
    PipelineSpec(
        name="simfin",
        file="simfin_pipeline.py",
        desc="SimFin income statements, balance sheets, cash flow for watchlist symbols",
        stage=1,
        tables=["simfin_income", "simfin_balance", "simfin_cashflow"],
        requires_env=["SIMFIN_API_KEY"],
        timeout=900,
    ),
    PipelineSpec(
        name="tiingo",
        file="tiingo_pipeline.py",
        desc="Tiingo clean adjusted EOD prices + ticker-tagged news for watchlist",
        stage=1,
        tables=["tiingo_prices", "tiingo_news"],
        requires_env=["TIINGO_API_KEY"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="institutional",
        file="institutional_pipeline.py",
        desc="SEC 13F institutional holdings for top 18 asset managers",
        stage=1,
        tables=["institutional_holdings"],
        requires_env=["EDGAR_USER_AGENT"],
        backfill_args=["--backfill"],
        timeout=900,
    ),
    PipelineSpec(
        name="noaa_climate",
        file="noaa_climate_pipeline.py",
        desc="NOAA NCEI monthly climate summaries for 15 US agricultural stations (keyless)",
        stage=1,
        tables=["noaa_climate"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="usda",
        file="usda_pipeline.py",
        desc="USDA NASS crop production (8 field crops) and fertilizer prices paid",
        stage=1,
        tables=["usda_crops", "usda_fertilizers"],
        requires_env=["USDA_NASS_API_KEY"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="trade",
        file="trade_pipeline.py",
        desc="US Census imports and exports by HTS chapter (8 chapters: ag, lumber, steel)",
        stage=1,
        tables=["us_imports_hs", "us_exports_hs"],
        requires_env=["CENSUS_API_KEY"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="eia",
        file="eia_pipeline.py",
        desc="EIA weekly petroleum inventories/refinery activity/crude trade, natural gas storage, monthly crude production",
        stage=1,
        tables=["eia_petroleum_stocks", "eia_natgas_storage", "eia_crude_production",
                "eia_refinery_activity", "eia_crude_trade"],
        requires_env=["EIA_API_KEY"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="stockanalysis",
        file="stockanalysis_pipeline.py",
        desc="Stock Analysis movers, IPOs, corporate actions, reference lists, per-symbol financials",
        stage=1,
        tables=[
            "sa_movers", "sa_ipos", "sa_ipo_calendar", "sa_ipo_stats",
            "sa_corporate_actions", "sa_stock_list", "sa_etf_list",
            "sa_income", "sa_balance", "sa_cashflow", "sa_ratios",
        ],
        timeout=1800,   # financials for ~45 symbols × 4 stmts × 2 periods
    ),
    PipelineSpec(
        name="finviz",
        file="finviz_pipeline.py",
        desc="Finviz market movers, S&P 500 screener, financials, insider trades, sector/group data",
        stage=1,
        tables=[
            "finviz_movers", "finviz_screener", "finviz_financials", "finviz_insider",
            "finviz_sector_perf", "finviz_industry_perf", "finviz_country_perf",
            "finviz_group_valuation",
        ],
    ),
    # ── Stage 1 — New free/public sources ──────────────────────────────────────
    PipelineSpec(
        name="coingecko",
        file="coingecko_pipeline.py",
        desc="CoinGecko top-250 crypto market snapshot + OHLCV history",
        stage=1,
        tables=["crypto_market", "crypto_history"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="forex",
        file="forex_pipeline.py",
        desc="Frankfurter ECB forex rates, 19 currencies vs USD (keyless)",
        stage=1,
        tables=["forex_rates"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="bea",
        file="bea_pipeline.py",
        desc="BEA NIPA tables: GDP, personal income, corporate profits",
        stage=1,
        tables=["bea_gdp", "bea_income", "bea_profits"],
        requires_env=["BEA_API_KEY"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="oecd",
        file="oecd_pipeline.py",
        desc="OECD MEI macro indicators for 14 major economies (keyless)",
        stage=1,
        tables=["oecd_macro"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="congressional_trades",
        file="congressional_trades_pipeline.py",
        desc="US House and Senate STOCK Act periodic transaction reports -- House Clerk + Senate eFD (keyless)",
        stage=1,
        tables=["congressional_trades"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="california_disclosures",
        file="california_disclosures_pipeline.py",
        desc="California legislature Form 700 Schedule A-1 investments -- FPPC eRetrieval (keyless)",
        stage=1,
        tables=["california_disclosures"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="patents",
        file="patents_pipeline.py",
        desc="USPTO PatentsView grants across 6 tech sectors (keyless)",
        stage=1,
        tables=["patents"],
        backfill_args=["--backfill"],
        timeout=1200,
    ),
    PipelineSpec(
        name="ecb",
        file="ecb_pipeline.py",
        desc="ECB policy rates, Euribor, yield curve, HICP inflation (keyless)",
        stage=1,
        tables=["ecb_rates"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="usgs_minerals",
        file="usgs_minerals_pipeline.py",
        desc="USGS DS-140 critical mineral statistics -- lithium, cobalt, graphite, rare earths",
        stage=1,
        tables=["usgs_minerals"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="usgs_helium_mcs",
        file="usgs_helium_mcs_pipeline.py",
        desc="USGS MCS helium + rare gases annual data releases via ScienceBase (keyless)",
        stage=1,
        tables=["usgs_mcs_helium"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="usgs_ds140",
        file="usgs_ds140_pipeline.py",
        desc="USGS Data Series 140 helium historical statistics 1935+ (keyless, hash-gated)",
        stage=1,
        tables=["usgs_ds140_helium"],
        timeout=120,
    ),
    # Unwired 2026-07-26: requires OMKAR_API_KEY, never set, pipeline never run.
    # Re-add if the key is obtained -- see omkar_commodity_pipeline.py.
    # PipelineSpec(
    #     name="omkar_commodity",
    #     file="omkar_commodity_pipeline.py",
    #     desc="Omkar Cloud commodity futures prices -- 30 CME/NYMEX commodities (free 100 req/mo)",
    #     stage=1,
    #     tables=["omkar_commodity"],
    #     requires_env=["OMKAR_API_KEY"],
    #     backfill_args=["--backfill"],
    #     timeout=120,
    # ),
    PipelineSpec(
        name="comtrade",
        file="comtrade_pipeline.py",
        desc="UN Comtrade — US import/export flows for battery materials and components",
        stage=1,
        tables=["comtrade_trade"],
        requires_env=["COMTRADE_API_KEY"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="gem_trackers",
        file="gem_trackers_pipeline.py",
        desc="Global Energy Monitor tracker summary tables via public Google Sheets (keyless)",
        stage=1,
        tables=["gem_coal_summary", "gem_coal_mine_summary", "gem_steel_summary",
                "gem_cement_summary", "gem_oilgas_summary", "gem_lng_summary"],
        timeout=1800,
    ),
    # ── Stage 1 — Quant / valuation / volatility / banking ─────────────────────
    PipelineSpec(
        name="fama_french",
        file="fama_french_pipeline.py",
        desc="Fama-French 5-factor + momentum returns + 48 industry portfolios (keyless)",
        stage=1,
        tables=["ff_factors", "ff_industry"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="shiller",
        file="shiller_pipeline.py",
        desc="Shiller CAPE long-run S&P 500 valuation back to 1871 (keyless)",
        stage=1,
        tables=["shiller_cape"],
        backfill_args=["--backfill"],
        timeout=120,
    ),
    PipelineSpec(
        name="cboe",
        file="cboe_pipeline.py",
        desc="CBOE VIX, VIX9D, VIX3M, VIX6M, VVIX, SKEW daily OHLC (keyless)",
        stage=1,
        tables=["cboe_volatility"],
        backfill_args=["--backfill"],
        timeout=120,
    ),
    PipelineSpec(
        name="fdic",
        file="fdic_pipeline.py",
        desc="FDIC bank institutions, quarterly financials, and failure history (keyless)",
        stage=1,
        tables=["fdic_institutions", "fdic_financials", "fdic_failures"],
        backfill_args=["--backfill"],
        timeout=1800,
    ),
    PipelineSpec(
        name="fear_greed",
        file="fear_greed_pipeline.py",
        desc="Crypto Fear & Greed Index daily composite sentiment (Alternative.me, keyless)",
        stage=1,
        tables=["fear_greed"],
        backfill_args=["--backfill"],
        timeout=60,
    ),
    PipelineSpec(
        name="nasdaq_data_link",
        file="nasdaq_data_link_pipeline.py",
        desc="Nasdaq Data Link: S&P 500 valuation metrics + full Treasury yield curve",
        stage=1,
        tables=["market_valuation", "treasury_yield_curve"],
        requires_env=["NASDAQ_DATA_LINK_API_KEY"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="treasury_curve",
        file="treasury_curve_pipeline.py",
        desc="Treasury.gov daily par yield curve - keyless replacement for dead NDL USTREASURY/YIELD",
        stage=1,
        tables=["treasury_yield_curve"],
        backfill_args=["--backfill"],
        timeout=900,
    ),
    PipelineSpec(
        name="usaspending",
        file="usaspending_pipeline.py",
        desc="USAspending federal contracts - keyless replacement for 403'd finnhub_usa_spending",
        stage=1,
        tables=["usaspending_award_counts", "usaspending_top_awards"],
        backfill_args=["--backfill"],
        timeout=900,
    ),
    PipelineSpec(
        name="lda_lobbying",
        file="lda_lobbying_pipeline.py",
        desc="Senate LDA lobbying filings - keyless replacement for 403'd finnhub_lobbying",
        stage=1,
        tables=["lda_lobbying_filings"],
        backfill_args=["--backfill"],
        timeout=1800,
    ),
    PipelineSpec(
        name="defillama",
        file="defillama_pipeline.py",
        desc="DeFi protocol fundamentals: TVL, fees/revenue, stablecoin supply (keyless, snapshot-only)",
        stage=1,
        tables=["defillama_protocols", "defillama_fees", "defillama_stablecoins"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="fed_soma",
        file="fed_soma_pipeline.py",
        desc="NY Fed SOMA weekly balance sheet: Treasury + Agency MBS holdings (keyless)",
        stage=1,
        tables=["fed_soma"],
        backfill_args=["--backfill"],
        timeout=3600,
    ),
    # ── Stage 1 — Alternative / attention / macro signals ──────────────────────
    PipelineSpec(
        name="open_meteo",
        file="open_meteo_pipeline.py",
        desc="Open-Meteo daily weather for 25 US economic locations (keyless)",
        stage=1,
        tables=["open_meteo_weather"],
        backfill_args=["--backfill"],
        timeout=1800,   # 25 locations; each may hit 60s rate-limit pause
    ),
    PipelineSpec(
        name="wikipedia",
        file="wikipedia_pipeline.py",
        desc="Wikipedia daily pageviews — investor attention signal for DJI + macro (keyless)",
        stage=1,
        tables=["wikipedia_pageviews"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="openfda",
        file="openfda_pipeline.py",
        desc="OpenFDA drug approvals and enforcement recalls — pharma/biotech signals (keyless)",
        stage=1,
        tables=["openfda_approvals", "openfda_recalls"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="treasury_tic",
        file="treasury_tic_pipeline.py",
        desc="Treasury TIC — foreign holdings of US Treasuries and equities by country (keyless)",
        stage=1,
        tables=["treasury_tic_holders", "treasury_tic_slt"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="google_trends",
        file="google_trends_pipeline.py",
        desc="Google Trends search interest for 45 financial keywords across 3 groups (keyless)",
        stage=1,
        tables=["google_trends_economic", "google_trends_market", "google_trends_sector"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="reddit",
        file="reddit_pipeline.py",
        desc="Reddit post volume + ticker mentions across 6 finance subreddits",
        stage=1,
        tables=["reddit_posts", "reddit_mentions"],
        requires_env=["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="ais",
        file="ais_pipeline.py",
        desc="AIS vessel tracking — cargo/tanker positions across 10 trade chokepoints (10-min snapshot)",
        stage=1,
        tables=["ais_positions", "ais_zone_summary"],
        requires_env=["AISSTREAM_API_KEY"],
        timeout=720,   # 10-min window + buffer
    ),
    PipelineSpec(
        name="real_estate",
        file="real_estate_pipeline.py",
        desc="FHFA House Price Index + Zillow ZHVI/ZORI (keyless)",
        stage=1,
        tables=["fhfa_hpi", "zillow_zhvi", "zillow_zori"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="shipping",
        file="shipping_pipeline.py",
        desc="NY Fed GSCPI + FRED multi-modal (marine/rail/truck/air) freight PPI and diesel fuel PPI",
        stage=1,
        tables=["shipping_gscpi", "shipping_freight_ppi"],
        requires_env=["FRED_API_KEY"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="piracy",
        file="piracy_pipeline.py",
        desc="Piracy incidents - ICC IMB live-map archive (global, 2012+, geo) + Wikipedia Somali hijacking log (2005+, dated)",
        stage=1,
        tables=["piracy_incidents", "somali_hijackings"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="yfinance",
        file="yfinance_pipeline.py",
        desc="Yahoo Finance deep daily history — indices, commodity futures, FX, rates ETFs (keyless)",
        stage=1,
        tables=["market_history"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="tradingview",
        file="tradingview_pipeline.py",
        desc="TradingView aggregate technical-rating snapshot for top US stocks + ETFs (keyless)",
        stage=1,
        tables=["tv_ratings"],
        timeout=300,
    ),
    PipelineSpec(
        name="sec_filings",
        file="sec_filings_pipeline.py",
        desc="SEC EDGAR daily filing index — 8-K, 10-K/Q, S-1, SC 13D/G, DEF 14A metadata",
        stage=1,
        tables=["sec_filings"],
        requires_env=["EDGAR_USER_AGENT"],
        backfill_args=["--backfill"],
        timeout=900,
    ),
    PipelineSpec(
        name="tsa",
        file="tsa_pipeline.py",
        desc="TSA daily checkpoint travel volumes (leading air-travel demand indicator)",
        stage=1,
        tables=["tsa_checkpoint"],
        requires_env=[],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    # Unwired 2026-07-26: requires ANTHROPIC_API_KEY, never set, pipeline never run.
    # Re-add if the key is obtained -- see fed_sentiment_pipeline.py.
    # PipelineSpec(
    #     name="fed_sentiment",
    #     file="fed_sentiment_pipeline.py",
    #     desc="Fed speeches/FOMC statements (RSS) scored hawkish/dovish via Claude",
    #     stage=1,
    #     tables=["fed_speeches", "fed_sentiment"],
    #     requires_env=["ANTHROPIC_API_KEY"],
    #     timeout=600,
    # ),
    PipelineSpec(
        name="fred_macro_extended",
        file="fred_macro_pipeline.py",
        desc="FRED macro extended -- housing, sentiment, industrial, consumer, trade",
        stage=1,
        tables=['fred_macro_housing', 'fred_macro_sentiment', 'fred_macro_industrial', 'fred_macro_consumer', 'fred_macro_trade'],
        requires_env=['FRED_API_KEY'],
        backfill_args=['--backfill'],
        timeout=600,
    ),
    PipelineSpec(
        name="fred_rates_gdp",
        file="fred_rates_gdp_pipeline.py",
        desc="FRED rates/GDP/inflation/FX/markets/federal debt (deduped vs commodity_macro)",
        stage=1,
        tables=['fred_rates_gdp_interest_rates', 'fred_rates_gdp_money_supply', 'fred_rates_gdp_gdp', 'fred_rates_gdp_inflation', 'fred_rates_gdp_mortgage', 'fred_rates_gdp_commodities', 'fred_rates_gdp_exchange_rates', 'fred_rates_gdp_markets', 'fred_rates_gdp_federal_debt', 'fred_rates_gdp_labor'],
        requires_env=['FRED_API_KEY'],
        backfill_args=['--backfill'],
        timeout=900,
    ),
    PipelineSpec(
        name="alpha_vantage_fundamentals",
        file="alpha_vantage_fundamentals_pipeline.py",
        desc="Alpha Vantage fundamentals -- overview, statements, earnings, dividends, insider, news, movers",
        stage=1,
        tables=['alpha_vantage_overview', 'alpha_vantage_income_statement', 'alpha_vantage_balance_sheet', 'alpha_vantage_cash_flow', 'alpha_vantage_earnings', 'alpha_vantage_earnings_calendar', 'alpha_vantage_dividends', 'alpha_vantage_insider_transactions', 'alpha_vantage_news_sentiment', 'alpha_vantage_top_gainers_losers'],
        requires_env=['ALPHA_VANTAGE_API_KEY'],
        backfill_args=['--backfill'],
        timeout=900,
    ),
    PipelineSpec(
        name="coingecko_expansion",
        file="coingecko_expansion_pipeline.py",
        desc="CoinGecko expansion -- global market, top coins, trending, categories, derivatives, FX rates",
        stage=1,
        tables=['coingecko_global_market', 'coingecko_coins_markets', 'coingecko_trending', 'coingecko_categories', 'coingecko_derivatives', 'coingecko_exchange_rates'],
        backfill_args=['--backfill'],
        timeout=900,
    ),
    PipelineSpec(
        name="sec_edgar_expansion",
        file="sec_edgar_pipeline.py",
        desc="SEC EDGAR submissions index, XBRL fundamentals, EFTS full-text search",
        stage=1,
        tables=['sec_edgar_submissions', 'sec_edgar_xbrl_fundamentals', 'sec_edgar_efts_search'],
        requires_env=['EDGAR_USER_AGENT'],
        backfill_args=['--backfill'],
        timeout=1800,
    ),
    PipelineSpec(
        name="bls_expansion",
        file="bls_expansion_pipeline.py",
        desc="BLS expansion -- import/export price indexes, ECI, productivity/unit labor costs",
        stage=1,
        tables=['bls_import_export_prices', 'bls_eci', 'bls_productivity'],
        backfill_args=['--backfill'],
        timeout=900,
    ),
    PipelineSpec(
        name="bls_oes_qcew",
        file="bls_oes_qcew_pipeline.py",
        desc="BLS occupational wages (OEWS), QCEW, ECEC, CPS labor-force demographics",
        stage=1,
        tables=['bls_oes', 'bls_qcew', 'bls_ecec', 'bls_cps_demographics'],
        backfill_args=['--backfill'],
        timeout=3600,
    ),
    PipelineSpec(
        name="eia_expansion",
        file="eia_expansion_pipeline.py",
        desc="EIA expansion -- electricity generation/sales, nuclear outages, coal, international energy, SEDS",
        stage=1,
        tables=['eia_electricity_generation', 'eia_electricity_sales', 'eia_nuclear_outages', 'eia_coal_production', 'eia_coal_trade', 'eia_international', 'eia_seds'],
        requires_env=['EIA_API_KEY'],
        backfill_args=['--backfill'],
        timeout=600,
    ),
    PipelineSpec(
        name="eia_petng_prices",
        file="eia_petng_prices_pipeline.py",
        desc="EIA petroleum/natural gas -- spot/futures prices, refiner margins, supply/demand, LNG",
        stage=1,
        tables=['eia_petroleum_spot_prices', 'eia_petroleum_futures', 'eia_refiner_margins', 'eia_petroleum_supply_demand', 'eia_natural_gas_consumption', 'eia_natural_gas_prices', 'eia_natural_gas_production', 'eia_lng_flows'],
        requires_env=['EIA_API_KEY'],
        backfill_args=['--backfill'],
        timeout=600,
    ),
    PipelineSpec(
        name="eia_hourly_grid",
        file="eia_hourly_grid_pipeline.py",
        desc="EIA-930 hourly grid monitor -- demand, forecast, net generation, interchange (65+ balancing authorities)",
        stage=1,
        tables=['eia_hourly_grid'],
        requires_env=['EIA_API_KEY'],
        backfill_args=['--backfill'],
        timeout=1800,
    ),
    PipelineSpec(
        name="index_constituents",
        file="index_constituents_pipeline.py",
        desc="Index constituents -- S&P 500, Nasdaq-100, Russell 3000/2000, Wilshire 5000 (Iceberg table)",
        stage=1,
        tables=['index_members'],
        backfill_args=['--backfill'],
        timeout=300,
    ),
    PipelineSpec(
        name="securities_reference",
        file="securities_reference_pipeline.py",
        desc="Securities reference table -- SEC EDGAR + Finnhub + index membership flags (Iceberg table)",
        stage=1,
        tables=['securities'],
        requires_env=['FINNHUB_API_KEY'],
        backfill_args=['--skip-finnhub'],
        incremental_args=['--skip-finnhub'],
        timeout=600,
    ),
    PipelineSpec(
        name="fund_holdings",
        file="fund_holdings_pipeline.py",
        desc="Fund holdings -- iShares ETFs (BlackRock API) + mutual funds (EdgarTools N-PORT) (Iceberg table)",
        stage=1,
        tables=['fund_holdings'],
        # EdgarTools N-PORT fetches run ~55s/fund (network+XML parse, not our
        # rate limiting) x 53 mutual funds = ~49min alone; 1200s was timing out
        # every night before the ETF/bond legs even started. First fix (3600s)
        # STILL wasn't enough -- measured live 2026-08-22: fetch phase (52 ETF
        # + 4 bond + 53 MF) took ~54min end to end, then the batched per-fund
        # Iceberg write phase (~8s/fund x ~105 funds) needs another ~14min on
        # top -- real total ~68min. 5400s (90min) leaves real headroom this
        # time. See daily_pipelines_2026-08-21/-22 and
        # storage/logs/failures/fund_holdings_20260822_204418.log.
        timeout=5400,
    ),
    PipelineSpec(
        name="etf_holdings",
        file="etf_holdings_pipeline.py",
        desc="ETF holdings -- SecuritiesDB free ETF holdings (200+ US ETFs, no auth) (Iceberg table)",
        stage=1,
        tables=['etf_holdings'],
        timeout=1800,
    ),
    PipelineSpec(
        name="finnhub_expansion",
        file="finnhub_expansion_pipeline.py",
        desc="Finnhub alt-data expansion -- ESG, lobbying, patents, econ calendar, etc",
        stage=1,
        tables=['finnhub_esg', 'finnhub_supply_chain', 'finnhub_insider_sentiment', 'finnhub_social_sentiment', 'finnhub_sec_filings', 'finnhub_earnings_quality', 'finnhub_lobbying', 'finnhub_usa_spending', 'finnhub_uspto_patents', 'finnhub_visa_applications', 'finnhub_economic_calendar'],
        requires_env=['FINNHUB_API_KEY'],
        backfill_args=['--backfill'],
        timeout=1200,
    ),
    PipelineSpec(
        name="finnhub_fundamentals",
        file="finnhub_fundamentals_pipeline.py",
        desc="Finnhub fundamentals -- earnings, estimates, ownership, splits, peers, executives, transcripts",
        stage=1,
        tables=['finnhub_earnings_history', 'finnhub_eps_estimates', 'finnhub_revenue_estimates', 'finnhub_ownership', 'finnhub_splits', 'finnhub_peers', 'finnhub_executives', 'finnhub_filing_sentiment', 'finnhub_transcripts', 'finnhub_company_news_sentiment'],
        requires_env=['FINNHUB_API_KEY'],
        backfill_args=['--backfill'],
        timeout=900,
    ),
    PipelineSpec(
        name="tiingo_corporate_actions",
        file="tiingo_corporate_actions_pipeline.py",
        desc="Tiingo corporate actions -- dividends, splits, distribution yield",
        stage=1,
        tables=['tiingo_corporate_actions_dividends', 'tiingo_corporate_actions_splits', 'tiingo_corporate_actions_yield'],
        requires_env=['TIINGO_API_KEY'],
        backfill_args=['--backfill'],
        timeout=600,
    ),
    PipelineSpec(
        name="tiingo_fundamentals",
        file="tiingo_fundamentals_pipeline.py",
        desc="Tiingo fundamentals -- daily valuation metrics + quarterly/annual statements (long format)",
        stage=1,
        tables=['tiingo_fundamentals_daily', 'tiingo_fundamentals_statements'],
        requires_env=['TIINGO_API_KEY'],
        backfill_args=['--backfill'],
        timeout=600,
    ),
    PipelineSpec(
        name="treasury_fiscal",
        file="treasury_fiscal_pipeline.py",
        desc="US Treasury Fiscal Data -- debt to penny, rates, auctions, FX, savings bonds, MTS/DTS",
        stage=1,
        tables=['treasury_debt_to_penny', 'treasury_avg_interest_rates', 'treasury_interest_expense', 'treasury_auctions_detail', 'treasury_exchange_rates', 'treasury_savings_bonds', 'treasury_mts_receipts_outlays', 'treasury_mts_outlays_by_agency', 'treasury_dts_operating_cash', 'treasury_mts_budget_comparison'],
        backfill_args=['--backfill'],
        timeout=1800,
    ),
    PipelineSpec(
        name="dark_pool",
        file="dark_pool_pipeline.py",
        desc="FINRA dark pool (ATS) daily aggregate trading volume",
        stage=1,
        tables=["dark_pool_volume"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="retail_sentiment",
        file="retail_sentiment_pipeline.py",
        desc="Retail investor sentiment from Stocktwits bullish/bearish counts",
        stage=1,
        tables=["retail_sentiment", "retail_sentiment_daily"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="insider_sentiment",
        file="insider_sentiment_pipeline.py",
        desc="Insider transaction sentiment from SEC EDGAR Form 4 filings",
        stage=1,
        tables=["insider_sentiment"],
        requires_env=["EDGAR_USER_AGENT"],
        backfill_args=["--backfill"],
        timeout=600,
    ),
    PipelineSpec(
        name="indeed_hiringlab",
        file="indeed_hiringlab_pipeline.py",
        desc="Indeed Hiring Lab job-postings index (national/sector/state, keyless)",
        stage=1,
        tables=["indeed_job_postings_national", "indeed_job_postings_sector", "indeed_job_postings_state"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    # ── Stage 2 — Schwab-authenticated ─────────────────────────────────────────
    PipelineSpec(
        name="openfigi",
        file="openfigi_pipeline.py",
        desc="OpenFIGI identifier resolution -- tickers to FIGI/Composite FIGI (Iceberg table)",
        stage=2,
        tables=['identifier_map'],
        timeout=600,
    ),
    PipelineSpec(
        name="prices",
        file="price_history_pipeline.py",
        desc="Schwab daily OHLCV for DJI components",
        stage=2,
        tables=["prices"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="sector_etfs",
        file="sector_etf_pipeline.py",
        desc="Schwab daily OHLCV for SPDR sector ETFs + broad indexes",
        stage=2,
        tables=["sector_etfs"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="schwab_quotes",
        file="schwab_quotes_pipeline.py",
        desc="Schwab real-time quote snapshot (S&P 500 + sector ETFs)",
        stage=2,
        tables=["schwab_quotes"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
    ),
    PipelineSpec(
        name="schwab_options",
        file="schwab_options_pipeline.py",
        desc="Schwab options chains with full greeks (delta/gamma/theta/vega/rho)",
        stage=2,
        tables=["schwab_options"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
        timeout=1800,   # ~507-symbol S&P 500 universe measured ~24min live 2026-08-02
    ),
    PipelineSpec(
        name="options_chain",
        file="options_chain_pipeline.py",
        desc="Schwab options metrics and chain snapshot",
        stage=2,
        tables=["options_metrics", "options_chain"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
    ),
    PipelineSpec(
        name="schwab_intraday",
        file="schwab_intraday_pipeline.py",
        desc="Schwab 5-min intraday bars (S&P 500)",
        stage=2,
        tables=["schwab_intraday"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
        backfill_args=["--backfill"],
        timeout=900,
    ),
    PipelineSpec(
        name="schwab_movers",
        file="schwab_movers_pipeline.py",
        desc="Schwab top-10 movers snapshot ($SPX/$COMPX/$DJI, up/down/volume)",
        stage=2,
        tables=["schwab_movers"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
    ),
    PipelineSpec(
        name="schwab_portfolio",
        file="schwab_portfolio_pipeline.py",
        desc="Schwab account mirror — daily positions + transactions",
        stage=2,
        tables=["schwab_positions", "schwab_transactions"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
        backfill_args=["--backfill"],
    ),
    # ── Stage 3 — Derived (depends on Stage 1/2 output) ────────────────────────
    PipelineSpec(
        name="synthetic_options",
        file="synthetic_options_pipeline.py",
        desc="BSM/BS2002 synthetic option pricing (requires prices table)",
        stage=3,
        tables=["synthetic_options"],
        backfill_args=["--backfill"],
        timeout=1200,
    ),
    PipelineSpec(
        name="news_sentiment",
        file="news_sentiment_pipeline.py",
        desc="Local VADER sentiment scoring of Finnhub news (requires finnhub table)",
        stage=3,
        tables=["news_sentiment"],
        requires_env=[],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="alpha_vantage",
        file="alpha_vantage_pipeline.py",
        desc="Alpha Vantage technical indicators (RSI/MACD/BB/ADX etc.) + forex rates",
        stage=3,
        tables=["alpha_vantage_technical", "alpha_vantage_forex"],
        requires_env=["ALPHA_VANTAGE_API_KEY"],
        timeout=1200,  # paced to respect 25 calls/day free tier
    ),
    PipelineSpec(
        name="signal_monitor",
        file="signal_monitor.py",
        desc="TA-rating signal health tracker — win rate/PF/CAR by window, flags decline",
        stage=3,
        tables=["signal_health"],
        timeout=1800,
    ),
]


# ── Run result ─────────────────────────────────────────────────────────────────

@dataclass
class RunResult:
    name:     str
    status:   str    # PASS | FAIL | SKIP | DRY RUN
    duration: float  # seconds
    note:     str    # skip reason or error context
    val_warnings: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_env(spec: PipelineSpec) -> str | None:
    """Return a skip reason if any required env var is missing, else None."""
    missing = [v for v in spec.requires_env if not os.environ.get(v)]
    if missing:
        return f"missing env: {', '.join(missing)}"
    return None


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def run_pipeline(
    spec: PipelineSpec,
    backfill: bool,
    dry_run: bool,
    validate: bool,
) -> RunResult:
    skip_reason = _check_env(spec)
    if skip_reason:
        print(f"  SKIP -- {skip_reason}")
        return RunResult(spec.name, "SKIP", 0.0, skip_reason)

    script = os.path.join(REPO_ROOT, spec.file)
    if not os.path.exists(script):
        reason = f"{spec.file} not found"
        print(f"  SKIP -- {reason}")
        return RunResult(spec.name, "SKIP", 0.0, reason)

    cmd = [sys.executable, script]
    cmd += spec.backfill_args if backfill else spec.incremental_args

    if dry_run:
        cmd_str = " ".join(os.path.basename(c) if i < 2 else c for i, c in enumerate(cmd))
        print(f"  DRY RUN: {cmd_str}")
        return RunResult(spec.name, "DRY RUN", 0.0, cmd_str)

    t0 = time.time()
    # Schwab pipelines need a live terminal for interactive OAuth re-auth (~30s
    # window) -- capturing their output would hide the prompt until exit/timeout.
    interactive = "SCHWAB_API_KEY" in spec.requires_env
    try:
        result = subprocess.run(
            cmd, timeout=spec.timeout, capture_output=not interactive,
            text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        duration = time.time() - t0
        output = (result.stdout or "") + (result.stderr or "")
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        if result.returncode != 0:
            log.error("%s failed: exit %d", spec.name, result.returncode)
            fail_path = log_pipeline_failure(
                spec.name,
                output or "(interactive pipeline -- output streamed to console, not captured)")
            return RunResult(spec.name, "FAIL", duration,
                              f"exit {result.returncode} -- log: {fail_path}")
    except subprocess.TimeoutExpired as exc:
        duration = time.time() - t0
        output = (exc.stdout or "") + (exc.stderr or "")
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        log.error("%s timed out after %ds", spec.name, spec.timeout)
        fail_path = log_pipeline_failure(
            spec.name, output or "(no output captured before timeout)")
        return RunResult(spec.name, "FAIL", duration,
                          f"timed out after {spec.timeout}s -- log: {fail_path}")
    except Exception as exc:
        duration = time.time() - t0
        log.exception("%s raised an unexpected error before completing", spec.name)
        fail_path = log_pipeline_failure(spec.name, traceback.format_exc())
        return RunResult(spec.name, "FAIL", duration, f"{exc} -- log: {fail_path}")

    # Post-run validation
    val_warnings = 0
    if validate and spec.tables:
        for table in spec.tables:
            vr = validate_table(table)
            if not vr.passed:
                print(f"\n  [VALIDATE] {table}: {len(vr.errors)} error(s)")
                for c in vr.errors:
                    print(f"    {c}")
            elif vr.warnings:
                val_warnings += len(vr.warnings)

    return RunResult(spec.name, "PASS", duration, "", val_warnings)


# ── Summary ────────────────────────────────────────────────────────────────────

def _print_summary(results: list[RunResult], backfill: bool, start_time: float) -> None:
    mode      = "BACKFILL" if backfill else "INCREMENTAL"
    wall_time = _fmt_duration(time.time() - start_time)
    now       = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    icons = {"PASS": "+", "FAIL": "!", "SKIP": "-", "DRY RUN": "?"}

    print(f"\n{'=' * 62}")
    print(f"  Run Summary -- {mode} -- {now}  ({wall_time} total)")
    print(f"{'=' * 62}")

    for r in results:
        icon = icons.get(r.status, "?")
        dur  = _fmt_duration(r.duration) if r.duration else "-"
        warn = f"  [{r.val_warnings} val warn]" if r.val_warnings else ""
        note = f"  {r.note}" if r.note and r.status not in ("PASS",) else ""
        print(f"  {icon} {r.status:8s}  {r.name:28s}  {dur:>6s}{warn}{note}")

    pass_n  = sum(1 for r in results if r.status == "PASS")
    fail_n  = sum(1 for r in results if r.status == "FAIL")
    skip_n  = sum(1 for r in results if r.status == "SKIP")
    total_w = sum(r.val_warnings for r in results)

    print(f"\n  {pass_n} PASS  |  {fail_n} FAIL  |  {skip_n} SKIP", end="")
    if total_w:
        print(f"  |  {total_w} validation warning(s)", end="")
    print()

    log_fn = log.warning if fail_n else log.info
    log_fn("%s run complete (%s): %d PASS, %d FAIL, %d SKIP, %d validation warning(s)",
           mode, wall_time, pass_n, fail_n, skip_n, total_w)
    for r in results:
        if r.status == "FAIL":
            log.warning("  FAILED: %s -- %s", r.name, r.note)


# ── Curated compaction ───────────────────────────────────────────────────────

def compact_curated(passed_specs: list[PipelineSpec]) -> None:
    """
    Rebuild deduplicated curated snapshots for the tables that just ran.

    Pipelines append a fresh dated Parquet file each run; left alone, the query
    layer would glob those alongside every prior file and double-count rows.
    Compacting here keeps storage/curated/ (which query.py reads by default)
    in sync with the raw layer after every run. Only tables whose pipeline
    PASSed are touched — no point re-reading unchanged tables.
    """
    tables = sorted({t for spec in passed_specs for t in spec.tables})
    if not tables:
        return

    print(f"\n-- Curated Compaction ({len(tables)} table(s)) --")
    try:
        df = curated.compact_all(tables=tables, verbose=True)
    except Exception as exc:  # noqa: BLE001 — never let compaction sink a run
        print(f"  ! compaction error: {exc}")
        return
    if df is not None and not df.empty:
        removed = int(df["removed"].sum())
        print(f"  Compacted {len(df)} table(s); removed {removed:,} duplicate row(s).")


def sync_huggingface(
    has_new_data: bool,
    compact_enabled: bool,
    dry_run: bool,
    hf_sync_enabled: bool,
) -> RunResult:
    """
    Push the curated snapshot (the full storage/curated/ folder) to the
    public HuggingFace dataset and verify the upload actually landed remotely.

    Gated on compact_enabled -- i.e. we never sync before curated compaction
    has had a chance to run at all this session. That guarantee is narrower
    than it may sound: compact_curated() only recompacts tables whose
    pipeline PASSed *this specific run*, but the sync afterward uploads ALL
    curated tables, most of which were NOT touched this run. So the ordering
    protects the tables that ran this run from being published stale -- it
    does not mean every table in the published snapshot was just freshly
    recompacted. Long-term freshness of untouched tables still rests on
    curated.dedup()'s own key-uniqueness guarantee (see tests/test_curated.py).
    This function adds no new duplicate-detection logic of its own.
    """
    if dry_run:
        return RunResult("hf_sync", "SKIP", 0.0, "dry run, skipping sync")
    if not hf_sync_enabled:
        return RunResult("hf_sync", "SKIP", 0.0, "--no-hf-sync set, skipping sync")
    if not compact_enabled:
        return RunResult("hf_sync", "SKIP", 0.0, "--no-compact set, skipping sync")
    if not has_new_data:
        return RunResult("hf_sync", "SKIP", 0.0, "no pipeline passed, nothing new to sync")
    if not (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")):
        return RunResult("hf_sync", "SKIP", 0.0, "no HF_TOKEN/HUGGINGFACE_TOKEN set")

    start = time.time()
    try:
        import upload_huggingface
        from huggingface_hub import HfApi

        stats = upload_huggingface.main()
        if stats is None:
            return RunResult(
                "hf_sync", "FAIL", time.time() - start,
                "upload_huggingface.main() returned no stats",
            )
        if not stats.get("tables") or not stats.get("files"):
            # Second, independent guard: upload_huggingface.main() already refuses
            # to publish when storage/curated/ has zero parquet files, but if that
            # guard is ever bypassed or changed, don't let an empty stats dict
            # vacuously PASS here (missing = [] when stats["files"] == []).
            return RunResult(
                "hf_sync", "FAIL", time.time() - start,
                "no curated parquet files found, refusing to publish",
            )

        remote_files = set(HfApi().list_repo_files(stats["repo_id"], repo_type="dataset"))
        missing = sorted(f for f in stats["files"] if f not in remote_files)
        duration = time.time() - start

        if missing:
            shown = ", ".join(missing[:5])
            suffix = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
            note = f"{len(missing)} table(s) missing remotely: {shown}{suffix}"
            return RunResult("hf_sync", "FAIL", duration, note)

        print("\n-- HuggingFace Sync --")
        print(f"  {stats['tables']} tables, {stats['rows']:,} rows, "
              f"{stats['size_mb']:.1f} MB, verified remotely.")
        return RunResult("hf_sync", "PASS", duration, "")
    except Exception as exc:  # noqa: BLE001 -- never let HF sync sink a run
        return RunResult("hf_sync", "FAIL", time.time() - start, f"sync error: {exc}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run all financial data pipelines in dependency order.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Pass --backfill to every pipeline that supports it.",
    )
    parser.add_argument(
        "--stage", type=int, choices=[1, 2, 3],
        help="Run only pipelines in the given stage (1=free, 2=Schwab, 3=derived).",
    )
    parser.add_argument(
        "--only",
        help="Comma-separated pipeline names to run (e.g. commodity_macro,finnhub).",
    )
    parser.add_argument(
        "--skip",
        help="Comma-separated pipeline names to skip.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would run without executing.",
    )
    parser.add_argument(
        "--no-validate", action="store_true",
        help="Skip post-run validation checks.",
    )
    parser.add_argument(
        "--no-compact", action="store_true",
        help="Skip post-run curated compaction (dedup of the tables that ran).",
    )
    parser.add_argument(
        "--no-hf-sync", action="store_true",
        help="Skip post-run HuggingFace dataset sync.",
    )
    args = parser.parse_args()

    # Build filtered pipeline list
    pipelines = list(PIPELINES)
    if args.stage:
        pipelines = [p for p in pipelines if p.stage == args.stage]
    if args.only:
        only_set  = {n.strip() for n in args.only.split(",")}
        pipelines = [p for p in pipelines if p.name in only_set]
        unknown   = only_set - {p.name for p in PIPELINES}
        if unknown:
            print(f"Warning: unknown pipeline names in --only: {sorted(unknown)}")
    if args.skip:
        skip_set  = {n.strip() for n in args.skip.split(",")}
        pipelines = [p for p in pipelines if p.name not in skip_set]
        unknown   = skip_set - {p.name for p in PIPELINES}
        if unknown:
            print(f"Warning: unknown pipeline names in --skip: {sorted(unknown)}")

    if not pipelines:
        print("No pipelines selected. Check --stage / --only / --skip arguments.")
        return 1

    mode = "BACKFILL" if args.backfill else "INCREMENTAL"
    validate = not args.no_validate
    compact = not args.no_compact
    start_time = time.time()

    print(f"\n{'=' * 62}")
    print(f"  Financial Data Pipeline Runner")
    print(f"  Mode: {mode}  |  Pipelines: {len(pipelines)}  |  "
          f"Validate: {validate}  |  Compact: {compact}")
    print(f"{'=' * 62}")

    # Stage-grouped run
    current_stage = 0
    results: list[RunResult] = []

    for spec in pipelines:
        if spec.stage != current_stage:
            current_stage = spec.stage
            labels = {1: "Free / Public Sources", 2: "Schwab Authenticated", 3: "Derived Pipelines"}
            print(f"\n-- Stage {current_stage}: {labels.get(current_stage, '')} --")

        print(f"\n>>  {spec.name}  --  {spec.desc}")
        result = run_pipeline(spec, args.backfill, args.dry_run, validate)
        results.append(result)

    # Rebuild curated snapshots for the tables that ran, so the query layer
    # (which prefers curated files) stays in sync with the new raw files.
    if compact and not args.dry_run:
        spec_by_name = {p.name: p for p in PIPELINES}
        passed_specs = [spec_by_name[r.name] for r in results if r.status == "PASS"]
        compact_curated(passed_specs)

    has_new_data = any(r.status == "PASS" for r in results)
    hf_result = sync_huggingface(
        has_new_data=has_new_data,
        compact_enabled=compact,
        dry_run=args.dry_run,
        hf_sync_enabled=not args.no_hf_sync,
    )
    results.append(hf_result)

    _print_summary(results, args.backfill, start_time)

    # hf_sync is excluded from the exit-code computation (but still shown in the
    # summary table above): an HF-side problem (rate limit, transient network
    # error, expired token) is not a data-collection failure, and the daily
    # accumulator scheduled task treats any nonzero exit code here as "the whole
    # run failed" (see AUTOMATION.md) -- we don't want an HF hiccup to trip that
    # alarm when real pipeline data collection succeeded.
    return 0 if all(r.status in ("PASS", "SKIP", "DRY RUN")
                     for r in results if r.name != "hf_sync") else 1


if __name__ == "__main__":
    sys.exit(main())
