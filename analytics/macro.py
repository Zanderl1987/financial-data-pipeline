"""
Macro analytics: yield curve shape, 2s10s inversion, commodity correlations.

Requires the macro table (FRED) to have data; commodity_vs_symbol also
needs the prices table populated.
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q

# FRED series IDs mapped to human-readable maturity labels
YIELD_SERIES: dict[str, str] = {
    "3m":  "DGS3MO",
    "2y":  "DGS2",
    "5y":  "DGS5",
    "10y": "DGS10",
    "30y": "DGS30",
}


def rate_environment(start: "str | None" = None) -> pd.DataFrame:
    """
    Treasury yields for key maturities over time (wide format).

    Returns:
        date | 3m | 2y | 5y | 10y | 30y
    Columns absent from the data are omitted.
    """
    series_ids = list(YIELD_SERIES.values())
    df = q.load("macro", series_id=series_ids, start=start)
    if df.empty:
        return df

    reverse_map = {v: k for k, v in YIELD_SERIES.items()}
    df = df.copy()
    df["maturity"] = df["series_id"].map(reverse_map)

    ordered = [m for m in ["3m", "2y", "5y", "10y", "30y"] if m in df["maturity"].values]
    return (
        df.pivot_table(index="date", columns="maturity", values="value")[ordered]
          .reset_index()
          .sort_values("date")
    )


def inversion(start: "str | None" = None) -> pd.DataFrame:
    """
    2s10s yield spread (10y minus 2y) — the canonical recession signal.

    Negative spread = inverted curve.

    Returns:
        date | 2y | 10y | spread_2s10s | inverted
    """
    rates = rate_environment(start=start)
    if rates.empty or "2y" not in rates.columns or "10y" not in rates.columns:
        return pd.DataFrame(columns=["date", "2y", "10y", "spread_2s10s", "inverted"])

    out = rates[["date", "2y", "10y"]].dropna().copy()
    out["spread_2s10s"] = (out["10y"] - out["2y"]).round(3)
    out["inverted"] = out["spread_2s10s"] < 0
    return out.reset_index(drop=True)


def commodity_vs_symbol(
    commodity_series_id: str,
    symbol: str,
    start: "str | None" = None,
    end: "str | None" = None,
) -> pd.DataFrame:
    """
    Align a FRED/EIA commodity series with an equity's close price.

    Useful for correlation analysis (e.g. WTI crude vs XOM).

    Parameters
    ----------
    commodity_series_id : FRED series ID (e.g. 'DCOILWTICO' for WTI crude)
    symbol              : equity ticker (e.g. 'XOM')
    start / end         : 'YYYY-MM-DD' date bounds

    Returns wide DataFrame (inner join on date):
        date | close | commodity_value
    """
    prices = q.load("prices", symbol=symbol, start=start, end=end,
                    columns=["date", "close"])
    comm = q.load("macro", series_id=commodity_series_id, start=start, end=end,
                  columns=["date", "value"])

    if prices.empty or comm.empty:
        return pd.DataFrame(columns=["date", "close", "commodity_value"])

    return (
        prices.merge(comm.rename(columns={"value": "commodity_value"}), on="date", how="inner")
              .sort_values("date")
              .reset_index(drop=True)
    )
