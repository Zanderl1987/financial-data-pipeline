"""
Sentiment evaluation harness — does the news sentiment score predict returns?

Establishes a measurable baseline for the sentiment factor so scorer changes
(lexicon tweaks, FinBERT, ...) can be judged against a number instead of vibes.
Point-in-time safe: an article dated day T is only acted on at the close of the
first trading day AFTER T (entry_lag=1), because intraday/after-hours timing
within T is not reliable.

Method
------
1. Aggregate news_sentiment to one signal per (symbol, day):
   confidence-weighted mean score + article count.
2. For each signal, measure forward returns over 1/3/5/10/21 trading days,
   excess vs a benchmark (default SPY).
3. Report:
   - pooled Spearman rank correlation (signal vs forward excess return)
   - daily cross-sectional IC (days with >= --min-names symbols): mean, t-stat
   - bucket spread: mean forward excess return of bullish vs bearish signals

Usage
-----
  python sentiment_eval.py                    # full history, SPY benchmark
  python sentiment_eval.py --min-articles 2   # require 2+ articles per signal
  python sentiment_eval.py --benchmark ""     # raw (non-excess) returns

Interpreting: mean daily IC > ~0.02 with |t| > 2 is a real (if modest) signal;
bullish-minus-bearish spread should be positive and grow with horizon if the
score carries information.
"""

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query as q
from event_backtest import load_close_matrix, load_close

HORIZONS = (1, 3, 5, 10, 21)
BULLISH_MIN = 0.10   # keep in sync with news_sentiment_pipeline thresholds
BEARISH_MAX = -0.10


