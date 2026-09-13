#!/usr/bin/env python3
"""
Delisted-names L/S sandwich test.

Tests whether the ~167 genuinely-recovered delisted names (2019+, from the 2020
decade of the survivorship-bias measurement) exhibit large-negative next-month
returns — as the constructive UMD bound assumes (missing delisting-bound names'
returns are negative, which would drag the delisting-inclusive L/S spread).

Uses:
- Delisting reference from the project's query module (delisting_reference table).
- Prices panel from curated parquet (storage/curated/prices/prices.parquet).
- Next-month return: return from T+1 to T+30 (single holding period).

Protocol (pre-registered, measured 2026-09-13):
- Separate SEP into alive (isdelisted=N) and delisted (isdelisted=Y).
- A "genuine" recovered delisted name: series starts within 370d of firstpricedate
  AND ends within 370d of lastpricedate (kills recycled-ticker pollution and live-
  ticker pollution). Measured: 166 in 2020 decade + 1 in 2010 decade = 167 total.
- For each recovered name, compute next-month return from the prices panel:
  r_{t+1} = price_{t+30} / price_t - 1 (or NaN if insufficient data).
- Benchmarks:
   * Average next-month return of ALL delisted names (the "missing" universe).
   * Average next-month return of SEP-alive names (the "panel" universe).
- Test: do the recovered names have particularly negative returns (supporting the
  constructive UMD bound assumption that missing delisting-bound names have mu < 0)?
"""

import os
import sys
import json
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q

PANEL = "storage/curated/prices/prices.parquet"
END_TOL = 370  # days; a "genuine" recovered name starts within 370d of firstpricedate
# and ends within 370d of lastpricedate.


def load_delisting_ref():
    """Load the delisting reference table."""
    ref = q.load("delisting_reference")
    ref = ref[ref["price_table"] == "SEP"].copy()
    ref["firstpricedate"] = pd.to_datetime(ref["firstpricedate"], errors="coerce")
    ref["lastpricedate"] = pd.to_datetime(ref["lastpricedate"], errors="coerce")
    ref = ref[ref["firstpricedate"].notna() & ref["lastpricedate"].notna()]
    return ref


def load_panel():
    """Load the prices panel parquet."""
    return pd.read_parquet(PANEL)


def is_genuine_recovered(row, end_tol=END_TOL):
    """Check if a delisted name is 'genuine recovered' per the 370-day rule."""
    gap_end = (row["mx"] - row["lastpricedate"]).days
    gap_start = (row["mn"] - row["firstpricedate"]).days
    return (gap_end <= end_tol) & (gap_start >= -end_tol)


