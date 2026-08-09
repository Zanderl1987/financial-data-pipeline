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


def flag_price_jumps(
    symbols: "list[str]",
    price_table: str = "prices",
    max_abs_log_return: float = 1.0986,   # ln(3): a >3x jump or <1/3x drop in one day
) -> pd.DataFrame:
    """
    Returns (symbol, max_abs_log_ret, min_close) for every symbol in
    `symbols` whose day-over-day price ratio exceeds `max_abs_log_return`
    in absolute log terms at least once in its history -- almost always an
    unadjusted stock split or a bad tick, not a real one-day move (a real
    move of that size is vanishingly rare even for distressed names).

    Static per-symbol flag, not date-varying -- a data-quality screen, not
    a liquidity filter (see point_in_time_eligible for that). Discovered
    2026-08-08 auditing a Russell 3000 backtest against `prices`: that
    table is built entirely from the Schwab API
    (schwab_universe_backfill.py -> price_history_pipeline.fetch_symbol),
    and Schwab's price_history endpoint returns UNADJUSTED closes -- no
    split-adjustment is applied anywhere in this pipeline, and no free
    corporate-actions source is currently wired in to backfill one
    (Tiingo's corporate-actions add-on needs a paid plan -- see CLAUDE.md).
    ~11% of Russell 3000 constituents were flagged by this screen. A
    symbol is excluded wholesale, not date-range-trimmed: a bad split
    ratio corrupts every price on one side of the jump, not just the jump
    day itself, so salvaging a "clean half" isn't safe without knowing
    which side (pre- or post-jump) is the unadjusted one.
    """
    if not symbols:
        return pd.DataFrame(columns=["symbol", "max_abs_log_ret", "min_close"])
    sql = f"""
        WITH p AS (
            SELECT symbol, CAST(date AS DATE) AS date, close,
                   LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
            FROM {price_table}
            WHERE symbol = ANY(?)
        )
        SELECT symbol, MAX(ABS(LN(close / NULLIF(prev_close, 0)))) AS max_abs_log_ret,
               MIN(close) AS min_close
        FROM p
        WHERE prev_close IS NOT NULL AND prev_close > 0 AND close > 0
        GROUP BY symbol
    """
    out = q._con().execute(sql, [list(symbols)]).df()
    return out[out["max_abs_log_ret"] > max_abs_log_return].reset_index(drop=True)


def clean_symbols(
    symbols: "list[str]",
    price_table: str = "prices",
    max_abs_log_return: float = 1.0986,
) -> "list[str]":
    """`symbols` with flag_price_jumps()'s data-quality flags removed."""
    flagged = set(flag_price_jumps(symbols, price_table, max_abs_log_return)["symbol"])
    return sorted(set(symbols) - flagged)


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
