"""
Options analytics: IV summary, put/call volume ratio.

put_call_ratio: sourced from options_history (volume-based; no OI available there).
iv_summary: sourced from schwab_options (preferred, has IV + greeks) or options_chain
            (fallback, has volatility). Both currently empty until Schwab OAuth lands.
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q


def _normalise_iv_source(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Map source-specific column names to a common schema for iv_summary.

    Canonical columns: contract_type, strike_price, iv, expiration_date, date.
    Unknown sources passed through unchanged (caller's problem).
    """
    if source == "schwab_options":
        # put_call -> contract_type (uppercase), strike -> strike_price,
        # implied_volatility -> iv. No date column — derive from fetched_at.
        out = df.copy()
        out["contract_type"] = out["put_call"].str.upper()
        out["strike_price"] = out["strike"]
        out["iv"] = out["implied_volatility"]
        if "fetched_at" in out.columns:
            out["date"] = out["fetched_at"].str[:10]
        return out
    elif source == "options_chain":
        out = df.copy()
        out["contract_type"] = out["contract_type"].str.upper()
        out["iv"] = out["volatility"]
        return out
    return df


def iv_summary(symbol: str, date: "str | None" = None) -> pd.DataFrame:
    """
    Implied volatility summary across strikes and expirations for a symbol.

    Sources (checked in order):
      1. schwab_options -- has implied_volatility + full greeks (preferred)
      2. options_chain  -- has volatility column (fallback)

    Both may be empty today (Schwab OAuth pending). Returns empty DataFrame
    when no source has data for the symbol.

    Parameters
    ----------
    symbol : ticker (required)
    date   : 'YYYY-MM-DD' snapshot date (default: latest available)

    Returns DataFrame grouped by expiration x option type with:
        expiration_date | contract_type | avg_iv | min_iv | max_iv | n_contracts
    """
    for source in ("schwab_options", "options_chain"):
        df = q.load(source, symbol=symbol)
        if df.empty:
            continue

        df = _normalise_iv_source(df, source)

        required = {"expiration_date", "contract_type", "iv", "date"}
        if not required.issubset(df.columns):
            continue

        if date is None:
            date = str(df["date"].max())

        df = df[df["date"] == date].copy()
        if df.empty:
            continue

        return (df.groupby(["expiration_date", "contract_type"])
                  .agg(
                      avg_iv=("iv", "mean"),
                      min_iv=("iv", "min"),
                      max_iv=("iv", "max"),
                      n_contracts=("strike_price", "count"),
                  )
                  .round(4)
                  .reset_index()
                  .sort_values(["expiration_date", "contract_type"]))

    return pd.DataFrame()


def put_call_ratio(
    symbol: "str | None" = None,
    start: "str | None" = None,
    end: "str | None" = None,
) -> pd.DataFrame:
    """
    Daily put/call volume ratio from options_history.

    NOTE: This is a *volume* ratio, not the traditional open-interest ratio --
    options_history carries no open interest.  Interpretation is similar:
    < 0.7 = bullish (calls dominating), > 1.0 = bearish/hedging.
    For OI-based PCR, see options_metrics.put_call_ratio_oi (Schwab pipeline,
    requires OAuth).

    Parameters
    ----------
    symbol : ticker or None for all symbols
    start  : 'YYYY-MM-DD'
    end    : 'YYYY-MM-DD'

    Returns DataFrame with:
        symbol | date | call_volume | put_volume | put_call_ratio
    """
    df = q.load("options_history", symbol=symbol, start=start, end=end)
    if df.empty:
        return df

    agg = (df.groupby(["symbol", "date", "contract_type"])["volume"]
             .sum()
             .unstack("contract_type", fill_value=0)
             .reset_index())

    call_col = "CALL" if "CALL" in agg.columns else None
    put_col = "PUT" if "PUT" in agg.columns else None

    if call_col and put_col:
        agg["call_volume"] = agg[call_col]
        agg["put_volume"] = agg[put_col]
    elif call_col:
        agg["call_volume"] = agg[call_col]
        agg["put_volume"] = 0
    elif put_col:
        agg["put_volume"] = agg[put_col]
        agg["call_volume"] = 0
    else:
        agg["call_volume"] = 0
        agg["put_volume"] = 0

    drop_cols = [c for c in ("CALL", "PUT") if c in agg.columns]
    agg = agg.drop(columns=drop_cols, errors="ignore")

    agg["put_call_ratio"] = (
        agg["put_volume"] / agg["call_volume"].replace(0, float("nan"))
    ).round(3)

    return agg.sort_values(["symbol", "date"]).reset_index(drop=True)
