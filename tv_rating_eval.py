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


def evaluate_signal(panel: pd.DataFrame, signal_col: str, horizons=HORIZONS,
                    min_names: int = 5, bull_min: float = BULL_MIN,
                    bear_max: float = BEAR_MAX) -> dict:
    """
    Pooled + daily cross-sectional IC and a bullish/bearish bucket spread
    for one signal column, at each horizon. Same method as
    sentiment_eval.evaluate(), generalized to an arbitrary signal column
    and configurable bull/bear thresholds (default +-0.5, TV's own
    strong_buy/strong_sell cutoffs, since rating_all/ma/osc share the
    [-1, 1] scale).
    """
    out = {}
    for h in horizons:
        col = f"fwd_{h}d"
        if col not in panel.columns:
            continue
        sub = panel.dropna(subset=[col, signal_col])
        if len(sub) < 10:
            continue
        res = {"n": len(sub)}

        rho, p = stats.spearmanr(sub[signal_col], sub[col])
        res["pooled_ic"] = round(float(rho), 4)
        res["pooled_p"] = round(float(p), 4)

        ics = []
        for _, day in sub.groupby("date"):
            if day["symbol"].nunique() >= min_names and day[signal_col].nunique() > 1:
                r, _ = stats.spearmanr(day[signal_col], day[col])
                if np.isfinite(r):
                    ics.append(r)
        if len(ics) >= 5:
            ics = np.array(ics)
            sd = ics.std(ddof=1)
            se = sd / math.sqrt(len(ics))
            res["mean_daily_ic"] = round(float(ics.mean()), 4)
            res["ic_se"] = round(float(se), 5) if sd > 0 else None
            res["ic_t_stat"] = round(float(ics.mean() / se), 2) if sd > 0 else None
            res["ic_days"] = len(ics)
            res["ic_pct_positive"] = round(100 * float((ics > 0).mean()), 1)

        bull = sub.loc[sub[signal_col] >= bull_min, col]
        bear = sub.loc[sub[signal_col] <= bear_max, col]
        res["bull_n"], res["bear_n"] = len(bull), len(bear)
        res["bull_mean_pct"] = round(100 * float(bull.mean()), 3) if len(bull) else None
        res["bear_mean_pct"] = round(100 * float(bear.mean()), 3) if len(bear) else None
        if len(bull) > 5 and len(bear) > 5:
            spread = float(bull.mean() - bear.mean())
            t, p2 = stats.ttest_ind(bull, bear, equal_var=False)
            res["spread_pct"] = round(100 * spread, 3)
            res["spread_t"] = round(float(t), 2)
            res["spread_p"] = round(float(p2), 4)
        out[h] = res
    return out
