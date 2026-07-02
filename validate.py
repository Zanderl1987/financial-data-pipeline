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
        "required":    ["symbol", "metric", "value", "period_end"],
        "critical_nn": ["symbol", "metric"],
        "date_col":    "period_end",
    },
    "fundamentals_quarterly": {
        "required":    ["symbol", "metric", "value", "period_end"],
        "critical_nn": ["symbol", "metric"],
        "date_col":    "period_end",
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
        "required":    ["series", "date", "price_usd_gallon"],
        "critical_nn": ["series", "date", "price_usd_gallon"],
        "date_col":    "date",
        "value_ranges": {"price_usd_gallon": (0, 20)},
    },
    "gas_retail": {
        "required":    ["duoarea", "date", "price_usd_gallon"],
        "critical_nn": ["duoarea", "date", "price_usd_gallon"],
        "date_col":    "date",
        "value_ranges": {"price_usd_gallon": (0, 20)},
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
        "required":    ["symbol", "transaction_date"],
        "critical_nn": ["symbol", "transaction_date"],
        "date_col":    "transaction_date",
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
    # ── IMF Primary Commodity Prices ─────────────────────────────────────────
    "imf_commodities": {
        "required":    ["series_id", "date", "value", "category"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    # ── Metals spot prices ───────────────────────────────────────────────────
    "metals_spot": {
        "required":    ["series_id", "date", "value"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    # ── FAO FAOSTAT ──────────────────────────────────────────────────────────
    "fao_production": {
        "required":    ["country", "commodity", "year", "value"],
        "critical_nn": ["country", "commodity", "value"],
        "date_col":    None,
    },
    "fao_prices": {
        "required":    ["country", "commodity", "year", "value"],
        "critical_nn": ["country", "commodity", "value"],
        "date_col":    None,
    },
    # ── World Bank Pink Sheet commodity prices ────────────────────────────────
    "wb_commodities": {
        "required":    ["commodity", "value", "fetched_at"],
        "critical_nn": ["commodity", "value"],
        "date_col":    "date",
    },
    # ── NOAA NCEI climate (monthly summaries) ────────────────────────────────
    "noaa_climate": {
        "required":    ["station_id", "date", "fetched_at"],
        "critical_nn": ["station_id", "date"],
        "date_col":    "date",
    },
    # ── USDA NASS crop production + fertilizer prices ─────────────────────────
    "usda_crops": {
        "required":    ["commodity", "stat_category", "year", "value", "fetched_at"],
        "critical_nn": ["commodity", "value"],
        "date_col":    "date",
    },
    "usda_fertilizers": {
        "required":    ["commodity", "stat_category", "date", "value", "fetched_at"],
        "critical_nn": ["commodity", "date", "value"],
        "date_col":    "date",
    },
    # ── US Census international trade (HS chapters) ───────────────────────────
    "us_imports_hs": {
        "required":    ["hs2_code", "hs2_desc", "date", "value_mo_usd", "fetched_at"],
        "critical_nn": ["hs2_code", "date"],
        "date_col":    "date",
        "value_ranges": {"value_mo_usd": (0, 1e13)},
    },
    "us_exports_hs": {
        "required":    ["hs2_code", "hs2_desc", "date", "value_mo_usd", "fetched_at"],
        "critical_nn": ["hs2_code", "date"],
        "date_col":    "date",
        "value_ranges": {"value_mo_usd": (0, 1e13)},
    },
    # ── CoinGecko cryptocurrency ──────────────────────────────────────────────
    "crypto_market": {
        "required":    ["coin_id", "symbol", "name", "price_usd", "fetched_at"],
        "critical_nn": ["coin_id", "symbol", "price_usd"],
        "date_col":    None,
        "value_ranges": {"price_usd": (0, 1e9)},
    },
    "crypto_history": {
        "required":    ["coin_id", "symbol", "date", "open", "high", "low", "close"],
        "critical_nn": ["coin_id", "symbol", "date", "close"],
        "date_col":    "date",
    },
    # ── Forex rates (Frankfurter keyless) ────────────────────────────────────
    "forex_rates": {
        "required":    ["base", "currency", "pair", "date", "rate"],
        "critical_nn": ["pair", "date", "rate"],
        "date_col":    "date",
        "value_ranges": {"rate": (0, 1_000_000)},
    },
    # ── BEA national accounts ─────────────────────────────────────────────────
    "bea_gdp": {
        "required":    ["table_id", "line_name", "date", "value", "fetched_at"],
        "critical_nn": ["table_id", "date", "value"],
        "date_col":    "date",
    },
    "bea_income": {
        "required":    ["table_id", "line_name", "date", "value", "fetched_at"],
        "critical_nn": ["table_id", "date", "value"],
        "date_col":    "date",
    },
    "bea_profits": {
        "required":    ["table_id", "line_name", "date", "value", "fetched_at"],
        "critical_nn": ["table_id", "date", "value"],
        "date_col":    "date",
    },
    # ── OECD macro indicators ─────────────────────────────────────────────────
    "oecd_macro": {
        "required":    ["country_code", "indicator", "date", "value", "unit", "fetched_at"],
        "critical_nn": ["country_code", "indicator", "date", "value"],
        "date_col":    "date",
    },
    # ── Congressional trade disclosures ───────────────────────────────────────
    "congressional_trades": {
        "required":    ["chamber", "member_name", "transaction_type", "fetched_at"],
        "critical_nn": ["chamber", "member_name"],
        "date_col":    "transaction_date",
    },
    # ── USPTO patents ─────────────────────────────────────────────────────────
    "patents": {
        "required":    ["patent_id", "patent_date", "sector", "fetched_at"],
        "critical_nn": ["patent_id", "patent_date", "sector"],
        "date_col":    "patent_date",
    },
    # ── ECB rates and Euribor ─────────────────────────────────────────────────
    "ecb_rates": {
        "required":    ["series_id", "series_name", "date", "value", "unit", "fetched_at"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    # ── EIA expanded (petroleum inventories, nat gas storage, crude production)
    "eia_petroleum_stocks": {
        "required":    ["series_id", "series_name", "date", "value", "fetched_at"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
        "value_ranges": {"value": (0, 2_000_000)},   # thousand barrels
    },
    "eia_natgas_storage": {
        "required":    ["duoarea", "date", "value", "fetched_at"],
        "critical_nn": ["duoarea", "date", "value"],
        "date_col":    "date",
        "value_ranges": {"value": (-500, 5_000)},     # BCF; can be negative (net withdrawal)
    },
    "eia_crude_production": {
        "required":    ["series_id", "series_name", "date", "value", "fetched_at"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
        "value_ranges": {"value": (0, 50_000)},       # thousand barrels per day
    },
    # ── USGS DS-140 critical mineral statistics ───────────────────────────────
    "usgs_minerals": {
        "required":    ["commodity", "category", "sheet", "metric", "year", "value", "fetched_at"],
        "critical_nn": ["commodity", "metric", "year", "value"],
        "date_col":    None,
        "value_ranges": {"year": (1900, 2031)},
    },
    # ── Fama-French factor returns ────────────────────────────────────────────
    "ff_factors": {
        "required":    ["date", "frequency", "factor", "value", "source", "fetched_at"],
        "critical_nn": ["date", "frequency", "factor", "value"],
        "date_col":    "date",
    },
    # ── Fama-French 48 industry portfolios ────────────────────────────────────
    "ff_industry": {
        "required":    ["date", "frequency", "weighting", "industry", "return_pct", "fetched_at"],
        "critical_nn": ["date", "industry", "return_pct"],
        "date_col":    "date",
    },
    # ── Shiller CAPE long-run valuation ──────────────────────────────────────
    "shiller_cape": {
        "required":    ["date", "price", "cape", "fetched_at"],
        "critical_nn": ["date", "price"],
        "date_col":    "date",
        "value_ranges": {"cape": (0, 200), "price": (0, 1_000_000)},
    },
    # ── CBOE volatility indices ───────────────────────────────────────────────
    "cboe_volatility": {
        "required":    ["date", "index_name", "close", "fetched_at"],
        "critical_nn": ["date", "index_name", "close"],
        "date_col":    "date",
        "value_ranges": {"close": (0, 400)},
    },
    # ── FDIC bank institutions ────────────────────────────────────────────────
    "fdic_institutions": {
        "required":    ["cert", "instname", "asset", "fetched_at"],
        "critical_nn": ["cert", "instname"],
        "date_col":    None,
    },
    # ── FDIC bank financials ──────────────────────────────────────────────────
    "fdic_financials": {
        "required":    ["cert", "repdte", "asset", "fetched_at"],
        "critical_nn": ["cert", "repdte", "asset"],
        "date_col":    "report_date",
    },
    # ── FDIC bank failures ────────────────────────────────────────────────────
    "fdic_failures": {
        "required":    ["cert", "name", "faildate", "fetched_at"],
        "critical_nn": ["cert", "name", "faildate"],
        "date_col":    "faildate",
    },
    # ── Crypto Fear & Greed Index ─────────────────────────────────────────────
    "fear_greed": {
        "required":    ["date", "value", "classification", "source", "fetched_at"],
        "critical_nn": ["date", "value"],
        "date_col":    "date",
        "value_ranges": {"value": (0, 100)},
    },
    # ── Nasdaq Data Link: S&P 500 valuation metrics ───────────────────────────
    "market_valuation": {
        "required":    ["date", "series", "value", "fetched_at"],
        "critical_nn": ["date", "series", "value"],
        "date_col":    "date",
    },
    # ── Nasdaq Data Link: Treasury yield curve ────────────────────────────────
    "treasury_yield_curve": {
        "required":    ["date", "10yr", "fetched_at"],
        "critical_nn": ["date", "10yr"],
        "date_col":    "date",
        "value_ranges": {"10yr": (0, 25)},
    },
    # ── NY Fed SOMA balance sheet holdings ────────────────────────────────────
    "fed_soma": {
        "required":    ["as_of_date", "asset_type", "fetched_at"],
        "critical_nn": ["as_of_date", "asset_type"],
        "date_col":    "as_of_date",
    },
    # ── UN Comtrade trade flows (battery materials and components) ────────────
    "comtrade_trade": {
        "required":    ["hs_code", "hs_name", "category", "year", "flow", "trade_value_usd", "fetched_at"],
        "critical_nn": ["hs_code", "year", "flow"],
        "date_col":    None,
        "value_ranges": {"trade_value_usd": (0, 5e12)},
    },
    # ── Open-Meteo daily weather ──────────────────────────────────────────────
    "open_meteo_weather": {
        "required":    ["location", "date", "temperature_2m_max", "temperature_2m_min", "fetched_at"],
        "critical_nn": ["location", "date"],
        "date_col":    "date",
    },
    # ── Wikipedia pageviews ───────────────────────────────────────────────────
    "wikipedia_pageviews": {
        "required":    ["article", "date", "views", "fetched_at"],
        "critical_nn": ["article", "date", "views"],
        "date_col":    "date",
        "value_ranges": {"views": (0, 50_000_000)},
    },
    # ── OpenFDA drug approvals + recalls ─────────────────────────────────────
    "openfda_approvals": {
        "required":    ["application_number", "fetched_at"],
        "critical_nn": ["application_number"],
        "date_col":    None,
    },
    "openfda_recalls": {
        "required":    ["recall_number", "fetched_at"],
        "critical_nn": ["recall_number"],
        "date_col":    "recall_initiation_date",
    },
    # ── Treasury TIC foreign holdings ─────────────────────────────────────────
    "treasury_tic_holders": {
        "required":    ["country", "date", "holdings_bn", "fetched_at"],
        "critical_nn": ["country", "date"],
        "date_col":    "date",
        "value_ranges": {"holdings_bn": (0, 10_000)},
    },
    "treasury_tic_slt": {
        "required":    ["fetched_at"],
        "critical_nn": [],
        "date_col":    "date",
    },
    # ── Google Trends search interest ─────────────────────────────────────────
    "google_trends_economic": {
        "required":    ["date", "keyword", "interest", "group", "fetched_at"],
        "critical_nn": ["date", "keyword", "interest"],
        "date_col":    "date",
        "value_ranges": {"interest": (0, 100)},
    },
    "google_trends_market": {
        "required":    ["date", "keyword", "interest", "group", "fetched_at"],
        "critical_nn": ["date", "keyword", "interest"],
        "date_col":    "date",
        "value_ranges": {"interest": (0, 100)},
    },
    "google_trends_sector": {
        "required":    ["date", "keyword", "interest", "group", "fetched_at"],
        "critical_nn": ["date", "keyword", "interest"],
        "date_col":    "date",
        "value_ranges": {"interest": (0, 100)},
    },
    # ── Reddit sentiment ──────────────────────────────────────────────────────
    "reddit_posts": {
        "required":    ["post_id", "subreddit", "title", "date", "fetched_at"],
        "critical_nn": ["post_id", "subreddit", "date"],
        "date_col":    "date",
    },
    "reddit_mentions": {
        "required":    ["date", "subreddit", "ticker", "mention_count", "fetched_at"],
        "critical_nn": ["date", "subreddit", "ticker", "mention_count"],
        "date_col":    "date",
        "value_ranges": {"mention_count": (0, 10_000)},
    },
    # ── AIS vessel tracking ───────────────────────────────────────────────────
    "ais_positions": {
        "required":    ["mmsi", "zone", "ship_type", "fetched_at"],
        "critical_nn": ["mmsi", "zone"],
        "date_col":    None,
    },
    "ais_zone_summary": {
        "required":    ["zone", "ship_type_label", "vessel_count", "fetched_at"],
        "critical_nn": ["zone", "ship_type_label", "vessel_count"],
        "date_col":    None,
        "value_ranges": {"vessel_count": (0, 10_000)},
    },
    # ── Stock Analysis (scraped — column set drifts with the site's HTML, so
    #    schemas require only the columns the pipeline itself constructs) ──────
    "sa_movers": {
        "required":    ["symbol", "signal", "fetched_at"],
        "critical_nn": ["symbol", "signal"],
        "date_col":    None,
    },
    "sa_ipos": {
        "required":    ["symbol", "fetched_at"],
        "critical_nn": ["symbol"],
        "date_col":    None,
    },
    "sa_ipo_calendar": {
        "required":    ["symbol", "fetched_at"],
        "critical_nn": ["symbol"],
        "date_col":    None,
    },
    "sa_ipo_stats": {
        "required":    ["fetched_at"],
        "critical_nn": [],
        "date_col":    None,
    },
    "sa_corporate_actions": {
        "required":    ["action_type", "fetched_at"],
        "critical_nn": ["action_type"],
        "date_col":    None,
    },
    "sa_stock_list": {
        "required":    ["symbol", "fetched_at"],
        "critical_nn": ["symbol"],
        "date_col":    None,
    },
    "sa_etf_list": {
        "required":    ["symbol", "fetched_at"],
        "critical_nn": ["symbol"],
        "date_col":    None,
    },
    "sa_income": {
        "required":    ["metric", "symbol", "period_type", "fetched_at"],
        "critical_nn": ["metric", "symbol"],
        "date_col":    None,
    },
    "sa_balance": {
        "required":    ["metric", "symbol", "period_type", "fetched_at"],
        "critical_nn": ["metric", "symbol"],
        "date_col":    None,
    },
    "sa_cashflow": {
        "required":    ["metric", "symbol", "period_type", "fetched_at"],
        "critical_nn": ["metric", "symbol"],
        "date_col":    None,
    },
    "sa_ratios": {
        "required":    ["metric", "symbol", "period_type", "fetched_at"],
        "critical_nn": ["metric", "symbol"],
        "date_col":    None,
    },
    # ── Finviz (scraped — same drift caveat as Stock Analysis above) ──────────
    "finviz_movers": {
        "required":    ["ticker", "signal", "fetched_at"],
        "critical_nn": ["ticker", "signal"],
        "date_col":    None,
    },
    "finviz_screener": {
        "required":    ["ticker", "fetched_at"],
        "critical_nn": ["ticker"],
        "date_col":    None,
    },
    "finviz_financials": {
        "required":    ["ticker", "fetched_at"],
        "critical_nn": ["ticker"],
        "date_col":    None,
    },
    "finviz_insider": {
        "required":    ["ticker", "owner", "transaction", "fetched_at"],
        "critical_nn": ["ticker"],
        "date_col":    None,
    },
    "finviz_sector_perf": {
        "required":    ["name", "group_type", "fetched_at"],
        "critical_nn": ["name"],
        "date_col":    None,
    },
    "finviz_industry_perf": {
        "required":    ["name", "group_type", "fetched_at"],
        "critical_nn": ["name"],
        "date_col":    None,
    },
    "finviz_country_perf": {
        "required":    ["name", "group_type", "fetched_at"],
        "critical_nn": ["name"],
        "date_col":    None,
    },
    "finviz_group_valuation": {
        "required":    ["name", "group_type", "fetched_at"],
        "critical_nn": ["name", "group_type"],
        "date_col":    None,
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
