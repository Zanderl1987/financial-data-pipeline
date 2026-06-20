"""
Sector analytics: performance, relative strength, rotation signals.

Requires the sector_etfs table populated by sector_etf_pipeline.py.
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q

# Canonical sector ordering (GICS) + broad indexes
SECTOR_ETFS = [
    "XLK", "XLF", "XLE", "XLV", "XLY",
    "XLI", "XLC", "XLRE", "XLP", "XLU", "XLB",
]
BROAD_ETFS = ["SPY", "QQQ", "IWM", "DIA"]


def sector_performance(
    start: "str | None" = None,
    end: "str | None" = None,
    include_broad: bool = True,
) -> pd.DataFrame:
    """
    Total return % per ETF over the specified date range.

    Computes (last_close / first_close - 1) * 100 for each symbol.

    Parameters
    ----------
    start         : 'YYYY-MM-DD' start of period (default: earliest available)
    end           : 'YYYY-MM-DD' end of period (default: latest available)
    include_broad : include SPY/QQQ/IWM/DIA alongside sector ETFs

    Returns DataFrame sorted by total_return descending:
        symbol | sector | first_date | last_date | first_close | last_close | total_return_pct
    """
    df = q.load("sector_etfs", start=start, end=end)
    if df.empty:
        return df

    symbols = (SECTOR_ETFS + BROAD_ETFS) if include_broad else SECTOR_ETFS
    df = df[df["symbol"].isin(symbols)].copy()

    agg = (
        df.sort_values("date")
          .groupby("symbol")
          .agg(
              sector=("sector", "first"),
              first_date=("date", "first"),
              last_date=("date", "last"),
              first_close=("close", "first"),
              last_close=("close", "last"),
          )
          .reset_index()
    )
    agg["total_return_pct"] = (
        (agg["last_close"] / agg["first_close"] - 1) * 100
    ).round(2)
    return agg.sort_values("total_return_pct", ascending=False).reset_index(drop=True)


def sector_vs_spy(start: "str | None" = None) -> pd.DataFrame:
    """
    Each sector ETF's total return minus SPY's total return over the period.

    Positive = outperformed SPY; negative = underperformed.

    Returns DataFrame sorted by relative_return_pct descending:
        symbol | sector | total_return_pct | spy_return_pct | relative_return_pct
    """
    perf = sector_performance(start=start, include_broad=True)
    if perf.empty:
        return perf

    spy_row = perf[perf["symbol"] == "SPY"]
    if spy_row.empty:
        return perf[perf["symbol"].isin(SECTOR_ETFS)].copy()

    spy_return = spy_row["total_return_pct"].iloc[0]
    sectors = perf[perf["symbol"].isin(SECTOR_ETFS)].copy()
    sectors["spy_return_pct"]      = spy_return
    sectors["relative_return_pct"] = (sectors["total_return_pct"] - spy_return).round(2)
    return (
        sectors[["symbol", "sector", "total_return_pct", "spy_return_pct", "relative_return_pct"]]
        .sort_values("relative_return_pct", ascending=False)
        .reset_index(drop=True)
    )


def sector_rotation(lookback_days: int = 20) -> pd.DataFrame:
    """
    Recent momentum ranking: which sectors have the strongest short-term momentum.

    Uses the average daily log return over the lookback window as the signal.
    Higher rank = stronger recent momentum.

    Parameters
    ----------
    lookback_days : number of most-recent trading days to use (default: 20 ≈ 1 month)

    Returns DataFrame sorted by avg_log_return descending:
        rank | symbol | sector | avg_log_return | cumulative_return_pct | trading_days
    """
    df = q.load("sector_etfs")
    if df.empty:
        return df

    df = df[df["symbol"].isin(SECTOR_ETFS)].copy()
    df = df.sort_values("date")

    # Keep only the last N trading days per symbol
    recent = (
        df.groupby("symbol", group_keys=False)
          .apply(lambda g: g.tail(lookback_days))
    )

    agg = (
        recent.groupby("symbol")
              .agg(
                  sector=("sector", "first"),
                  avg_log_return=("log_return", "mean"),
                  trading_days=("date", "count"),
              )
              .reset_index()
    )
    agg["avg_log_return"]       = agg["avg_log_return"].round(6)
    agg["cumulative_return_pct"] = (
        (agg["avg_log_return"] * agg["trading_days"] * 100).round(2)
    )
    agg = agg.sort_values("avg_log_return", ascending=False).reset_index(drop=True)
    agg.insert(0, "rank", range(1, len(agg) + 1))
    return agg