def daily_signals(min_articles: int = 1,
                  start: "str | None" = None,
                  end: "str | None" = None) -> pd.DataFrame:
    """
    One row per (symbol, date): confidence-weighted mean sentiment score.
    Columns: symbol, date, sent_score, n_articles.
    """
    df = q.load("news_sentiment", start=start, end=end)
    if df.empty:
        return pd.DataFrame(columns=["symbol", "date", "sent_score", "n_articles"])
    df = df.dropna(subset=["symbol", "date", "score"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    w = df["confidence"].clip(lower=0.05) if "confidence" in df.columns else 1.0
    df["_w"] = w
    df["_ws"] = df["score"] * df["_w"]
    agg = (df.groupby(["symbol", "date"])
             .agg(_ws=("_ws", "sum"), _w=("_w", "sum"), n_articles=("score", "size"))
             .reset_index())
    agg["sent_score"] = agg["_ws"] / agg["_w"]
    agg = agg[agg["n_articles"] >= min_articles]
    return agg[["symbol", "date", "sent_score", "n_articles"]].sort_values(["date", "symbol"])


def forward_returns(signals: pd.DataFrame,
                    horizons=HORIZONS,
                    benchmark: "str | None" = "SPY") -> pd.DataFrame:
    """
    Attach fwd_{h}d columns: excess-vs-benchmark return from the close of the
    first trading day after the signal date through h trading days later.
    Signals whose entry day is not within 5 calendar days of the signal (data
    gap) are dropped.
    """
    closes = load_close_matrix(signals["symbol"].unique())
    if closes.empty:
        raise RuntimeError("No price data for any signal symbol.")
    bench = load_close(benchmark) if benchmark else None
    if benchmark and (bench is None or bench.empty):
        raise RuntimeError(f"No price data for benchmark '{benchmark}'.")

    rows = []
    for sym, grp in signals.groupby("symbol"):
        if sym not in closes.columns:
            continue
        s = closes[sym].dropna()
        idx = s.index
        # entry at close of first trading day strictly after the signal date
        pos = idx.searchsorted(grp["date"] + pd.Timedelta(days=1), side="left")
        for (_, sig), p in zip(grp.iterrows(), pos):
            if p >= len(idx) or (idx[p] - sig["date"]).days > 5:
                continue
            row = dict(sig)
            row["entry_date"] = idx[p]
            base = s.iloc[p]
            for h in horizons:
                if p + h >= len(s):
                    row[f"fwd_{h}d"] = np.nan
                    continue
                r = s.iloc[p + h] / base - 1.0
                if bench is not None:
                    bp = bench.index.searchsorted(idx[p], side="left")
                    if bp + h < len(bench) and bench.index[bp] == idx[p]:
                        r -= bench.iloc[bp + h] / bench.iloc[bp] - 1.0
                    else:
                        r = np.nan
                row[f"fwd_{h}d"] = r
            rows.append(row)
    return pd.DataFrame(rows)


def evaluate(panel: pd.DataFrame, horizons=HORIZONS,
             min_names: int = 5) -> dict:
    """Compute pooled IC, daily cross-sectional IC, and bucket spreads."""
    out = {}
    for h in horizons:
        col = f"fwd_{h}d"
        if col not in panel.columns:
            continue
        sub = panel.dropna(subset=[col, "sent_score"])
        if len(sub) < 10:
            continue
        res = {"n": len(sub)}

        rho, p = stats.spearmanr(sub["sent_score"], sub[col])
        res["pooled_ic"] = round(float(rho), 4)
        res["pooled_p"] = round(float(p), 4)

        # daily cross-sectional IC
        ics = []
        for _, day in sub.groupby("date"):
            if day["symbol"].nunique() >= min_names and day["sent_score"].nunique() > 1:
                r, _ = stats.spearmanr(day["sent_score"], day[col])
                if np.isfinite(r):
                    ics.append(r)
        if len(ics) >= 5:
            ics = np.array(ics)
            res["mean_daily_ic"] = round(float(ics.mean()), 4)
            sd = ics.std(ddof=1)
            res["ic_t_stat"] = round(float(ics.mean() / (sd / math.sqrt(len(ics)))), 2) if sd > 0 else None
            res["ic_days"] = len(ics)
            res["ic_pct_positive"] = round(100 * float((ics > 0).mean()), 1)

        # bucket spread
        bull = sub.loc[sub["sent_score"] >= BULLISH_MIN, col]
        bear = sub.loc[sub["sent_score"] <= BEARISH_MAX, col]
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


def main():
    parser = argparse.ArgumentParser(description="Evaluate news sentiment vs forward returns")
    parser.add_argument("--min-articles", type=int, default=1,
                        help="Min articles per (symbol, day) signal (default 1)")
    parser.add_argument("--min-names", type=int, default=5,
                        help="Min symbols per day for cross-sectional IC (default 5)")
    parser.add_argument("--benchmark", default="SPY",
                        help="Benchmark for excess returns; '' for raw returns")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    print("[sentiment_eval] building daily signals...")
    sig = daily_signals(min_articles=args.min_articles, start=args.start, end=args.end)
    if sig.empty:
        print("No sentiment signals found. Run news_sentiment_pipeline.py first.")
        return
    print(f"  {len(sig):,} symbol-day signals | {sig['symbol'].nunique()} symbols | "
          f"{sig['date'].min().date()} -> {sig['date'].max().date()}")

    bench = args.benchmark or None
    print(f"[sentiment_eval] computing forward returns (benchmark: {bench or 'none'})...")
    panel = forward_returns(sig, benchmark=bench)
    if panel.empty:
        print("No signals had usable price data.")
        return
    print(f"  {len(panel):,} signals matched to prices")

    results = evaluate(panel, min_names=args.min_names)

    print("\n=== SENTIMENT PREDICTIVE POWER" + (" (excess vs %s)" % bench if bench else "") + " ===")
    hdr = (f"{'h':>3} {'n':>6} {'pooledIC':>9} {'p':>7} {'dailyIC':>8} {'t':>6} "
           f"{'days':>5} {'%pos':>5} {'bull%':>7} {'bear%':>7} {'spread%':>8} {'t':>6}")
    print(hdr)
    for h, r in results.items():
        print(f"{h:>3} {r['n']:>6} {r.get('pooled_ic', float('nan')):>9} "
              f"{r.get('pooled_p', float('nan')):>7} "
              f"{str(r.get('mean_daily_ic', '-')):>8} {str(r.get('ic_t_stat', '-')):>6} "
              f"{str(r.get('ic_days', '-')):>5} {str(r.get('ic_pct_positive', '-')):>5} "
              f"{str(r.get('bull_mean_pct', '-')):>7} {str(r.get('bear_mean_pct', '-')):>7} "
              f"{str(r.get('spread_pct', '-')):>8} {str(r.get('spread_t', '-')):>6}")

    print("\nGuide: dailyIC > 0.02 with |t| > 2 = real signal; spread% should be")
    print("positive (bullish outperforms bearish) and grow with horizon.")


if __name__ == "__main__":
    main()
