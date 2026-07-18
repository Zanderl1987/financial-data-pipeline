"""
tv_rating_eval.py — statistical evaluation of the local TradingView Technical
Rating replica (analytics.technical.tv_rating) against forward returns, plus
a rule-based trade simulation.

Point-in-time safe: the rating for day T is computed from day T's own OHLCV
close (no external publication lag to model), but every simulated action
(entering/exiting a position, measuring forward returns) executes at day
T+1's close at the earliest -- never same-day, since intraday you don't yet
have the close the rating depends on.

Method
------
1. Build a per-symbol cache of analytics.technical.rating_history() frames
   (all 69 tiingo_prices symbols by default, full available history).
2. Level-IC evaluation: for rating_all/rating_ma/rating_osc, measure forward
   returns (excess vs SPY) at 1/3/5/10/21 trading days; report pooled +
   daily cross-sectional Spearman IC with t-stats, and a strong_buy vs
   strong_sell bucket spread. Same method as sentiment_eval.py, generalized
   to 3 signal columns.
3. Transition study: reuse event_backtest.rating_changes() to find every
   rating_label change, then event_backtest.event_study() per (from, to)
   pair for the average cumulative-return path and its significance.
4. Trade simulation: threshold-cross long/short rule on rating_all (see
   simulate_trades docstring), fixed $10k notional per trade.

Usage
-----
  python tv_rating_eval.py                      # full 69-symbol universe
  python tv_rating_eval.py --symbols AAPL,MSFT  # faster iteration subset
  python tv_rating_eval.py --start 2015-01-01

Output (storage/reports/tv_rating_eval/):
  ic_stats.json       -- level-IC + transition significance stats
  panel.parquet       -- symbol-day signal + forward-return panel (+ close)
  transitions.parquet -- mean cumulative-return path per transition type
  trades.parquet      -- one row per simulated trade

Interpreting: mean daily IC > ~0.02 with |t| > 2 is a real (if modest)
signal; |IC| > 0.05 on daily data is suspicious, hunt for a leak. t-stat
needs >= ~250 days to trust. Sign flips across horizons = noise.
"""

import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query as q
import event_backtest as eb

SIGNALS = ("rating_all", "rating_ma", "rating_osc")
HORIZONS = (1, 3, 5, 10, 21)
BENCHMARK = "SPY"
PRICE_TABLE = "tiingo_prices"
BULL_MIN = 0.5
BEAR_MAX = -0.5
EXIT_LONG_MAX = 0.1
EXIT_SHORT_MIN = -0.1
NOTIONAL = 10_000.0
OUT_DIR = "storage/reports/tv_rating_eval"


def universe(price_table: str = PRICE_TABLE) -> "list[str]":
    """All symbols available in the given price table, sorted."""
    return q.symbols(price_table)


def build_signal_cache(symbols, price_table: str = PRICE_TABLE,
                       start: "str | None" = None,
                       end: "str | None" = None) -> "dict[str, pd.DataFrame]":
    """
    One analytics.technical.rating_history() frame per symbol, keyed by
    symbol. Symbols with no usable price data are skipped.
    """
    from analytics.technical import rating_history
    cache = {}
    for sym in symbols:
        d = rating_history(sym, price_table=price_table, start=start, end=end)
        if not d.empty:
            cache[sym] = d
    return cache


def build_return_panel(cache: "dict[str, pd.DataFrame]",
                       horizons=HORIZONS,
                       benchmark: "str | None" = BENCHMARK) -> pd.DataFrame:
    """
    Tidy (symbol, date) panel: close, rating_all/rating_ma/rating_osc/
    rating_label, plus fwd_{h}d forward excess returns for each horizon.

    Entry executes at the close of the day AFTER the signal date (no
    same-day look-ahead). fwd_{h}d is the return from that entry close to
    h trading days later, excess vs `benchmark`'s matching path (reindexed
    onto the symbol's own dates). The benchmark symbol itself is excluded
    from the output panel, since its excess return vs itself is identically
    zero and would only dilute downstream stats.
    """
    bench_close = None
    if benchmark and benchmark in cache:
        bench_close = cache[benchmark]["close"]

    frames = []
    for sym, d in cache.items():
        if sym == benchmark:
            continue
        c = d["close"]
        entry = c.shift(-1)
        bench_reidx = bench_close.reindex(d.index) if bench_close is not None else None
        bench_entry = bench_reidx.shift(-1) if bench_reidx is not None else None

        out = pd.DataFrame({
            "symbol": sym,
            "date": d.index,
            "close": c.values,
            "rating_all": d["rating_all"].values,
            "rating_ma": d["rating_ma"].values,
            "rating_osc": d["rating_osc"].values,
            "rating_label": d["rating_label"].values,
        })
        for h in horizons:
            exit_ = c.shift(-(1 + h))
            ret = exit_ / entry - 1.0
            if bench_reidx is not None:
                bench_exit = bench_reidx.shift(-(1 + h))
                ret = ret - (bench_exit / bench_entry - 1.0)
            out[f"fwd_{h}d"] = ret.values
        frames.append(out)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
