"""
Feature-matrix builder — turns the 85-table store into one ML-ready panel.

`feature_matrix()` assembles a tidy (symbol, date) DataFrame where each row is a
trading day for one symbol and each column is a feature: price-derived returns
and momentum, point-in-time fundamentals, and broadcast macro series. This is
the join that makes the breadth of the pipeline usable as a single model input.

Point-in-time correctness
--------------------------
Fundamentals are merged with a DuckDB ASOF JOIN on the SEC *filed* date, so a
row dated 2025-03-01 only ever sees fundamentals that were public on or before
2025-03-01 — never the filing that lands a week later. This is what prevents
look-ahead bias from leaking future financials into a backtest.

Robustness
----------
Every feature block is guarded: if a source table has no data in this clone
(e.g. prices needs Schwab creds), that block is skipped and the rest of the
matrix still builds. The price source auto-detects: 'prices' if populated,
else 'tiingo_prices'.

Usage
-----
    from analytics import feature_matrix
    fm = feature_matrix(["AAPL", "MSFT"], start="2023-01-01")
    fm = feature_matrix(start="2024-01-01", fundamentals=False)   # price+macro only
"""

from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q

# Macro/series features broadcast cross-sectionally (same value for every symbol
# on a given date), ASOF-joined so each date carries the latest prior reading.
_MACRO_SERIES = {
    "DGS10":  "macro_dgs10",      # 10y Treasury yield
    "DGS2":   "macro_dgs2",       # 2y Treasury yield
    "VIXCLS": "macro_vix",        # equity volatility
    "T10Y2Y": "macro_2s10s",      # yield-curve slope
    "BAMLH0A0HYM2": "macro_hy_oas",  # high-yield credit spread
}

# Fundamentals pulled point-in-time. (metric in source table -> output column.)
# Metric names follow the pipeline's snake_case normalization (see
# fundamentals_pipeline.py), not raw SEC XBRL tags.
_FUND_METRICS = {
    "revenue":           "fund_revenue",
    "net_income":        "fund_net_income",
    "eps_diluted":       "fund_eps",
    "gross_profit":      "fund_gross_profit",
    "operating_income":  "fund_operating_income",
    "total_assets":      "fund_total_assets",
    "total_liabilities": "fund_total_liabilities",
    "operating_cash_flow": "fund_ocf",
    "shares_outstanding":  "fund_shares",
}


def _has_data(table: str) -> bool:
    try:
        return not q.load(table, limit=1).empty
    except Exception:
        return False


def _pick_price_table(price_table: "str | None") -> "str | None":
    if price_table:
        return price_table if _has_data(price_table) else None
    for cand in ("prices", "tiingo_prices", "sector_etfs"):
        if _has_data(cand):
            return cand
    return None


def _price_panel(price_table: str, symbols, start, end) -> pd.DataFrame:
    """Base panel: symbol, date, close (+ volume if present)."""
    cols = q.schema(price_table)["column_name"].tolist()
    close_col = "adj_close" if "adj_close" in cols else "close"
    select = ["symbol", "date", f"{close_col} AS close"]
    if "volume" in cols:
        select.append("volume")
    df = q.load(
        price_table, symbol=symbols, start=start, end=end,
        columns=["symbol", "date", close_col] + (["volume"] if "volume" in cols else []),
    )
    if df.empty:
        return df
    df = df.rename(columns={close_col: "close"})
    df["date"] = pd.to_datetime(df["date"])
    df = (df.dropna(subset=["symbol", "date", "close"])
            .drop_duplicates(["symbol", "date"])
            .sort_values(["symbol", "date"])
            .reset_index(drop=True))
    return df


