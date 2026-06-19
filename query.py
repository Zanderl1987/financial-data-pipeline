"""
DuckDB query layer over the financial-data-pipeline Parquet store.

Usage
-----
    import query as q

    # Load a table (returns a pandas DataFrame)
    df = q.load("prices", symbol="NVDA", start="2025-01-01")
    df = q.load("prices", symbol=["NVDA", "AAPL"])
    df = q.load("macro", series_id="DGS10", start="2024-01-01")

    # Raw SQL — table names match the keys in CATALOG
    df = q.sql(\"\"\"
        SELECT s.symbol, s.date, s.strike_price, s.bsm_price, r.mark
        FROM synthetic_options s
        JOIN options_chain r
          ON  s.symbol          = r.symbol
          AND s.strike_price    = r.strikePrice
          AND s.expiration_date = r.expirationDate
          AND s.date            = r.date
        WHERE s.vol_method = 'cc' AND s.symbol = 'NVDA'
    \"\"\")

    # Discover what's loaded and how many rows
    q.tables()
    q.schema("fundamentals_annual")

Run directly to see a summary:
    python query.py
"""

import glob as _glob_mod
import os
import duckdb
import pandas as pd

_STORAGE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage", "raw")


def _glob(relative: str) -> str:
    return os.path.join(_STORAGE_ROOT, relative).replace("\\", "/")


CATALOG: dict[str, str] = {
    "prices":                 _glob("prices/prices_*.parquet"),
    "options_metrics":        _glob("options/options_metrics_*.parquet"),
    "options_chain":          _glob("options/options_chain_raw_*.parquet"),
    "fundamentals_annual":    _glob("fundamentals/fundamentals_*annual_*.parquet"),
    "fundamentals_quarterly": _glob("fundamentals/fundamentals_*quarterly_*.parquet"),
    "commodities":            _glob("commodities/commodities_*.parquet"),
    "macro":                  _glob("macro/macro_*.parquet"),
    "gas_spot":               _glob("gas_prices/gas_prices_spot_daily_*.parquet"),
    "gas_retail":             _glob("gas_prices/gas_prices_retail_weekly_*.parquet"),
    "futures":                _glob("futures/futures_ohlcv_*.parquet"),
    "cot":                    _glob("cot/cot_*.parquet"),
    "synthetic_options":      _glob("synthetic_options/synthetic_options_*.parquet"),
    "options_history":        _glob("options_history/options_history_*.parquet"),
}

_CON: duckdb.DuckDBPyConnection | None = None


def _con() -> duckdb.DuckDBPyConnection:
    global _CON
    if _CON is None:
        _CON = duckdb.connect()
        for name, glob_path in CATALOG.items():
            # Only register views for tables that have at least one file
            if not _glob_mod.glob(glob_path.replace("/", os.sep)):
                continue
            # union_by_name tolerates schema evolution across incremental files
            _CON.execute(f"""
                CREATE OR REPLACE VIEW {name} AS
                SELECT * FROM read_parquet('{glob_path}', union_by_name=True)
            """)
    return _CON


def sql(query: str) -> pd.DataFrame:
    """Execute raw SQL against the registered views. Returns a DataFrame."""
    return _con().execute(query).df()


def load(
    table: str,
    symbol=None,
    series_id=None,
    start: str | None = None,
    end: str | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """
    Load a table with optional filters. Returns a DataFrame.

    Parameters
    ----------
    table     : table name — one of the keys in CATALOG
    symbol    : str or list  — filter on 'symbol' column
    series_id : str or list  — filter on 'series_id' column (commodities / macro)
    start     : 'YYYY-MM-DD' — filter date >= start
    end       : 'YYYY-MM-DD' — filter date <= end
    columns   : list of column names to SELECT (default: all)
    """
    if table not in CATALOG:
        raise ValueError(f"Unknown table '{table}'. Available: {sorted(CATALOG)}")

    select = ", ".join(columns) if columns else "*"
    clauses = []

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

    if start is not None:
        clauses.append(f"date >= '{start}'")
    if end is not None:
        clauses.append(f"date <= '{end}'")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return sql(f"SELECT {select} FROM {table} {where}")


def schema(table: str) -> pd.DataFrame:
    """Return column names and types for a table."""
    if table not in CATALOG:
        raise ValueError(f"Unknown table '{table}'. Available: {sorted(CATALOG)}")
    return sql(f"DESCRIBE {table}")


def tables() -> pd.DataFrame:
    """List all registered tables with row counts."""
    rows = []
    for name in CATALOG:
        try:
            count = _con().execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            status = f"{count:,}"
        except Exception:
            status = "no data"
        rows.append({"table": name, "rows": status})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    print(tables().to_string(index=False))
