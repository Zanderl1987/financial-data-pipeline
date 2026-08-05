"""
Fundamentals analytics: YoY growth, valuation multiples, metric rankings.

All functions return pandas DataFrames and require fundamentals_annual
(and prices for valuation ratios) to have data in storage/raw/.
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q


def yoy_growth(
    symbols: "list[str] | str | None" = None,
    metric: str = "revenue",
) -> pd.DataFrame:
    """
    Year-over-year growth for any annual fundamentals metric.

    Compares each company's most recent fiscal year against its own prior
    fiscal year, so staggered FY end dates are handled correctly.

    Parameters
    ----------
    symbols : ticker or list (default: all companies with data)
    metric  : fundamentals_annual metric name (default: 'revenue')

    Returns DataFrame indexed by symbol:
        fiscal_year | prior_B | current_B | yoy_pct | period_end
    """
    ann = q.load("fundamentals_annual")
    ann["period_end"] = pd.to_datetime(ann["period_end"], format="mixed")

    mask = (ann["metric"] == metric) & (ann["form"] == "10-K") & ann["symbol"].ne("")
    if symbols is not None:
        syms = [symbols] if isinstance(symbols, str) else list(symbols)
        mask &= ann["symbol"].isin(syms)

    rev = (ann[mask]
           .sort_values("period_end", ascending=False)
           .drop_duplicates(["symbol", "fiscal_year"])
           [["symbol", "fiscal_year", "value", "period_end"]]
           .sort_values(["symbol", "fiscal_year"]))

    rev["prior"] = rev.groupby("symbol")["value"].shift(1)

    latest = (rev.sort_values("fiscal_year", ascending=False)
                 .drop_duplicates("symbol")
                 .dropna(subset=["prior"])
                 .copy())

    latest["yoy_pct"]   = ((latest["value"] - latest["prior"]) / latest["prior"] * 100).round(1)
    latest["current_B"] = (latest["value"] / 1e9).round(2)
    latest["prior_B"]   = (latest["prior"]  / 1e9).round(2)

    return (latest.set_index("symbol")
                  [["fiscal_year", "prior_B", "current_B", "yoy_pct", "period_end"]]
                  .sort_values("yoy_pct", ascending=False))


def valuation(symbols: "list[str] | str | None" = None) -> pd.DataFrame:
    """
    P/E, P/S, and P/B ratios.

    Joins latest close price from 'prices' against the most recent 10-K
    filing for each metric. Requires both tables to have data.

    Returns DataFrame sorted by P/E ascending (cheapest first):
        symbol | price | pe | ps | pb | price_date
    """
    prices = q.load("prices", symbol=symbols, columns=["symbol", "date", "close"])
    if prices.empty:
        return pd.DataFrame(columns=["symbol", "price", "pe", "ps", "pb", "price_date"])

    latest_prices = (prices.sort_values("date", ascending=False)
                           .drop_duplicates("symbol")
                           .rename(columns={"close": "price", "date": "price_date"}))

    ann = q.load("fundamentals_annual", symbol=symbols)
    if ann.empty:
        return latest_prices.assign(pe=None, ps=None, pb=None)

    ann["period_end"] = pd.to_datetime(ann["period_end"], format="mixed")

    def _latest(metric_name: str, col: str) -> pd.DataFrame:
        return (ann[ann["metric"] == metric_name]
                .sort_values("period_end", ascending=False)
                .drop_duplicates("symbol")
                [["symbol", "value"]]
                .rename(columns={"value": col}))

    df = latest_prices
    df = df.merge(_latest("eps", "eps"), on="symbol", how="left")
    df = df.merge(_latest("bookValuePerShare", "bvps"), on="symbol", how="left")
    df = df.merge(_latest("revenue", "revenue"), on="symbol", how="left")
    df = df.merge(_latest("weightedAverageShares", "shares"), on="symbol", how="left")

    df["rev_per_share"] = (df["revenue"] / df["shares"]).where(df["shares"] > 0)
    df["pe"] = (df["price"] / df["eps"]).where(df["eps"] > 0).round(1)
    df["ps"] = (df["price"] / df["rev_per_share"]).where(df["rev_per_share"] > 0).round(1)
    df["pb"] = (df["price"] / df["bvps"]).where(df["bvps"] > 0).round(1)

    return (df[["symbol", "price", "pe", "ps", "pb", "price_date"]]
              .sort_values("pe")
              .reset_index(drop=True))


def top_by_metric(
    metric: str,
    n: int = 10,
    form: str = "10-K",
    ascending: bool = False,
) -> pd.DataFrame:
    """
    Top N (or bottom N) companies by a fundamentals metric, latest period.

    Parameters
    ----------
    metric    : e.g. 'revenue', 'netIncome', 'eps', 'grossMargin', 'totalDebt'
    n         : number of results (default: 10)
    form      : '10-K' for annual, '10-Q' for quarterly
    ascending : True returns bottom N (lowest values)

    Returns DataFrame with: symbol | value | value_B | period_end | fiscal_year
    """
    table = "fundamentals_annual" if form == "10-K" else "fundamentals_quarterly"
    ann = q.load(table)
    if ann.empty:
        return pd.DataFrame()

    ann["period_end"] = pd.to_datetime(ann["period_end"], format="mixed")

    sub = (ann[(ann["metric"] == metric) & (ann["form"] == form) & ann["symbol"].ne("")]
           .sort_values("period_end", ascending=False)
           .drop_duplicates("symbol")
           [["symbol", "value", "period_end", "fiscal_year"]]
           .sort_values("value", ascending=ascending)
           .head(n)
           .copy())

    sub["value_B"] = (sub["value"] / 1e9).round(3)
    return sub.reset_index(drop=True)
