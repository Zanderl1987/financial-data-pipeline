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
        SELECT s.symbol, s.date, s.strike_price, s.bsm_price, f.value AS revenue
        FROM synthetic_options s
        JOIN fundamentals_annual f
          ON s.symbol = f.symbol AND f.metric = 'revenue'
        WHERE s.vol_method = 'cc' AND s.symbol = 'NVDA'
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

# When True (default), a table's view reads its deduplicated curated snapshot
# (storage/curated/<table>/<table>.parquet) if one exists, falling back to the
# raw glob otherwise. Set q.USE_CURATED = False then q.reload() to force raw.
USE_CURATED = True


def _glob(relative: str) -> str:
    return os.path.join(_STORAGE_ROOT, relative).replace("\\", "/")


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
    "shipping_gscpi":          _glob("shipping/gscpi/**/*.parquet"),
    "shipping_freight_ppi":    _glob("shipping/freight_ppi/**/*.parquet"),
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
    if table not in CATALOG:
        raise ValueError(f"Unknown table '{table}'. Available: {sorted(CATALOG)}")

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
    if table not in CATALOG:
        raise ValueError(f"Unknown table '{table}'. Available: {sorted(CATALOG)}")
    return sql(f"DESCRIBE {table}")


def tables() -> pd.DataFrame:
    """List all catalog entries with row counts. 'no data' = no files on disk yet."""
    rows = []
    for name in CATALOG:
        try:
            count = _con().execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            rows.append({"table": name, "rows": f"{count:,}"})
        except Exception:
            rows.append({"table": name, "rows": "no data"})
    return pd.DataFrame(rows)


def symbols(table: str) -> list[str]:
    """Return sorted list of distinct tickers available in a table."""
    if table not in CATALOG:
        raise ValueError(f"Unknown table '{table}'. Available: {sorted(CATALOG)}")
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
