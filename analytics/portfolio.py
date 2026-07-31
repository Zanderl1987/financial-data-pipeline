"""
Portfolio analytics: sector exposure, overweight vs. a benchmark, top
holdings, holdings overlap, and bond duration/YTM profiles.

Draws on the Iceberg-backed constituents tables (fund_holdings, etf_holdings)
populated by fund_holdings_pipeline.py and etf_holdings_pipeline.py. A fund's
holdings can live in either table depending on which source built it (BlackRock
iShares vs. SecuritiesDB), so every lookup here checks both.

CAVEAT: the two source tables use different, non-normalized sector taxonomies
(BlackRock: "Health Care", "Financials", "Information Technology" vs.
SecuritiesDB: "Healthcare", "Financial Services", "Technology"). sector_
exposure() and sector_overweight() do NOT reconcile these -- comparing funds
from different sources (e.g. IWM/BlackRock vs SPY/SecuritiesDB) will show
each vendor's sector as a separate row instead of merging equivalent sectors,
which reads as far more "overweight/underweight" than reality. Comparisons
between two funds from the SAME source (e.g. IWM vs IVV, both BlackRock) are
reliable today; cross-source comparisons need a sector-name mapping first.
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q

_HOLDINGS_TABLES = ("fund_holdings", "etf_holdings")


def _latest_holdings(fund_ticker: str) -> pd.DataFrame:
    """Latest-snapshot holdings for a fund, checking fund_holdings then
    etf_holdings (whichever actually has data for this ticker)."""
    for table in _HOLDINGS_TABLES:
        df = q.load(table)
        if df.empty or "fund_ticker" not in df.columns:
            continue
        fund_df = df[df["fund_ticker"] == fund_ticker]
        if fund_df.empty:
            continue
        latest_date = fund_df["snapshot_date"].max()
        return fund_df[fund_df["snapshot_date"] == latest_date].copy()
    return pd.DataFrame()


def sector_exposure(fund_ticker: str) -> pd.DataFrame:
    """
    Sector weight breakdown for a fund's most recent holdings snapshot.

    Parameters
    ----------
    fund_ticker : e.g. 'IWM', 'SPY', 'AGG'

    Returns DataFrame sorted by weight_pct descending:
        sector | weight_pct
    """
    df = _latest_holdings(fund_ticker)
    if df.empty or "sector" not in df.columns:
        return pd.DataFrame(columns=["sector", "weight_pct"])

    agg = (
        df.dropna(subset=["sector"])
          .groupby("sector", as_index=False)["weight_pct"]
          .sum()
    )
    agg["weight_pct"] = agg["weight_pct"].round(2)
    return agg.sort_values("weight_pct", ascending=False).reset_index(drop=True)


def sector_overweight(fund_ticker: str, benchmark_ticker: str) -> pd.DataFrame:
    """
    Per-sector weight difference between a fund and a benchmark fund's most
    recent holdings snapshots (e.g. "what sectors does IWM overweight vs SPY").

    Positive overweight_pct = fund_ticker holds more of that sector than
    benchmark_ticker; negative = underweight.

    Reliable only when both funds' holdings come from the same source table
    (both BlackRock or both SecuritiesDB) -- see module docstring's sector-
    taxonomy caveat for cross-source comparisons.

    Parameters
    ----------
    fund_ticker      : the portfolio being evaluated, e.g. 'IWM'
    benchmark_ticker : the benchmark to compare against, e.g. 'SPY'

    Returns DataFrame sorted by overweight_pct descending:
        sector | fund_weight_pct | benchmark_weight_pct | overweight_pct
    """
    fund = sector_exposure(fund_ticker).rename(columns={"weight_pct": "fund_weight_pct"})
    bench = sector_exposure(benchmark_ticker).rename(columns={"weight_pct": "benchmark_weight_pct"})
    if fund.empty or bench.empty:
        return pd.DataFrame(columns=["sector", "fund_weight_pct", "benchmark_weight_pct", "overweight_pct"])

    merged = fund.merge(bench, on="sector", how="outer").fillna(0.0)
    merged["overweight_pct"] = (merged["fund_weight_pct"] - merged["benchmark_weight_pct"]).round(2)
    return merged.sort_values("overweight_pct", ascending=False).reset_index(drop=True)


def top_holdings(fund_ticker: str, n: int = 10) -> pd.DataFrame:
    """
    A fund's largest positions by weight in its most recent snapshot.

    Parameters
    ----------
    fund_ticker : e.g. 'IWM'
    n           : number of holdings to return (default 10)

    Returns DataFrame sorted by weight_pct descending:
        holding_ticker | holding_name | sector | weight_pct | market_value_usd
    """
    df = _latest_holdings(fund_ticker)
    if df.empty:
        return pd.DataFrame(columns=["holding_ticker", "holding_name", "sector", "weight_pct", "market_value_usd"])

    cols = [c for c in ["holding_ticker", "holding_name", "sector", "weight_pct", "market_value_usd"] if c in df.columns]
    return (
        df[cols]
          .sort_values("weight_pct", ascending=False)
          .head(n)
          .reset_index(drop=True)
    )


def holdings_overlap(fund_a: str, fund_b: str) -> pd.DataFrame:
    """
    Holdings shared between two funds' most recent snapshots, with each
    fund's weight in that position.

    Parameters
    ----------
    fund_a, fund_b : fund tickers to compare, e.g. 'IVV', 'ITOT'

    Returns DataFrame sorted by combined weight descending:
        holding_ticker | holding_name | {fund_a}_weight_pct | {fund_b}_weight_pct
    """
    a = _latest_holdings(fund_a)
    b = _latest_holdings(fund_b)
    col_a, col_b = f"{fund_a}_weight_pct", f"{fund_b}_weight_pct"
    empty = pd.DataFrame(columns=["holding_ticker", "holding_name", col_a, col_b])
    if a.empty or b.empty or "holding_ticker" not in a.columns or "holding_ticker" not in b.columns:
        return empty

    a2 = a.dropna(subset=["holding_ticker"])[["holding_ticker", "holding_name", "weight_pct"]].rename(columns={"weight_pct": col_a})
    b2 = b.dropna(subset=["holding_ticker"])[["holding_ticker", "weight_pct"]].rename(columns={"weight_pct": col_b})
    merged = a2.merge(b2, on="holding_ticker", how="inner")
    if merged.empty:
        return empty
    merged["_combined"] = merged[col_a] + merged[col_b]
    return (
        merged.sort_values("_combined", ascending=False)
              .drop(columns="_combined")
              .reset_index(drop=True)
    )


def bond_duration_profile(fund_ticker: str) -> pd.DataFrame:
    """
    Weight-weighted duration/YTM/coupon/maturity profile for a fixed-income
    fund's most recent holdings snapshot (e.g. AGG, LQD, HYG, TIP).

    Returns a single-row DataFrame:
        fund_ticker | weighted_duration | weighted_ytm_pct | weighted_coupon_pct |
        avg_maturity_date | n_holdings
    """
    df = _latest_holdings(fund_ticker)
    cols = ["fund_ticker", "weighted_duration", "weighted_ytm_pct",
            "weighted_coupon_pct", "avg_maturity_date", "n_holdings"]
    if df.empty or "duration" not in df.columns:
        return pd.DataFrame(columns=cols)

    bonds = df.dropna(subset=["duration"])
    if bonds.empty:
        return pd.DataFrame(columns=cols)

    w = bonds["weight_pct"].fillna(0.0)
    w_sum = w.sum()
    if w_sum == 0:
        return pd.DataFrame(columns=cols)

    def wavg(col):
        vals = bonds[col]
        mask = vals.notna()
        if not mask.any():
            return None
        return (vals[mask] * w[mask]).sum() / w[mask].sum()

    maturity_numeric = pd.to_datetime(bonds["maturity_date"], errors="coerce").dropna()
    avg_maturity = maturity_numeric.mean() if not maturity_numeric.empty else None

    return pd.DataFrame([{
        "fund_ticker": fund_ticker,
        "weighted_duration": round(wavg("duration"), 2) if wavg("duration") is not None else None,
        "weighted_ytm_pct": round(wavg("ytm_pct"), 2) if wavg("ytm_pct") is not None else None,
        "weighted_coupon_pct": round(wavg("coupon_pct"), 2) if wavg("coupon_pct") is not None else None,
        "avg_maturity_date": avg_maturity.date() if avg_maturity is not None else None,
        "n_holdings": len(bonds),
    }])
