"""
Short interest analytics: squeeze candidates, trend changes, FTD pressure.

Requires short_interest, finra_short_interest, and/or sec_ftd tables
populated by short_interest_pipeline.py.
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q


def squeeze_candidates(
    min_short_pct: float = 0.15,
    max_days_to_cover: float = 10.0,
    source: str = "yfinance",
) -> pd.DataFrame:
    """
    Stocks with high short interest relative to float — potential squeeze fuel.

    Screens for symbols where short_pct_float >= min_short_pct AND
    days_to_cover <= max_days_to_cover (a large DTC means shorts can't exit
    quickly even if they want to, amplifying a squeeze).

    Parameters
    ----------
    min_short_pct    : minimum short % of float as decimal (default 0.15 = 15%)
    max_days_to_cover: ceiling on days-to-cover (default 10; set higher to widen)
    source           : "yfinance" or "finra"

    Returns DataFrame sorted by short_pct_float descending:
        symbol | short_pct_float | days_to_cover | shares_short |
        float_shares | filing_date
    """
    table = "short_interest" if source == "yfinance" else "finra_short_interest"
    df = q.load(table)
    if df.empty:
        return df

    # Keep latest snapshot per symbol
    if "snapshot_date" in df.columns:
        df = df.sort_values("snapshot_date").groupby("symbol").last().reset_index()

    mask = df["short_pct_float"] >= min_short_pct
    if "days_to_cover" in df.columns:
        mask &= df["days_to_cover"].fillna(999) <= max_days_to_cover

    result = df[mask].copy()
    result["short_pct_float"] = result["short_pct_float"].round(4)

    cols = [c for c in [
        "symbol", "short_pct_float", "days_to_cover",
        "shares_short", "float_shares", "filing_date",
    ] if c in result.columns]
    return result[cols].sort_values("short_pct_float", ascending=False).reset_index(drop=True)


def short_change(
    symbols: "list[str] | str | None" = None,
    periods: int = 2,
) -> pd.DataFrame:
    """
    Change in short interest across the last N yfinance snapshots.

    Compares the most recent snapshot to N snapshots ago to show
    whether short sellers are increasing or covering their positions.

    Parameters
    ----------
    symbols : ticker or list (default: all)
    periods : how many snapshots back to compare (default: 2)

    Returns DataFrame sorted by pct_change_short descending:
        symbol | latest_date | prior_date | shares_short_latest |
        shares_short_prior | change_shares | pct_change_short
    """
    df = q.load("short_interest", symbol=symbols)
    if df.empty:
        return df

    df = df.sort_values("snapshot_date")

    def _compare(g: pd.DataFrame) -> pd.Series | None:
        if len(g) < 2:
            return None
        latest = g.iloc[-1]
        prior  = g.iloc[max(-periods - 1, -len(g))]
        if pd.isna(latest["shares_short"]) or pd.isna(prior["shares_short"]):
            return None
        change = latest["shares_short"] - prior["shares_short"]
        pct    = change / prior["shares_short"] * 100 if prior["shares_short"] else None
        return pd.Series({
            "latest_date":          latest["snapshot_date"],
            "prior_date":           prior["snapshot_date"],
            "shares_short_latest":  latest["shares_short"],
            "shares_short_prior":   prior["shares_short"],
            "change_shares":        change,
            "pct_change_short":     round(pct, 2) if pct is not None else None,
        })

    result = (
        df.groupby("symbol")
          .apply(_compare)
          .dropna()
          .reset_index()
    )
    if result.empty:
        return result

    return result.sort_values("pct_change_short", ascending=False).reset_index(drop=True)


def ftd_pressure(
    symbols: "list[str] | str | None" = None,
    top_n: int = 20,
) -> pd.DataFrame:
    """
    Aggregate SEC Fails-to-Deliver by symbol — a proxy for naked short pressure.

    High FTD relative to average daily volume or float suggests settlement
    stress; combined with high short interest it flags squeeze candidates.

    Parameters
    ----------
    symbols : ticker or list to filter (default: all)
    top_n   : return top N symbols by total fails (default: 20)

    Returns DataFrame sorted by total_shares_failed descending:
        symbol | description | total_shares_failed | avg_price |
        trading_days | latest_settlement
    """
    df = q.load("sec_ftd", symbol=symbols)
    if df.empty:
        return df

    agg = (
        df.groupby("symbol")
          .agg(
              description=("description", "first"),
              total_shares_failed=("shares_failed", "sum"),
              avg_price=("price", "mean"),
              trading_days=("settlement_date", "nunique"),
              latest_settlement=("settlement_date", "max"),
          )
          .reset_index()
    )
    agg["total_shares_failed"] = agg["total_shares_failed"].astype("Int64")
    agg["avg_price"]           = agg["avg_price"].round(2)

    return (
        agg.sort_values("total_shares_failed", ascending=False)
           .head(top_n)
           .reset_index(drop=True)
    )


def short_vs_ftd(symbols: "list[str] | str | None" = None) -> pd.DataFrame:
    """
    Join latest short interest snapshot with total FTD per symbol.

    Symbols with both high short_pct_float and high FTD are the strongest
    squeeze / forced-covering candidates.

    Returns DataFrame sorted by short_pct_float descending:
        symbol | short_pct_float | days_to_cover | shares_short |
        total_shares_failed | filing_date
    """
    si = q.load("short_interest", symbol=symbols)
    ftd = q.load("sec_ftd", symbol=symbols)

    if si.empty:
        return si

    # Latest snapshot per symbol
    if "snapshot_date" in si.columns:
        si = si.sort_values("snapshot_date").groupby("symbol").last().reset_index()

    if ftd.empty:
        return si[["symbol", "short_pct_float", "days_to_cover",
                   "shares_short", "filing_date"]].copy()

    ftd_agg = ftd.groupby("symbol")["shares_failed"].sum().reset_index()
    ftd_agg = ftd_agg.rename(columns={"shares_failed": "total_shares_failed"})

    merged = si.merge(ftd_agg, on="symbol", how="left")
    cols = [c for c in [
        "symbol", "short_pct_float", "days_to_cover",
        "shares_short", "total_shares_failed", "filing_date",
    ] if c in merged.columns]
    return (
        merged[cols]
        .sort_values("short_pct_float", ascending=False, na_position="last")
        .reset_index(drop=True)
    )
