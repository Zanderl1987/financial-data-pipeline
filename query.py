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


def _glob(relative: str) -> str:
    return os.path.join(_STORAGE_ROOT, relative).replace("\\", "/")


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
    # ── News sentiment ────────────────────────────────────────────────────────
    "news_sentiment":          _glob("finnhub/news_sentiment/**/*.parquet"),
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
    """Register a DuckDB view for every catalog entry that has at least one file."""
    for name, glob_path in CATALOG.items():
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