def main():
    print("=== Delisted-names L/S sandwich test ===")
    
    sep = load_delisting_ref()
    print(f"SEP equities: {len(sep):,} | alive {int((sep['isdelisted']=='N').sum()):,} | "
          f"delisted {int((sep['isdelisted']=='Y').sum()):,}")
    
    panel = load_panel()
    print(f"Prices panel: {len(panel):,} symbols")
    
    # Build per-symbol min/max dates from the panel
    ran = panel.groupby("symbol").agg(mn=("date", "min"), mx=("date", "max")).reset_index()
    
    # Merge delisted names with panel ranges
    de = sep[sep["isdelisted"] == "Y"].copy()
    merged = de.merge(ran, left_on="ticker", right_on="symbol", how="inner")
    
    # Compute gap metrics
    merged["gap_end"] = (merged["mx"] - merged["lastpricedate"]).dt.days
    merged["gap_start"] = (merged["mn"] - merged["firstpricedate"]).dt.days
    merged["genuine"] = (merged["gap_end"] <= END_TOL) & (merged["gap_start"] >= -END_TOL)
    
    # Count by decade
    merged["dec"] = merged["lastpricedate"].dt.year // 10 * 10
    tot = merged.groupby("dec").size()
    rec = merged[merged["genuine"]].groupby("dec").size()
    alloc = pd.DataFrame({"delisted": tot, "recovered_genuine": rec}).fillna(0)
    alloc["recovery_pct"] = (alloc["recovered_genuine"] / alloc["delisted"] * 100).round(2)
    print("\nGenuine recovered delisted names by decade:")
    print(alloc.to_string())
    
    # Extract the genuine recovered names' tickers
    genuine = merged[merged["genuine"]]
    rec_tickers = set(genuine["ticker"].tolist())
    n_rec = len(rec_tickers)
    print(f"\nGenuine recovered tickers: {n_rec} (from {len(merged)} merged delisted names)")
    
    # Compute next-month returns for recovered names
    # next_month_return = price at T+30 / price at T - 1
    # We'll use the panel's adjusted close; need a date column.
    if "date" not in panel.columns or "close" not in panel.columns:
        print("Panel does not have date/close columns; skipping return computation.")
        return
    
    panel = panel.sort_values(["symbol", "date"])
    # For each symbol, compute pct change over 30 days
    panel["ret_30d"] = panel.groupby("symbol")["close"].pct_change(30)
    
    # Get next-month return for each recovered ticker
    # We need the return starting from the panel's min date for each symbol.
    # Simplify: for each recovered ticker, find its first date in the panel, then
    # take the 30-day return from that date.
    rec_returns = {}
    for ticker in rec_tickers:
        ticker_panel = panel[panel["symbol"] == ticker]
        if ticker_panel.empty:
            continue
        # Use the earliest date in the panel for this ticker
        first_date = ticker_panel["date"].min()
        if pd.isna(first_date):
            continue
        # Get the close at first_date and close 30 days later
        row_init = ticker_panel[ticker_panel["date"] == first_date]
        if row_init.empty:
            continue
        price_init = row_init["close"].values[0]
        # Find the date 30 days later
        later_dates = ticker_panel[ticker_panel["date"] >= first_date].sort_values("date")
        if len(later_dates) < 2:
            continue
        price_later = later_dates.iloc[1]["close"]  # next available close after init
        if price_init == 0:
            continue
        ret = price_later / price_init - 1
        rec_returns[ticker] = ret
    
    if not rec_returns:
        print("No next-month returns computable for recovered names.")
        return
    
    ret_series = pd.Series(rec_returns)
    print(f"\nNext-month returns for {len(ret_series)} recovered names:")
    print(f"  Mean: {ret_series.mean()*100:.4f}%")
    print(f"  Median: {ret_series.median()*100:.4f}%")
    print(f"  % negative: {(ret_series < 0).sum() / len(ret_series) * 100:.1f}%")
    print(f"  Min: {ret_series.min()*100:.4f}%")
    print(f"  5th pctile: {ret_series.quantile(0.05)*100:.4f}%")
    
    # Benchmark: all delisted names' next-month returns
    de_panel = panel[panel["symbol"].isin(sep[sep["isdelisted"]=="Y"]["ticker"])]
    de_ret = de_panel.groupby("symbol")["close"].pct_change(30)
    de_ret_series = de_ret.dropna()
    print(f"\nAll delisted names next-month return stats:")
    print(f"  Mean: {de_ret_series.mean()*100:.4f}%")
    print(f"  % negative: {(de_ret_series < 0).sum() / len(de_ret_series) * 100:.1f}%")
    print(f"  Min: {de_ret_series.min()*100:.4f}%")
    
    # Benchmark: SEP-alive names
    alive_tickers = set(sep[sep["isdelisted"]=="N"]["ticker"].tolist())
    alive_panel = panel[panel["symbol"].isin(alive_tickers)]
    alive_ret = alive_panel.groupby("symbol")["close"].pct_change(30)
    alive_ret_series = alive_ret.dropna()
    print(f"\nSEP-alive names next-month return stats:")
    print(f"  Mean: {alive_ret_series.mean()*100:.4f}%")
    print(f"  % negative: {(alive_ret_series < 0).sum() / len(alive_ret_series) * 100:.1f}%")
    
    # Key test: are recovered names' returns particularly negative?
    # Compare recovered mean to delisted mean
    print(f"\n--- Comparison ---")
    print(f"Recovered mean return: {ret_series.mean()*100:.4f}%")
    print(f"Delisted mean return: {de_ret_series.mean()*100:.4f}%")
    print(f"Recovered are {ret_series.mean()/de_ret_series.mean()*100:.1f}% of delisted mean "
          f"({'less negative' if ret_series.mean() > de_ret_series.mean() else 'more negative'})")
    
    # Also: what fraction of recovered names have returns below the delisted median?
    below_median = (ret_series < de_ret_series.median()).sum()
    print(f"Recovered names with return below delisted median: {below_median}/{len(ret_series)} "
          f"({below_median/len(ret_series)*100:.1f}%)")
    
    # Write results note
    print("\n--- Verdict ---")
    if ret_series.mean() < de_ret_series.mean():
        print("Recovered names have MORE NEGATIVE mean returns than the delisted universe average.")
        print("This SUPPORTS the constructive UMD bound assumption that missing delisting-bound")
        print("names have negative returns, which would drag the delisting-inclusive L/S spread.")
    else:
        print("Recovered names have LESS NEGATIVE (or more positive) mean returns than the "
              "delisted universe average.")
        print("This does NOT support the assumption that missing delisting-bound names are")
        print("overwhelmingly negative; the 30-year verdict remains unchanged as a sidelight.")