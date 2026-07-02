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
"""

import argparse
import datetime
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

import curated
from validate import validate_table

load_dotenv()


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
        tables=["bls_cpi", "bls_ppi", "bls_employment", "bls_jolts", "bls_unemployment"],
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
        desc="US Census agricultural imports and exports by HTS chapter (5 chapters)",
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
        desc="US House and Senate stock trade disclosures (keyless)",
        stage=1,
        tables=["congressional_trades"],
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
        desc="USGS DS-140 critical mineral statistics — lithium, cobalt, graphite, rare earths",
        stage=1,
        tables=["usgs_minerals"],
        backfill_args=["--backfill"],
        timeout=300,
    ),
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
        name="tsa",
        file="tsa_pipeline.py",
        desc="TSA daily checkpoint travel volumes (leading air-travel demand indicator)",
        stage=1,
        tables=["tsa_checkpoint"],
        requires_env=[],
        backfill_args=["--backfill"],
        timeout=300,
    ),
    PipelineSpec(
        name="fed_sentiment",
        file="fed_sentiment_pipeline.py",
        desc="Fed speeches/FOMC statements (RSS) scored hawkish/dovish via Claude",
        stage=1,
        tables=["fed_speeches", "fed_sentiment"],
        requires_env=["ANTHROPIC_API_KEY"],
        timeout=600,
    ),
    # ── Stage 2 — Schwab-authenticated ─────────────────────────────────────────
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
        desc="Schwab real-time quote snapshot (DJI + sector ETFs)",
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
    ),
    PipelineSpec(
        name="options_chain",
        file="options_chain_pipeline.py",
        desc="Schwab options metrics and chain snapshot",
        stage=2,
        tables=["options_metrics", "options_chain"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
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
        desc="Claude API sentiment scoring of Finnhub news (requires finnhub table)",
        stage=3,
        tables=["news_sentiment"],
        requires_env=["ANTHROPIC_API_KEY"],
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
    try:
        result = subprocess.run(cmd, timeout=spec.timeout,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        duration = time.time() - t0
        if result.returncode != 0:
            return RunResult(spec.name, "FAIL", duration, f"exit {result.returncode}")
    except subprocess.TimeoutExpired:
        duration = time.time() - t0
        return RunResult(spec.name, "FAIL", duration, f"timed out after {spec.timeout}s")
    except Exception as exc:
        duration = time.time() - t0
        return RunResult(spec.name, "FAIL", duration, str(exc))

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

    _print_summary(results, args.backfill, start_time)

    return 0 if all(r.status in ("PASS", "SKIP", "DRY RUN") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