def _add_price_features(df: pd.DataFrame) -> pd.DataFrame:
    """Returns, trailing volatility, and 12-1 momentum, computed per symbol."""
    g = df.groupby("symbol", group_keys=False)
    df["ret_1d"]   = g["close"].pct_change()
    df["ret_21d"]  = g["close"].pct_change(21)
    df["ret_63d"]  = g["close"].pct_change(63)
    df["ret_252d"] = g["close"].pct_change(252)
    # 12-1 momentum: trailing 12m return excluding the most recent month
    df["mom_12_1"] = g["close"].shift(21) / g["close"].shift(252) - 1
    # annualized trailing 21d realized vol
    df["vol_21d"] = (g["ret_1d"].rolling(21).std().reset_index(level=0, drop=True)
                     * (252 ** 0.5))
    if "volume" in df.columns:
        df["dollar_vol_21d"] = (
            (df["close"] * df["volume"]).groupby(df["symbol"])
            .rolling(21).mean().reset_index(level=0, drop=True)
        )
    return df


def _asof_fundamentals(panel: pd.DataFrame) -> pd.DataFrame:
    """
    Point-in-time fundamentals via DuckDB ASOF JOIN on the SEC `filed` date.

    For each (symbol, date) row, attaches the most recent annual fact whose
    filing date is on or before that date — no look-ahead.
    """
    if not _has_data("fundamentals_annual"):
        return panel

    con = q._con()
    con.register("_panel", panel[["symbol", "date"]])
    out = panel
    for metric, col in _FUND_METRICS.items():
        try:
            joined = con.execute(f"""
                SELECT p.symbol, p.date, f.value AS {col}
                FROM _panel p
                ASOF LEFT JOIN (
                    SELECT symbol, CAST(filed AS DATE) AS filed, value
                    FROM fundamentals_annual
                    WHERE metric = '{metric}' AND form = '10-K' AND filed IS NOT NULL
                ) f
                ON p.symbol = f.symbol AND p.date >= f.filed
            """).df()
        except Exception:
            continue
        joined["date"] = pd.to_datetime(joined["date"])
        out = out.merge(joined, on=["symbol", "date"], how="left")
    con.unregister("_panel")
    return out


def _broadcast_macro(panel: pd.DataFrame) -> pd.DataFrame:
    """ASOF-join macro series onto every row by date (cross-sectional broadcast)."""
    if not _has_data("macro"):
        return panel
    con = q._con()
    # one date axis for the whole panel
    dates = panel[["date"]].drop_duplicates().sort_values("date")
    con.register("_dates", dates)
    out_dates = dates.copy()
    for series_id, col in _MACRO_SERIES.items():
        try:
            joined = con.execute(f"""
                SELECT d.date, m.value AS {col}
                FROM _dates d
                ASOF LEFT JOIN (
                    SELECT CAST(date AS DATE) AS date, value
                    FROM macro WHERE series_id = '{series_id}'
                ) m
                ON d.date >= m.date
            """).df()
        except Exception:
            continue
        joined["date"] = pd.to_datetime(joined["date"])
        out_dates = out_dates.merge(joined, on="date", how="left")
    con.unregister("_dates")
    return panel.merge(out_dates, on="date", how="left")


def feature_matrix(
    symbols: "list[str] | str | None" = None,
    start: "str | None" = None,
    end: "str | None" = None,
    price_table: "str | None" = None,
    fundamentals: bool = True,
    macro: bool = True,
) -> pd.DataFrame:
    """
    Build a point-in-time (symbol, date) feature panel.

    Parameters
    ----------
    symbols      : ticker or list (default: all symbols in the price table)
    start, end   : 'YYYY-MM-DD' date filters on the panel
    price_table  : override the price source (default: auto-detect)
    fundamentals : include point-in-time SEC fundamentals (default True)
    macro        : include broadcast macro series (default True)

    Returns a DataFrame with columns:
        symbol | date | close | ret_1d | ret_21d | ret_63d | ret_252d |
        mom_12_1 | vol_21d | [dollar_vol_21d] | fund_* | macro_*
    Empty DataFrame if no price source has data.
    """
    pt = _pick_price_table(price_table)
    if pt is None:
        return pd.DataFrame()

    panel = _price_panel(pt, symbols, start, end)
    if panel.empty:
        return panel

    panel = _add_price_features(panel)
    if fundamentals:
        panel = _asof_fundamentals(panel)
    if macro:
        panel = _broadcast_macro(panel)

    panel.attrs["price_table"] = pt
    return panel.reset_index(drop=True)
