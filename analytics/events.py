"""
Events analytics: earnings calendar, insider transactions, EPS surprise,
dividends.

Requires earnings_calendar, insider_transactions, and dividends tables
populated by finnhub_events_pipeline.py and dividend_pipeline.py.
"""

import os
import sys
import datetime
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q


def upcoming_earnings(days_ahead: int = 30, days_back: int = 7) -> pd.DataFrame:
    """
    Earnings calendar for the window [today - days_back, today + days_ahead].

    Returns rows sorted by date ascending with EPS estimates where available.
    """
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=days_back)).isoformat()
    end = (today + datetime.timedelta(days=days_ahead)).isoformat()
    df = q.load("earnings_calendar", start=start, end=end)
    if df.empty:
        return df
    return df.sort_values("date").reset_index(drop=True)


def insider_sentiment(
    symbol: "str | list[str] | None" = None,
    days: int = 90,
) -> pd.DataFrame:
    """
    Net insider buy/sell sentiment over the past N days.

    Parameters
    ----------
    symbol : ticker or list (default: all)
    days   : lookback window in calendar days

    Returns DataFrame with:
        symbol | buy_txns | sell_txns | net_shares | sentiment
    Sentiment is 'bullish' (net buy), 'bearish' (net sell), or 'neutral'.
    """
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    df = q.load("insider_transactions", symbol=symbol, start=start)
    if df.empty:
        return df

    df = df.copy()
    txn_lower = df["transactionType"].str.lower().fillna("")
    df["is_buy"]  = txn_lower.str.contains("buy|purchase", regex=True)
    df["is_sell"] = txn_lower.str.contains("sell|sale", regex=True)

    agg = (df.groupby("symbol")
             .agg(
                 buy_txns=("is_buy", "sum"),
                 sell_txns=("is_sell", "sum"),
                 net_shares=("change", "sum"),
             )
             .reset_index())

    agg["buy_txns"]  = agg["buy_txns"].astype(int)
    agg["sell_txns"] = agg["sell_txns"].astype(int)
    agg["sentiment"] = agg["net_shares"].apply(
        lambda x: "bullish" if x > 0 else ("bearish" if x < 0 else "neutral")
    )
    return agg.sort_values("net_shares", ascending=False).reset_index(drop=True)


def dividend_history(
    symbols: "list[str] | str | None" = None,
    start: "str | None" = None,
) -> pd.DataFrame:
    """
    Cash dividend history per symbol, sorted by ex-date descending.

    Parameters
    ----------
    symbols : ticker or list (default: all)
    start   : 'YYYY-MM-DD' earliest ex-date to include

    Returns DataFrame with:
        symbol | ex_date | pay_date | record_date | declaration_date |
        amount | adj_amount | frequency | currency
    """
    df = q.load("dividends", symbol=symbols, start=start)
    if df.empty:
        return df
    return (
        df.sort_values("ex_date", ascending=False)
          .reset_index(drop=True)
    )


def dividend_calendar(days_ahead: int = 60, days_back: int = 7) -> pd.DataFrame:
    """
    Upcoming (and very recent) dividend ex-dates within the given window.

    Parameters
    ----------
    days_ahead : how many calendar days forward to look
    days_back  : how many calendar days back to include (catches recent ex-dates)

    Returns DataFrame sorted by ex_date ascending:
        symbol | ex_date | pay_date | amount | adj_amount | frequency
    """
    today  = datetime.date.today()
    start  = (today - datetime.timedelta(days=days_back)).isoformat()
    end    = (today + datetime.timedelta(days=days_ahead)).isoformat()
    df = q.load("dividends", start=start, end=end)
    if df.empty:
        return df

    cols = [c for c in ("symbol", "ex_date", "pay_date", "amount", "adj_amount", "frequency")
            if c in df.columns]
    return df[cols].sort_values("ex_date").reset_index(drop=True)


def news_sentiment(
    symbols: "list[str] | str | None" = None,
    days: int = 7,
) -> pd.DataFrame:
    """
    Claude-scored news sentiment for one or more symbols.

    Parameters
    ----------
    symbols : ticker or list (default: all)
    days    : lookback window in calendar days (default: 7)

    Returns DataFrame sorted by date descending:
        symbol | date | headline | sentiment | score | confidence | key_topics

    sentiment: "bullish" | "bearish" | "neutral"
    score    : -1.0 (very bearish) to +1.0 (very bullish)
    """
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    df = q.load("news_sentiment", symbol=symbols, start=start)
    if df.empty:
        return df
    return df.sort_values("date", ascending=False).reset_index(drop=True)


def sentiment_summary(
    symbols: "list[str] | str | None" = None,
    days: int = 7,
) -> pd.DataFrame:
    """
    Aggregate sentiment score per symbol over the lookback window.

    Returns DataFrame sorted by avg_score descending:
        symbol | articles | bullish | neutral | bearish | avg_score | net_sentiment
    """
    df = news_sentiment(symbols=symbols, days=days)
    if df.empty:
        return df

    agg = (
        df.groupby("symbol")
          .agg(
              articles=("headline", "count"),
              bullish=("sentiment", lambda s: (s == "bullish").sum()),
              neutral=("sentiment", lambda s: (s == "neutral").sum()),
              bearish=("sentiment", lambda s: (s == "bearish").sum()),
              avg_score=("score", "mean"),
          )
          .reset_index()
    )
    agg["avg_score"]     = agg["avg_score"].round(3)
    agg["net_sentiment"] = agg["avg_score"].apply(
        lambda x: "bullish" if x > 0.1 else ("bearish" if x < -0.1 else "neutral")
    )
    return agg.sort_values("avg_score", ascending=False).reset_index(drop=True)


def earnings_surprise(
    symbols: "list[str] | str | None" = None,
    n_quarters: int = 4,
) -> pd.DataFrame:
    """
    EPS actual vs estimate for the most recent N quarters per symbol.

    Positive surprise_pct = beat, negative = miss.

    Returns DataFrame with:
        symbol | date | epsEstimate | epsActual | surprise_pct
    """
    df = q.load("earnings_calendar", symbol=symbols)
    if df.empty:
        return df

    df = df.dropna(subset=["eps_actual", "eps_estimate"]).copy()
    df["surprise_pct"] = (
        (df["eps_actual"] - df["eps_estimate"]) / df["eps_estimate"].abs() * 100
    ).round(1)

    return (df.sort_values("date", ascending=False)
              .groupby("symbol")
              .head(n_quarters)
              [["symbol", "date", "eps_estimate", "eps_actual", "surprise_pct"]]
              .sort_values(["symbol", "date"])
              .reset_index(drop=True))
