"""
Options analytics: IV surface, put/call ratio.

Requires options_history (and optionally synthetic_options) to have data.
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q


def iv_summary(symbol: str, date: "str | None" = None) -> pd.DataFrame:
    """
    Implied volatility summary across strikes and expirations for a symbol.

    Parameters
    ----------
    symbol : ticker (required)
    date   : 'YYYY-MM-DD' snapshot date (default: latest available)

    Returns DataFrame grouped by expiration x option type with:
        expirationDate | optionType | avg_iv | min_iv | max_iv | n_contracts
    """
    df = q.load("options_history", symbol=symbol)
    if df.empty:
        return df

    if date is None:
        date = str(df["date"].max())

    df = df[df["date"] == date].copy()
    if df.empty:
        return df

    return (df.groupby(["expirationDate", "optionType"])
              .agg(
                  avg_iv=("impliedVolatility", "mean"),
                  min_iv=("impliedVolatility", "min"),
                  max_iv=("impliedVolatility", "max"),
                  n_contracts=("strike", "count"),
              )
              .round(4)
              .reset_index()
              .sort_values(["expirationDate", "optionType"]))


def put_call_ratio(
    symbol: "str | None" = None,
    start: "str | None" = None,
    end: "str | None" = None,
) -> pd.DataFrame:
    """
    Daily put/call open interest ratio.

    Interpretation: < 0.7 = bullish (calls dominating), > 1.0 = bearish/hedging.

    Parameters
    ----------
    symbol : ticker or None for all symbols
    start  : 'YYYY-MM-DD'
    end    : 'YYYY-MM-DD'

    Returns DataFrame with:
        symbol | date | call | put | put_call_ratio
    """
    df = q.load("options_history", symbol=symbol, start=start, end=end)
    if df.empty:
        return df

    agg = (df.groupby(["symbol", "date", "optionType"])["openInterest"]
             .sum()
             .unstack("optionType", fill_value=0)
             .reset_index())

    if "put" in agg.columns and "call" in agg.columns:
        agg["put_call_ratio"] = (
            agg["put"] / agg["call"].replace(0, float("nan"))
        ).round(3)

    return agg.sort_values(["symbol", "date"]).reset_index(drop=True)
