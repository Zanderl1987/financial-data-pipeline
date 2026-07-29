"""
evaluation/universe.py -- point-in-time universe construction for full-universe
factor evaluation. See docs/superpowers/specs/2026-07-29-full-universe-factor-
validation-design.md.

Two independent filters:
  exchange_listed_symbols() -- static market-structure filter (OTC exclusion)
  point_in_time_eligible()  -- date-varying liquidity filter, computed only from
                               data available as of each date (no look-ahead)

Neither filter addresses delisting survivorship: symbol_universe.csv and the
`prices` table are both a 2026-07-24 snapshot of currently-tradable instruments,
so any company that delisted/was acquired/went bankrupt before that snapshot is
entirely absent from the data for its whole history. That is a data-source
limitation, not something a filter here can fix -- state it in any report that
uses this module.
"""

import os

import pandas as pd

import query as q

_UNIVERSE_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "symbol_universe.csv")

OTC_EXCHANGES = {"OTC Markets", "Nasdaq OTCBB"}


def exchange_listed_symbols(exclude_otc: bool = True) -> list[str]:
    """
    Symbols from symbol_universe.csv, optionally excluding OTC Markets /
    Nasdaq OTCBB. Static (not date-varying) -- a market-structure filter, not
    a liquidity filter.
    """
    df = pd.read_csv(_UNIVERSE_CSV)
    if exclude_otc:
        df = df[~df["exchange"].isin(OTC_EXCHANGES)]
    return sorted(df["symbol"].dropna().unique().tolist())


def point_in_time_eligible(
    symbols: "list[str]",
    min_dollar_volume: float,
    start: "str | None" = None,
    end: "str | None" = None,
    price_table: str = "prices",
) -> pd.DataFrame:
    """
    Returns (symbol, date, eligible) for every (symbol, date) row in
    `price_table` within range. eligible=True iff trailing 21-trading-day
    average dollar volume (close * volume), computed using ONLY data on or
    before that date, is >= min_dollar_volume.

    Pads the query window back before `start` so the trailing window isn't
    truncated at the boundary (the same bug class fixed in
    analytics/features.py::feature_matrix -- see backlog item R).
    """
    if not symbols:
        return pd.DataFrame(columns=["symbol", "date", "eligible"])

    query_start = start
    if start is not None:
        query_start = (pd.Timestamp(start) - pd.Timedelta(days=45)).strftime("%Y-%m-%d")

    clauses = ["symbol = ANY(?)"]
    params: list = [list(symbols)]
    if query_start is not None:
        clauses.append("date >= ?")
        params.append(query_start)
    if end is not None:
        clauses.append("date <= ?")
        params.append(end)
    where = " AND ".join(clauses)

    sql = f"""
        WITH filtered AS (
            SELECT symbol, CAST(date AS DATE) AS date, close, volume
            FROM {price_table}
            WHERE {where}
        ),
        roll AS (
            SELECT symbol, date,
                   AVG(close * volume) OVER (
                       PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN 20 PRECEDING AND CURRENT ROW
                   ) AS trailing_dollar_vol
            FROM filtered
        )
        SELECT symbol, date, trailing_dollar_vol >= ? AS eligible
        FROM roll
    """
    params.append(min_dollar_volume)
    out = q._con().execute(sql, params).df()
    out["date"] = pd.to_datetime(out["date"])
    if start is not None:
        out = out[out["date"] >= pd.Timestamp(start)].reset_index(drop=True)
    return out
