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
# positive_cols — columns that must be STRICTLY > 0            → WARN if violated

SCHEMAS: dict[str, dict] = {
    "prices": {
        "required":    ["symbol", "date", "open", "high", "low", "close", "volume"],
        "critical_nn": ["symbol", "date", "close"],
        "date_col":    "date",
        "positive_cols": ["open", "high", "low", "close"],
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
        "required":    ["symbol", "date", "strike_price", "expiration_date", "theo_price"],
        "critical_nn": ["symbol", "date", "theo_price"],
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
        "positive_cols": ["open", "high", "low", "close"],
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
        "positive_cols": ["last"],
    },
    "schwab_options": {
        "required":    ["symbol", "put_call", "expiration_date", "strike", "snapshot_date"],
        "critical_nn": ["symbol", "expiration_date", "strike", "snapshot_date"],
        # date_col drives the future-date check, so it has to be a column that
        # should never exceed today. expiration_date was the opposite: an option
        # is future-dated by definition, so that check warned on ~100% of rows
        # every run and carried no information. snapshot_date is an observation
        # date -- past it, and something is genuinely wrong (clock skew, or the
        # market-time conversion in schwab_options_pipeline.py misbehaving).
        "date_col":    "snapshot_date",
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
    "bls_avg_price": {
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
        "positive_cols": ["open", "high", "low", "close"],
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
        "positive_cols": ["open", "high", "low", "close"],
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
        "required":    ["country", "commodity", "obs_year", "value"],
        "critical_nn": ["country", "commodity", "value"],
        "date_col":    None,
    },
    "fao_prices": {
        "required":    ["country", "commodity", "obs_year", "value"],
        "critical_nn": ["country", "commodity", "value"],
        "date_col":    None,
    },
    # ── Plastics production (OWID) ─────────────────────────────────────────────
    "plastics_production": {
        "required":    ["country", "obs_year", "value", "unit"],
        "critical_nn": ["country", "obs_year", "value"],
        "date_col":    None,
    },
    # ── CFPB consumer complaints ────────────────────────────────────────────────
    "cfpb_complaints": {
        "required":    ["complaint_id", "date_received", "product", "company"],
        "critical_nn": ["complaint_id", "date_received"],
        "date_col":    "date_received",
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
        "required":    ["commodity", "stat_category", "date", "value", "fetched_at"],
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
        "positive_cols": ["close"],
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
    # ── California legislature Form 700 Schedule A-1 (investments) ────────────
    "california_disclosures": {
        "required":    ["agency", "filer_last_name", "business_entity", "fetched_at"],
        "critical_nn": ["agency", "filer_last_name", "business_entity"],
        "date_col":    "filed_date",
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
    "eia_refinery_activity": {
        "required":    ["series_id", "series_name", "date", "value", "fetched_at"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
        "value_ranges": {"value": (0, 30_000)},       # thousand bbl/day inputs, or 0-100 percent
    },
    "eia_crude_trade": {
        "required":    ["series_id", "series_name", "date", "value", "fetched_at"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
        "value_ranges": {"value": (0, 20_000)},       # thousand barrels per day
    },
    # ── USGS DS-140 critical mineral statistics ───────────────────────────────
    "usgs_minerals": {
        "required":    ["commodity", "category", "sheet", "table_title", "period", "value", "fetched_at"],
        "critical_nn": ["commodity", "table_title", "period", "value"],
        "date_col":    None,
    },
    # ── USGS MCS helium (+ rare gases) annual releases ────────────────────────
    "usgs_mcs_helium": {
        "required":    ["obs_year", "series", "commodity", "country", "value", "unit", "fetched_at"],
        "critical_nn": ["obs_year", "series", "commodity", "value"],
        "date_col":    None,
    },
    # ── USGS DS-140 helium historical statistics ───────────────────────────────
    "usgs_ds140_helium": {
        "required":    ["obs_year", "metric", "value", "unit", "fetched_at"],
        "critical_nn": ["obs_year", "metric", "value"],
        "date_col":    None,
    },
    # Unwired 2026-07-26 (not in CATALOG): requires OMKAR_API_KEY, never set.
    # "omkar_commodity": {
    #     "required":    ["commodity", "price_usd", "fetched_at"],
    #     "critical_nn": ["commodity", "price_usd"],
    #     "date_col":    None,
    # },
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
        "positive_cols": ["close"],
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
        "required":    ["hs_code", "hs_name", "category", "obs_year", "flow", "trade_value_usd", "fetched_at"],
        "critical_nn": ["hs_code", "obs_year", "flow"],
        "date_col":    None,
        "value_ranges": {"trade_value_usd": (0, 5e12)},
    },
    # ── GEM tracker summary tables (biannual Google Sheets exports) ───────────
    "gem_coal_summary": {
        "required":    ["tracker_sheet", "indicator", "country_or_region", "column_label", "value", "fetched_at"],
        "critical_nn": ["tracker_sheet", "indicator", "country_or_region", "column_label"],
        "date_col":    None,
    },
    "gem_coal_mine_summary": {
        "required":    ["tracker_sheet", "indicator", "country_or_region", "column_label", "value", "fetched_at"],
        "critical_nn": ["tracker_sheet", "indicator", "country_or_region", "column_label"],
        "date_col":    None,
    },
    "gem_steel_summary": {
        "required":    ["tracker_sheet", "indicator", "country_or_region", "column_label", "value", "fetched_at"],
        "critical_nn": ["tracker_sheet", "indicator", "country_or_region", "column_label"],
        "date_col":    None,
    },
    "gem_cement_summary": {
        "required":    ["tracker_sheet", "indicator", "country_or_region", "column_label", "value", "fetched_at"],
        "critical_nn": ["tracker_sheet", "indicator", "country_or_region", "column_label"],
        "date_col":    None,
    },
    "gem_oilgas_summary": {
        "required":    ["tracker_sheet", "indicator", "country_or_region", "column_label", "value", "fetched_at"],
        "critical_nn": ["tracker_sheet", "indicator", "country_or_region", "column_label"],
        "date_col":    None,
    },
    "gem_lng_summary": {
        "required":    ["tracker_sheet", "indicator", "country_or_region", "column_label", "value", "fetched_at"],
        "critical_nn": ["tracker_sheet", "indicator", "country_or_region", "column_label"],
        "date_col":    None,
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
    # ── Fed sentiment (RSS scrape + Claude hawkish/dovish scoring) ───────────
    # Unwired 2026-07-26 (not in CATALOG): requires ANTHROPIC_API_KEY, never set.
    # "fed_speeches": {
    #     "required":    ["doc_id", "doc_type", "title", "link", "text", "fetched_at"],
    #     "critical_nn": ["doc_id", "doc_type", "text"],
    #     "date_col":    "date",
    # },
    # "fed_sentiment": {
    #     "required":    ["doc_id", "doc_type", "stance", "hawkish_score", "fetched_at"],
    #     "critical_nn": ["doc_id", "stance", "hawkish_score"],
    #     "date_col":    "date",
    #     "value_ranges": {"hawkish_score": (-1.0, 1.0), "confidence": (0.0, 1.0)},
    # },
    # ── Real estate: FHFA House Price Index ───────────────────────────────────
    "fhfa_hpi": {
        "required":    ["hpi_type", "level", "place_name", "date", "fetched_at"],
        "critical_nn": ["hpi_type", "level", "place_name", "date"],
        "date_col":    "date",
    },
    # ── Real estate: Zillow ZHVI / ZORI ───────────────────────────────────────
    "zillow_zhvi": {
        "required":    ["region_name", "region_type", "date", "zhvi", "fetched_at"],
        "critical_nn": ["region_name", "date", "zhvi"],
        "date_col":    "date",
        "value_ranges": {"zhvi": (0, 20_000_000)},
    },
    "zillow_zori": {
        "required":    ["region_name", "region_type", "date", "zori", "fetched_at"],
        "critical_nn": ["region_name", "date", "zori"],
        "date_col":    "date",
        "value_ranges": {"zori": (0, 100_000)},
    },
    # ── Shipping / logistics ──────────────────────────────────────────────────
    "shipping_gscpi": {
        "required":    ["date", "gscpi", "fetched_at"],
        "critical_nn": ["date", "gscpi"],
        "date_col":    "date",
        "value_ranges": {"gscpi": (-10, 10)},
    },
    "shipping_freight_ppi": {
        "required":    ["series_id", "date", "value", "fetched_at"],
        "critical_nn": ["series_id", "date", "value"],
        "date_col":    "date",
    },
    # ── Piracy incidents ─────────────────────────────────────────────────────
    # incident_year is nullable (~2% of IMB pins carry no parseable year);
    # day-level dating lives in somali_hijackings.incident_date.
    "piracy_incidents": {
        "required":    ["incident_id", "lat", "lng", "region", "source", "fetched_at"],
        "critical_nn": ["incident_id", "lat", "lng", "region"],
        "date_col":    None,
        "value_ranges": {"lat": (-90, 90), "lng": (-180, 180)},
    },
    "somali_hijackings": {
        "required":    ["vessel_name", "section_year", "hijack_status", "source", "fetched_at"],
        "critical_nn": ["vessel_name", "section_year"],
        "date_col":    None,
        "value_ranges": {"section_year": (1990, 2030)},
    },
    # ── TSA checkpoint travel volumes ─────────────────────────────────────────
    "tsa_checkpoint": {
        "required":    ["date", "travelers", "fetched_at"],
        "critical_nn": ["date", "travelers"],
        "date_col":    "date",
        "value_ranges": {"travelers": (0, 10_000_000)},
    },
    # ── Yahoo Finance market history ──────────────────────────────────────────
    "market_history": {
        "required":    ["symbol", "name", "asset_class", "date", "close", "fetched_at"],
        "critical_nn": ["symbol", "date", "close"],
        "date_col":    "date",
    },
    # ── Yahoo Finance Russell 3000 universe (split-adjusted equity OHLCV) ─────
    "yfinance_universe_prices": {
        "required":    ["symbol", "date", "close", "adj_close", "fetched_at"],
        "critical_nn": ["symbol", "date", "close", "adj_close"],
        "date_col":    "date",
        "positive_cols": ["close", "adj_close"],
    },
    # ── TradingView technical-rating snapshots ────────────────────────────────
    "tv_ratings": {
        "required":    ["symbol", "date", "rating_all", "rating_ma", "rating_osc",
                        "rating_label", "fetched_at"],
        "critical_nn": ["symbol", "date", "rating_all"],
        "date_col":    "date",
        "value_ranges": {"rating_all": (-1, 1), "rating_ma": (-1, 1),
                         "rating_osc": (-1, 1)},
    },
    # ── SEC EDGAR filing index ────────────────────────────────────────────────
    "sec_filings": {
        "required":    ["form", "company", "cik", "filed", "url", "fetched_at"],
        "critical_nn": ["form", "cik", "filed"],
        "date_col":    "filed",
    },
    # ── Schwab intraday bars, movers, portfolio mirror ────────────────────────
    "schwab_intraday": {
        "required":    ["symbol", "datetime", "date", "open", "high", "low",
                        "close", "volume", "freq_min", "fetched_at"],
        "critical_nn": ["symbol", "datetime", "close"],
        "date_col":    "date",
        "positive_cols": ["open", "high", "low", "close"],
    },
    "schwab_movers": {
        "required":    ["date", "index_symbol", "sort", "rank", "symbol",
                        "last_price", "fetched_at"],
        "critical_nn": ["date", "index_symbol", "symbol"],
        "date_col":    "date",
    },
    "schwab_positions": {
        "required":    ["date", "account", "symbol", "asset_type",
                        "market_value", "fetched_at"],
        "critical_nn": ["date", "account", "symbol"],
        "date_col":    "date",
    },
    "schwab_transactions": {
        "required":    ["account", "activity_id", "date", "type",
                        "net_amount", "fetched_at"],
        "critical_nn": ["account", "date", "type"],
        "date_col":    "date",
    },
    "signal_health": {
        "required":    ["run_date", "signal", "window", "symbols_key", "n_trades",
                        "win_rate_pct", "avg_return_pct", "profit_factor",
                        "car21_mean_pct", "car21_tstat", "holding_days", "fetched_at"],
        "critical_nn": ["signal", "window", "win_rate_pct"],
        "date_col":    "run_date",
    },
    "fred_macro_housing": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_macro_sentiment": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_macro_industrial": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_macro_consumer": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_macro_trade": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_rates_gdp_interest_rates": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_rates_gdp_money_supply": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_rates_gdp_gdp": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_rates_gdp_inflation": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_rates_gdp_mortgage": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_rates_gdp_commodities": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_rates_gdp_exchange_rates": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_rates_gdp_markets": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_rates_gdp_federal_debt": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "fred_rates_gdp_labor": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "alpha_vantage_overview": {
        "required":    ['Symbol', 'fetched_at'],
        "critical_nn": ['Symbol'],
        "date_col":    "fetched_at",
    },
    "alpha_vantage_income_statement": {
        "required":    ['ticker', 'fiscalDateEnding', 'report_type', 'totalRevenue', 'fetched_at'],
        "critical_nn": ['ticker', 'fiscalDateEnding', 'report_type'],
        "date_col":    "fiscalDateEnding",
    },
    "alpha_vantage_balance_sheet": {
        "required":    ['ticker', 'fiscalDateEnding', 'report_type', 'totalAssets', 'fetched_at'],
        "critical_nn": ['ticker', 'fiscalDateEnding', 'report_type'],
        "date_col":    "fiscalDateEnding",
    },
    "alpha_vantage_cash_flow": {
        "required":    ['ticker', 'fiscalDateEnding', 'report_type', 'operatingCashflow', 'fetched_at'],
        "critical_nn": ['ticker', 'fiscalDateEnding', 'report_type'],
        "date_col":    "fiscalDateEnding",
    },
    "alpha_vantage_earnings": {
        "required":    ['ticker', 'fiscalDateEnding', 'report_type', 'reportedEPS', 'fetched_at'],
        "critical_nn": ['ticker', 'fiscalDateEnding', 'report_type'],
        "date_col":    "fiscalDateEnding",
    },
    "alpha_vantage_earnings_calendar": {
        "required":    ['symbol', 'reportDate', 'fiscalDateEnding', 'fetched_at'],
        "critical_nn": ['symbol', 'reportDate'],
        "date_col":    "reportDate",
    },
    "alpha_vantage_dividends": {
        "required":    ['ticker', 'ex_dividend_date', 'amount', 'fetched_at'],
        "critical_nn": ['ticker', 'ex_dividend_date'],
        "date_col":    "ex_dividend_date",
    },
    "alpha_vantage_insider_transactions": {
        "required":    ['ticker', 'transaction_date', 'executive', 'shares', 'fetched_at'],
        "critical_nn": ['ticker', 'transaction_date', 'executive'],
        "date_col":    "transaction_date",
    },
    "alpha_vantage_news_sentiment": {
        "required":    ['url', 'time_published', 'overall_sentiment_score', 'fetched_at'],
        "critical_nn": ['url'],
        "date_col":    "time_published",
    },
    "alpha_vantage_top_gainers_losers": {
        "required":    ['category', 'ticker', 'price', 'fetched_at'],
        "critical_nn": ['category', 'ticker'],
        "date_col":    "fetched_at",
    },
    "coingecko_global_market": {
        "required":    ['snapshot_date', 'total_market_cap_usd', 'fetched_at'],
        "critical_nn": ['snapshot_date'],
        "date_col":    "snapshot_date",
    },
    "coingecko_coins_markets": {
        "required":    ['coin_id', 'snapshot_date', 'current_price_usd', 'fetched_at'],
        "critical_nn": ['coin_id', 'snapshot_date'],
        "date_col":    "snapshot_date",
    },
    "coingecko_trending": {
        "required":    ['type', 'name', 'snapshot_date', 'fetched_at'],
        "critical_nn": ['type', 'name', 'snapshot_date'],
        "date_col":    "snapshot_date",
    },
    "coingecko_categories": {
        "required":    ['category_id', 'snapshot_date', 'market_cap_usd', 'fetched_at'],
        "critical_nn": ['category_id', 'snapshot_date'],
        "date_col":    "snapshot_date",
    },
    "coingecko_derivatives": {
        "required":    ['symbol', 'exchange_name', 'snapshot_date', 'price', 'fetched_at'],
        "critical_nn": ['symbol', 'exchange_name', 'snapshot_date'],
        "date_col":    "snapshot_date",
    },
    "coingecko_exchange_rates": {
        "required":    ['currency', 'snapshot_date', 'value', 'fetched_at'],
        "critical_nn": ['currency', 'snapshot_date'],
        "date_col":    "snapshot_date",
    },
    "sec_edgar_submissions": {
        "required":    ['cik', 'accession_number', 'form_type', 'filing_date', 'fetched_at'],
        "critical_nn": ['cik', 'accession_number'],
        "date_col":    "filing_date",
    },
    "sec_edgar_xbrl_fundamentals": {
        "required":    ['cik', 'concept', 'end_date', 'value', 'unit', 'fetched_at'],
        "critical_nn": ['cik', 'concept', 'end_date'],
        "date_col":    "end_date",
    },
    "sec_edgar_efts_search": {
        "required":    ['search_query', 'accession_number', 'filing_date', 'fetched_at'],
        "critical_nn": ['search_query', 'accession_number'],
        "date_col":    "filing_date",
    },
    "bls_import_export_prices": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "bls_eci": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "bls_productivity": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "bls_oes": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "bls_qcew": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "bls_ecec": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "bls_cps_demographics": {
        "required":    ['series_id', 'date', 'value', 'fetched_at'],
        "critical_nn": ['series_id', 'date', 'value'],
        "date_col":    "date",
    },
    "eia_electricity_generation": {
        "required":    ['date', 'fuel_code', 'state', 'value', 'fetched_at'],
        "critical_nn": ['date', 'fuel_code', 'state'],
        "date_col":    "date",
    },
    "eia_electricity_sales": {
        "required":    ['date', 'stateid', 'sector_code', 'value', 'fetched_at'],
        "critical_nn": ['date', 'stateid', 'sector_code'],
        "date_col":    "date",
    },
    "eia_nuclear_outages": {
        "required":    ['date', 'value', 'fetched_at'],
        "critical_nn": ['date'],
        "date_col":    "date",
    },
    "eia_coal_production": {
        "required":    ['date', 'rank_code', 'mine_type', 'state', 'value', 'fetched_at'],
        "critical_nn": ['date', 'rank_code', 'mine_type', 'state'],
        "date_col":    "date",
    },
    "eia_coal_trade": {
        "required":    ['date', 'flow_type', 'country', 'coal_rank', 'value', 'fetched_at'],
        "critical_nn": ['date', 'flow_type', 'country', 'coal_rank'],
        "date_col":    "date",
    },
    "eia_international": {
        "required":    ['date', 'activity_code', 'product_code', 'country_code', 'value', 'fetched_at'],
        "critical_nn": ['date', 'activity_code', 'product_code', 'country_code'],
        "date_col":    "date",
    },
    "eia_seds": {
        "required":    ['date', 'state_code', 'series_id', 'value', 'fetched_at'],
        "critical_nn": ['date', 'state_code', 'series_id'],
        "date_col":    "date",
    },
    "eia_petroleum_spot_prices": {
        "required":    ['date', 'product_code', 'location_code', 'value', 'fetched_at'],
        "critical_nn": ['date', 'product_code', 'location_code'],
        "date_col":    "date",
    },
    "eia_petroleum_futures": {
        "required":    ['date', 'product_code', 'value', 'fetched_at'],
        "critical_nn": ['date', 'product_code'],
        "date_col":    "date",
    },
    "eia_refiner_margins": {
        "required":    ['date', 'series_code', 'value', 'fetched_at'],
        "critical_nn": ['date', 'series_code'],
        "date_col":    "date",
    },
    "eia_petroleum_supply_demand": {
        "required":    ['date', 'process_code', 'product_code', 'value', 'fetched_at'],
        "critical_nn": ['date', 'process_code', 'product_code'],
        "date_col":    "date",
    },
    "eia_natural_gas_consumption": {
        "required":    ['date', 'sector_code', 'value', 'fetched_at'],
        "critical_nn": ['date', 'sector_code'],
        "date_col":    "date",
    },
    "eia_natural_gas_prices": {
        "required":    ['date', 'series_code', 'value', 'fetched_at'],
        "critical_nn": ['date', 'series_code'],
        "date_col":    "date",
    },
    "eia_natural_gas_production": {
        "required":    ['date', 'series_code', 'value', 'fetched_at'],
        "critical_nn": ['date', 'series_code'],
        "date_col":    "date",
    },
    "eia_lng_flows": {
        "required":    ['date', 'region_code', 'value', 'fetched_at'],
        "critical_nn": ['date', 'region_code'],
        "date_col":    "date",
    },
    "eia_hourly_grid": {
        "required":    ['region_code', 'metric_type', 'value', 'fetched_at'],
        "critical_nn": ['region_code', 'metric_type', 'value'],
        "date_col":    "timestamp_utc",
    },
    "index_members": {
        "required":    ['index_code', 'ticker', 'snapshot_date', 'fetched_at'],
        "critical_nn": ['index_code', 'ticker'],
        "date_col":    "snapshot_date",
    },
    "securities": {
        "required":    ['symbol', 'last_refreshed'],
        "critical_nn": ['symbol'],
        "date_col":    None,
    },
    "fund_holdings": {
        # holding_ticker is NOT critical_nn: bond ETF rows (AGG/LQD/HYG/TIP)
        # legitimately have no ticker in BlackRock's fixed-income feed (bonds
        # are identified by holding_name + maturity_date/coupon_pct instead;
        # see fund_holdings_pipeline.py's fetch_blackrock_bond_holdings). A
        # single-file spot check on a bond-only raw file is 100% null on this
        # column by design, not a broken pipeline.
        "required":    ['fund_ticker', 'holding_ticker', 'source', 'fetched_at'],
        "critical_nn": ['fund_ticker'],
        "date_col":    "snapshot_date",
    },
    "etf_holdings": {
        "required":    ['fund_ticker', 'holding_ticker', 'source', 'fetched_at'],
        "critical_nn": ['fund_ticker', 'holding_ticker'],
        "date_col":    "snapshot_date",
    },
    "identifier_map": {
        "required":    ['ticker', 'source', 'fetched_at'],
        "critical_nn": ['ticker'],
        "date_col":    None,
    },
    "finnhub_esg": {
        "required":    ['symbol', 'fetched_at'],
        "critical_nn": ['symbol'],
        "date_col":    "fetched_at",
    },
    # Unwired 2026-08-28: free-tier 403, superseded by congressional_trades
    # "finnhub_congressional_trading": {
    #     "required":    ['symbol', 'member_name', 'transaction_date', 'fetched_at'],
    #     "critical_nn": ['symbol', 'member_name', 'transaction_date'],
    #     "date_col":    "transaction_date",
    # },
    "finnhub_supply_chain": {
        "required":    ['symbol', 'side', 'fetched_at'],
        "critical_nn": ['symbol', 'side'],
        "date_col":    "fetched_at",
    },
    "finnhub_insider_sentiment": {
        "required":    ['symbol', 'obs_year', 'obs_month', 'mspr', 'fetched_at'],
        "critical_nn": ['symbol', 'obs_year', 'obs_month'],
        "date_col":    "fetched_at",
    },
    "finnhub_social_sentiment": {
        "required":    ['symbol', 'timestamp', 'fetched_at'],
        "critical_nn": ['symbol', 'timestamp'],
        "date_col":    "timestamp",
    },
    "finnhub_sec_filings": {
        "required":    ['symbol', 'filing_date', 'form_type', 'fetched_at'],
        "critical_nn": ['symbol', 'filing_date', 'form_type'],
        "date_col":    "filing_date",
    },
    "finnhub_earnings_quality": {
        "required":    ['symbol', 'fetched_at'],
        "critical_nn": ['symbol'],
        "date_col":    "fetched_at",
    },
    "finnhub_lobbying": {
        # Finnhub's /stock/lobbying never populates a real per-filing date
        # (its own "date" field is blank on every row, verified 2026-08-22);
        # only year/period are reliable, so fetched_at is the date_col here.
        "required":    ['symbol', 'client_name', 'year', 'period', 'fetched_at'],
        "critical_nn": ['symbol', 'client_name'],
        "date_col":    "fetched_at",
    },
    "finnhub_usa_spending": {
        "required":    ['symbol', 'action_date', 'awarding_agency', 'fetched_at'],
        "critical_nn": ['symbol', 'action_date', 'awarding_agency'],
        "date_col":    "action_date",
    },
    "finnhub_uspto_patents": {
        "required":    ['symbol', 'fetched_at'],
        "critical_nn": ['symbol'],
        "date_col":    "fetched_at",
    },
    "finnhub_visa_applications": {
        "required":    ['symbol', 'fetched_at'],
        "critical_nn": ['symbol'],
        "date_col":    "fetched_at",
    },
    "finnhub_economic_calendar": {
        "required":    ['fetched_at'],
        "critical_nn": [],
        "date_col":    "fetched_at",
    },
    # USAspending federal contracts (keyless replacement for finnhub_usa_spending)
    "usaspending_award_counts": {
        "required":    ['window_start', 'window_end', 'award_type_code', 'award_count', 'fetched_at'],
        "critical_nn": ['window_start', 'award_type_code', 'award_count'],
        "date_col":    "window_end",
    },
    "usaspending_top_awards": {
        # date_col is window_end, NOT start_date: USAspending award-level rows
        # carry the original award start (can be decades old or even future-
        # dated for planned awards); only the query window bounds freshness.
        "required":    ['recipient_name', 'award_amount', 'awarding_agency', 'fetched_at'],
        "critical_nn": ['recipient_name', 'awarding_agency', 'award_amount'],
        "date_col":    "window_end",
    },
    # Senate LDA lobbying filings (keyless replacement for finnhub_lobbying)
    "lda_lobbying_filings": {
        "required":    ['filing_uuid', 'registrant_name', 'fetched_at'],
        "critical_nn": ['filing_uuid', 'registrant_name'],
        # dt_posted can be null on amended filings; fetched_at is the safe date_col
        "date_col":    "fetched_at",
    },
    # DeFi protocol fundamentals (snapshot-only, run daily to accumulate history)
    "defillama_protocols": {
        "required":    ['protocol_id', 'name', 'tvl', 'fetched_at'],
        "critical_nn": ['protocol_id', 'name'],
        "date_col":    "fetched_at",
    },
    "defillama_fees": {
        "required":    ['protocol_id', 'name', 'total_24h', 'fetched_at'],
        "critical_nn": ['protocol_id', 'name'],
        "date_col":    "fetched_at",
    },
    "defillama_stablecoins": {
        "required":    ['stablecoin_id', 'name', 'symbol', 'circulating', 'fetched_at'],
        "critical_nn": ['stablecoin_id', 'name', 'symbol'],
        "date_col":    "fetched_at",
    },
    "finnhub_earnings_history": {
        "required":    ['symbol', 'obs_year', 'quarter', 'actual', 'fetched_at'],
        "critical_nn": ['symbol', 'obs_year', 'quarter'],
        "date_col":    "fetched_at",
    },
    "finnhub_eps_estimates": {
        "required":    ['symbol', 'date', 'freq', 'eps_estimate', 'fetched_at'],
        "critical_nn": ['symbol', 'date', 'freq'],
        "date_col":    "date",
    },
    "finnhub_revenue_estimates": {
        "required":    ['symbol', 'date', 'freq', 'revenue_estimate', 'fetched_at'],
        "critical_nn": ['symbol', 'date', 'freq'],
        "date_col":    "date",
    },
    "finnhub_ownership": {
        "required":    ['symbol', 'holder_name', 'shares_held', 'fetched_at'],
        "critical_nn": ['symbol', 'holder_name'],
        "date_col":    "fetched_at",
    },
    "finnhub_splits": {
        "required":    ['symbol', 'fetched_at'],
        "critical_nn": ['symbol'],
        "date_col":    "fetched_at",
    },
    "finnhub_peers": {
        "required":    ['symbol', 'peer', 'fetched_at'],
        "critical_nn": ['symbol', 'peer'],
        "date_col":    "fetched_at",
    },
    "finnhub_executives": {
        "required":    ['symbol', 'fetched_at'],
        "critical_nn": ['symbol'],
        "date_col":    "fetched_at",
    },
    "finnhub_filing_sentiment": {
        "required":    ['symbol', 'fetched_at'],
        "critical_nn": ['symbol'],
        "date_col":    "fetched_at",
    },
    "finnhub_transcripts": {
        "required":    ['symbol', 'fetched_at'],
        "critical_nn": ['symbol'],
        "date_col":    "fetched_at",
    },
    "finnhub_company_news_sentiment": {
        "required":    ['symbol', 'buzz', 'sentiment', 'fetched_at'],
        "critical_nn": ['symbol'],
        "date_col":    "fetched_at",
    },
    "tiingo_corporate_actions_dividends": {
        "required":    ['symbol', 'ex_date', 'amount', 'fetched_at'],
        "critical_nn": ['symbol', 'ex_date'],
        "date_col":    "ex_date",
    },
    "tiingo_corporate_actions_splits": {
        "required":    ['symbol', 'ex_date', 'split_factor', 'fetched_at'],
        "critical_nn": ['symbol', 'ex_date'],
        "date_col":    "ex_date",
    },
    "tiingo_corporate_actions_yield": {
        "required":    ['symbol', 'date', 'distribution_yield', 'fetched_at'],
        "critical_nn": ['symbol', 'date'],
        "date_col":    "date",
    },
    "tiingo_fundamentals_daily": {
        "required":    ['symbol', 'date', 'market_cap', 'fetched_at'],
        "critical_nn": ['symbol', 'date'],
        "date_col":    "date",
    },
    "tiingo_fundamentals_statements": {
        "required":    ['symbol', 'date', 'statement_type', 'data_code', 'value', 'fetched_at'],
        "critical_nn": ['symbol', 'date', 'statement_type', 'data_code'],
        "date_col":    "date",
    },
    "treasury_debt_to_penny": {
        "required":    ['record_date', 'tot_pub_debt_out_amt', 'fetched_at'],
        "critical_nn": ['record_date'],
        "date_col":    "record_date",
    },
    "treasury_avg_interest_rates": {
        "required":    ['record_date', 'security_desc', 'avg_interest_rate_amt', 'fetched_at'],
        "critical_nn": ['record_date', 'security_desc'],
        "date_col":    "record_date",
    },
    "treasury_interest_expense": {
        "required":    ['record_date', 'expense_catg_desc', 'expense_type_desc', 'fytd_expense_amt', 'fetched_at'],
        "critical_nn": ['record_date', 'expense_catg_desc', 'expense_type_desc'],
        "date_col":    "record_date",
    },
    "treasury_auctions_detail": {
        "required":    ['cusip', 'auction_date', 'security_type', 'high_yield', 'fetched_at'],
        "critical_nn": ['cusip', 'auction_date'],
        "date_col":    "auction_date",
    },
    "treasury_exchange_rates": {
        "required":    ['record_date', 'country', 'currency', 'exchange_rate', 'fetched_at'],
        "critical_nn": ['record_date', 'country', 'currency'],
        "date_col":    "record_date",
    },
    "treasury_savings_bonds": {
        "required":    ['record_date', 'series_cd', 'bonds_out_cnt', 'fetched_at'],
        "critical_nn": ['record_date', 'series_cd'],
        "date_col":    "record_date",
    },
    "treasury_mts_receipts_outlays": {
        "required":    ['record_date', 'line_code_nbr', 'classification_desc', 'current_month_rcpt_outly_amt', 'fetched_at'],
        "critical_nn": ['record_date', 'line_code_nbr'],
        "date_col":    "record_date",
    },
    "treasury_mts_outlays_by_agency": {
        "required":    ['record_date', 'line_code_nbr', 'current_month_net_outly_amt', 'fetched_at'],
        "critical_nn": ['record_date', 'line_code_nbr'],
        "date_col":    "record_date",
    },
    "treasury_dts_operating_cash": {
        "required":    ['record_date', 'src_line_nbr', 'transaction_today_amt', 'fetched_at'],
        "critical_nn": ['record_date', 'src_line_nbr'],
        "date_col":    "record_date",
    },
    "treasury_mts_budget_comparison": {
        "required":    ['record_date', 'line_code_nbr', 'current_month_dfct_sur_amt', 'fetched_at'],
        "critical_nn": ['record_date', 'line_code_nbr'],
        "date_col":    "record_date",
    },
    "dark_pool_volume": {
        "required":    ['trade_date', 'fetched_at'],
        "critical_nn": ['trade_date'],
        "date_col":    "trade_date",
    },
    "retail_sentiment": {
        "required":    ['symbol', 'date', 'created_at', 'fetched_at'],
        "critical_nn": ['symbol'],
        "date_col":    "date",
    },
    "retail_sentiment_daily": {
        "required":    ['date', 'symbol', 'message_count', 'bullish_ratio', 'fetched_at'],
        "critical_nn": ['date', 'symbol'],
        "date_col":    "date",
    },
    "insider_sentiment": {
        "required":    ['ticker', 'cik', 'form_type', 'filing_date', 'transaction_class', 'fetched_at'],
        "critical_nn": ['ticker', 'filing_date'],
        "date_col":    "filing_date",
    },
    "indeed_job_postings_national": {
        "required":    ['date', 'indeed_job_postings_index_sa', 'indeed_job_postings_index_nsa', 'variable', 'fetched_at'],
        "critical_nn": ['date', 'indeed_job_postings_index_sa', 'indeed_job_postings_index_nsa'],
        "date_col":    "date",
    },
    "indeed_job_postings_sector": {
        "required":    ['date', 'sector', 'indeed_job_postings_index', 'variable', 'fetched_at'],
        "critical_nn": ['date', 'sector', 'indeed_job_postings_index'],
        "date_col":    "date",
    },
    "indeed_job_postings_state": {
        "required":    ['date', 'state', 'indeed_job_postings_index', 'fetched_at'],
        "critical_nn": ['date', 'state', 'indeed_job_postings_index'],
        "date_col":    "date",
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


def _check_positive(df: pd.DataFrame, schema: dict) -> list:
    """
    Columns that must be strictly greater than zero.

    Separate from value_ranges because that check uses `< lo`, so a bound of
    (0, hi) silently accepts an exact 0 -- and 0 is the common corruption in
    price data, not a near-miss. A zero or negative equity close is impossible,
    not merely implausible.

    Opt-in per table: a negative close is REAL for some instruments (WTI
    settled at -$37.63 on 2020-04-20, which is genuinely present in `futures`
    and `market_history`), and an option can expire worthless at 0. Only list
    columns where non-positive is definitionally wrong.
    """
    results = []
    for col in schema.get("positive_cols", []):
        if col not in df.columns:
            continue
        numeric = pd.to_numeric(df[col], errors="coerce")
        bad = numeric.notna() & (numeric <= 0)
        n_bad = int(bad.sum())
        if n_bad > 0:
            worst = float(numeric[bad].min())
            n_zero = int((numeric == 0).sum())
            results.append(CheckResult(
                f"positive:{col}", Severity.WARNING,
                f"{n_bad} non-positive values ({n_zero} zero, "
                f"{n_bad - n_zero} negative, min {worst:g})"
            ))
        else:
            results.append(CheckResult(
                f"positive:{col}", Severity.OK, f"all {col} values > 0"))
    return results


def _latest_file(glob_path: str) -> list[str]:
    """
    List matching files sorted OLDEST to NEWEST by modification time.

    Filenames aren't a reliable date proxy -- older pipeline versions used
    different naming conventions (no source prefix, hyphenated dates) that
    can sort alphabetically AFTER current filenames, silently selecting a
    stale file. mtime is the only reliable "latest" signal across renames.
    """
    files = _glob_mod.glob(glob_path.replace("/", os.sep), recursive=True)
    return sorted(files, key=os.path.getmtime)


def _check_row_count(table: str, df: pd.DataFrame) -> CheckResult:
    """Warn if the new DataFrame is less than 50% the size of the most recent snapshot."""
    glob_path = q.CATALOG.get(table, "")
    existing = _latest_file(glob_path)
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
    checks.extend(_check_positive(df, schema))
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
    files = _latest_file(glob_path)
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


def validate_table_full(table: str) -> ValidationResult:
    """
    Validate a table's ENTIRE deduplicated history, not just its newest file.

    validate_table() reads only the mtime-latest raw file. That keeps the
    routine health check fast, but it means a defect anywhere in history is
    invisible: `prices` carries 170,795 non-positive rows (161,783 of them
    negative) and still spot-checks clean, because its newest daily file is
    fine. Use this to audit an existing table; use validate_table() for the
    daily gate.

    Reads through query.py, so it sees the curated snapshot when one exists.
    """
    if table not in q.CATALOG:
        return ValidationResult(table, [
            CheckResult("catalog", Severity.ERROR, f"'{table}' not in CATALOG")
        ])
    try:
        df = q.load(table)
    except Exception as exc:
        return ValidationResult(table, [
            CheckResult("read", Severity.ERROR, f"Failed to load {table}: {exc}")
        ])
    if df.empty:
        return ValidationResult(table, [
            CheckResult("files", Severity.WARNING, "No data on disk yet")
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
        files = _latest_file(glob_path)
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
    parser.add_argument("--full",  action="store_true",
                        help="Validate the table's ENTIRE history via the curated "
                             "snapshot instead of spot-checking its newest raw file "
                             "(slower; use to audit, not as the daily gate)")
    args = parser.parse_args()

    if args.table:
        res = validate_table_full(args.table) if args.full else validate_table(args.table)
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
