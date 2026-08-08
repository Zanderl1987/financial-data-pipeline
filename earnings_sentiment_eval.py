#!/usr/bin/env python3
"""
Earnings Sentiment Evaluation -- crosswalk joining:
  1. Earnings dates (earnings_calendar with EPS actual vs estimate)
  2. Pre-earnings news sentiment (filtered to earnings-tagged articles)
  3. EPS surprise magnitude
  4. Post-earnings price reaction (CAR around earnings date)

Produces a single panel that the dashboard and evaluation adapters consume.

CLI:
  python earnings_sentiment_eval.py                    # build panel, print stats
  python earnings_sentiment_eval.py --sentiment-window 10  # look back 10 days
  python earnings_sentiment_eval.py --car-window 10    # 10-day CAR window
  python earnings_sentiment_eval.py --export panel.csv # save to CSV

Output columns:
  symbol | date | eps_estimate | eps_actual | surprise_pct | surprise_dir |
  sent_score | sent_n_articles | sent_confidence |
  car_{h}d (cumulative abnormal return) |
  fwd_{h}d (forward return from entry)
"""

import argparse
import os
import sys
import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query as q
from event_backtest import load_close, load_close_matrix

DEFAULT_SENTIMENT_WINDOW = 5
CAR_HORIZONS = [1, 2, 3, 5, 10]
FWD_HORIZONS = [1, 3, 5, 10, 21]
BULLISH_THRESHOLD = 0.10
BEARISH_THRESHOLD = -0.10
BENCHMARK = "SPY"


def load_earnings_events(symbols=None, start=None, end=None):
    df = q.load("earnings_calendar", symbol=symbols, start=start, end=end)
    if df.empty:
        return df
    df = df.dropna(subset=["eps_actual", "eps_estimate"]).copy()
    df = df[df["eps_estimate"] != 0].copy()
    df["surprise_pct"] = (
        (df["eps_actual"] - df["eps_estimate"]) / df["eps_estimate"].abs() * 100
    ).round(2)
    df["date"] = pd.to_datetime(df["date"])
    df["surprise_dir"] = df["surprise_pct"].apply(
        lambda x: "beat" if x > 0 else ("miss" if x < 0 else "inline")
    )
    return df[["symbol", "date", "eps_estimate", "eps_actual", "surprise_pct", "surprise_dir"]]


def load_earnings_sentiment(symbols=None, start=None, end=None,
                            window_days=DEFAULT_SENTIMENT_WINDOW):
    df = q.load("news_sentiment", symbol=symbols, start=start, end=end)
    if df.empty:
        return pd.DataFrame(columns=["symbol", "date", "sent_score",
                                     "sent_n_articles", "sent_confidence"])
    df = df.dropna(subset=["symbol", "date", "score"]).copy()
    df["date"] = pd.to_datetime(df["date"])

    if "key_topics" in df.columns:
        earnings_mask = df["key_topics"].str.contains("earnings", case=False, na=False)
        df = df[earnings_mask].copy()
        if df.empty:
            return pd.DataFrame(columns=["symbol", "date", "sent_score",
                                         "sent_n_articles", "sent_confidence"])
    else:
        return pd.DataFrame(columns=["symbol", "date", "sent_score",
                                     "sent_n_articles", "sent_confidence"])

    w = df["confidence"].clip(lower=0.05) if "confidence" in df.columns else pd.Series(1.0, index=df.index)
    df["_w"] = w
    df["_ws"] = df["score"] * df["_w"]

    daily = (df.groupby(["symbol", "date"])
               .agg(_ws=("_ws", "sum"), _w=("_w", "sum"),
                    n_articles=("score", "size"),
                    mean_confidence=("confidence", "mean"))
               .reset_index())
    daily["sent_score"] = daily["_ws"] / daily["_w"]
    daily["sent_confidence"] = daily["mean_confidence"].round(3)
    daily = daily.rename(columns={"n_articles": "sent_n_articles"})

    return daily[["symbol", "date", "sent_score", "sent_n_articles", "sent_confidence"]]


def _snap_to_trading_day(date, trading_days):
    idx = trading_days.searchsorted(date, side="left")
    if idx >= len(trading_days):
        return None
    if trading_days[idx] == date:
        return trading_days[idx]
    if idx > 0 and (trading_days[idx] - date).days <= (date - trading_days[idx - 1]).days:
        return trading_days[idx]
    if idx > 0:
        return trading_days[idx - 1]
    return trading_days[idx]


def build_panel(symbols=None, start=None, end=None,
                sentiment_window=DEFAULT_SENTIMENT_WINDOW,
                car_window=5):
    earnings = load_earnings_events(symbols=symbols, start=start, end=end)
    if earnings.empty:
        print("No earnings events found.")
        return pd.DataFrame()

    print(f"  Earnings events: {len(earnings):,} rows, "
          f"{earnings['symbol'].nunique()} symbols, "
          f"{earnings['date'].min().date()} -> {earnings['date'].max().date()}")

    sent = load_earnings_sentiment(symbols=symbols, start=start, end=end,
                                   window_days=sentiment_window)
    print(f"  Earnings sentiment signals: {len(sent):,} rows, "
          f"{sent['symbol'].nunique()} symbols")

    all_syms = list(set(earnings["symbol"].unique()) | set(sent["symbol"].unique()))
    print(f"  Loading prices for {len(all_syms)} symbols...")
    closes = load_close_matrix(all_syms)
    bench_close = load_close(BENCHMARK) if BENCHMARK else None

    # Check if tiingo data covers the earnings date range; if not, fetch via yfinance
    if not closes.empty and not earnings.empty:
        max_price_date = closes.index.max()
        max_earn_date = earnings["date"].max()
        if max_earn_date > max_price_date:
            print(f"  Price data ends {max_price_date.date()}, "
                  f"earnings go to {max_earn_date.date()} -- fetching recent via yfinance...")
            try:
                import yfinance as yf
                missing_syms = [s for s in all_syms if s not in closes.columns]
                stale_syms = [s for s in closes.columns
                              if closes[s].dropna().index.max() < max_earn_date]
                fetch_syms = list(set(missing_syms + stale_syms))
                if fetch_syms:
                    fetch_start = (max_price_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                    fetch_end = (max_earn_date + pd.Timedelta(days=15)).strftime("%Y-%m-%d")
                    print(f"  Fetching {len(fetch_syms)} symbols: {fetch_start} -> {fetch_end}")
                    fresh = yf.download(
                        fetch_syms, start=fetch_start, end=fetch_end,
                        progress=False, auto_adjust=True,
                    )
                    if not fresh.empty:
                        if isinstance(fresh.columns, pd.MultiIndex):
                            fresh_close = fresh["Close"]
                        else:
                            fresh_close = fresh[["Close"]].rename(columns={"Close": fetch_syms[0]})
                        fresh_close.index = pd.to_datetime(fresh_close.index).tz_localize(None)
                        if fresh_close.index.name == "Date":
                            fresh_close.index.name = None
                        closes = pd.concat([closes, fresh_close], axis=1)
                        closes = closes.loc[:, ~closes.columns.duplicated(keep="last")]
                        closes = closes[~closes.index.duplicated(keep="last")]
                        closes = closes.sort_index()
                        print(f"  Added {len(fresh_close)} fresh price rows for "
                              f"{len(fresh_close.columns)} symbols")
                    if BENCHMARK and bench_close is not None:
                        bench_max = bench_close.index.max()
                        if bench_max < max_earn_date:
                            bench_fresh = yf.download(
                                BENCHMARK, start=fetch_start, end=fetch_end,
                                progress=False, auto_adjust=True,
                            )
                            if not bench_fresh.empty:
                                if isinstance(bench_fresh.columns, pd.MultiIndex):
                                    bc = bench_fresh["Close"]
                                else:
                                    bc = bench_fresh["Close"]
                                bc.index = pd.to_datetime(bc.index).tz_localize(None)
                                bench_close = pd.concat([bench_close, bc]).sort_index()
                                bench_close = bench_close[~bench_close.index.duplicated(keep="last")]
            except ImportError:
                print("  yfinance not installed -- skipping price supplement")
            except Exception as e:
                print(f"  yfinance fetch failed: {e}")

    sent_by_sym = {}
    for _, row in sent.iterrows():
        key = (row["symbol"], row["date"])
        sent_by_sym[key] = row

    results = []
    for _, earn_row in earnings.iterrows():
        sym = earn_row["symbol"]
        earn_date = earn_row["date"]

        if sym not in closes.columns:
            continue

        sym_close = closes[sym]
        if isinstance(sym_close, pd.DataFrame):
            sym_close = sym_close.iloc[:, 0]
        sym_close = sym_close.dropna()
        if sym_close.empty:
            continue

        best_sent = None
        best_delta = None
        for h in range(1, sentiment_window + 1):
            lookback = earn_date - pd.Timedelta(days=h)
            key = (sym, lookback)
            if key in sent_by_sym:
                if best_delta is None or h < best_delta:
                    best_sent = sent_by_sym[key]
                    best_delta = h
        key_exact = (sym, earn_date)
        if key_exact in sent_by_sym:
            best_sent = sent_by_sym[key_exact]
            best_delta = 0

        t_idx = _snap_to_trading_day(earn_date, sym_close.index)
        if t_idx is None:
            continue
        pos = sym_close.index.get_loc(t_idx)
        if isinstance(pos, slice):
            pos = pos.start

        fwd_row = {}
        entry_pos = pos + 1
        if entry_pos < len(sym_close):
            base_entry = sym_close.iloc[entry_pos]
            for h in FWD_HORIZONS:
                if entry_pos + h < len(sym_close) and base_entry > 0:
                    fwd_row[f"fwd_{h}d"] = round(float(
                        (sym_close.iloc[entry_pos + h] / base_entry - 1.0) * 100), 3)
                else:
                    fwd_row[f"fwd_{h}d"] = np.nan
        else:
            for h in FWD_HORIZONS:
                fwd_row[f"fwd_{h}d"] = np.nan

        car_row = {}
        start_pos = pos - car_window
        end_pos = pos + car_window
        if start_pos >= 0 and end_pos < len(sym_close):
            base_price = sym_close.iloc[start_pos]
            if base_price > 0 and not np.isnan(base_price):
                stock_path = sym_close.iloc[start_pos:end_pos + 1].values / base_price - 1.0
                if bench_close is not None and not bench_close.empty:
                    bc = bench_close.iloc[:, 0] if isinstance(bench_close, pd.DataFrame) else bench_close
                    b_start = bc.index.searchsorted(sym_close.index[start_pos], side="left")
                    b_end = bc.index.searchsorted(sym_close.index[end_pos], side="right")
                    if b_start < len(bc) and b_end <= len(bc):
                        bench_slice = bc.iloc[b_start:b_end]
                        if len(bench_slice) > 1:
                            b0 = float(bench_slice.iloc[0])
                            if b0 > 0 and not np.isnan(b0):
                                bench_path = bench_slice.values.astype(float) / b0 - 1.0
                                min_len = min(len(stock_path), len(bench_path))
                                abnormal = stock_path[:min_len] - bench_path[:min_len]
                            else:
                                abnormal = stock_path
                        else:
                            abnormal = stock_path
                    else:
                        abnormal = stock_path
                else:
                    abnormal = stock_path

                center = car_window
                for h in CAR_HORIZONS:
                    if center + h < len(abnormal):
                        car_row[f"car_{h}d"] = round(float(abnormal[center + h] * 100), 3)
                    else:
                        car_row[f"car_{h}d"] = np.nan

        rec = {
            "symbol": sym,
            "date": earn_date,
            "eps_estimate": earn_row["eps_estimate"],
            "eps_actual": earn_row["eps_actual"],
            "surprise_pct": earn_row["surprise_pct"],
            "surprise_dir": earn_row["surprise_dir"],
        }

        if best_sent is not None:
            rec["sent_score"] = best_sent["sent_score"]
            rec["sent_n_articles"] = best_sent["sent_n_articles"]
            rec["sent_confidence"] = best_sent["sent_confidence"]
        else:
            rec["sent_score"] = np.nan
            rec["sent_n_articles"] = 0
            rec["sent_confidence"] = np.nan

        rec.update(car_row)
        rec.update(fwd_row)
        results.append(rec)

    panel = pd.DataFrame(results)
    if panel.empty:
        print("  No events matched to price data.")
        return panel

    for col in ["sent_score", "sent_n_articles", "sent_confidence"]:
        if col in panel.columns:
            non_null = panel[col].notna().sum()
            print(f"  {col}: {non_null:,} / {len(panel):,} non-null")

    return panel


def print_stats(panel):
    if panel.empty:
        return

    print(f"\n{'='*70}")
    print(f"EARNINGS SENTIMENT EVALUATION STATS")
    print(f"{'='*70}")
    print(f"Total events: {len(panel):,}")
    print(f"Unique symbols: {panel['symbol'].nunique()}")
    print(f"Date range: {panel['date'].min().date()} -> {panel['date'].max().date()}")
    print(f"With sentiment: {panel['sent_score'].notna().sum():,} "
          f"({100*panel['sent_score'].notna().mean():.1f}%)")

    sent_valid = panel.dropna(subset=["sent_score"])
    if len(sent_valid) > 10:
        from scipy import stats as sp_stats
        print(f"\n--- Sentiment vs Surprise Correlation ---")
        rho, p = sp_stats.spearmanr(sent_valid["sent_score"], sent_valid["surprise_pct"])
        print(f"  Spearman rho (sentiment vs surprise%): {rho:.4f} (p={p:.4f})")

        for h in CAR_HORIZONS:
            col = f"car_{h}d"
            if col in panel.columns:
                sub = panel.dropna(subset=[col, "sent_score"])
                if len(sub) > 10:
                    rho, p = sp_stats.spearmanr(sub["sent_score"], sub[col])
                    print(f"  Spearman rho (sentiment vs CAR_{h}d): {rho:.4f} (p={p:.4f})")

    print(f"\n--- Surprise Direction Distribution ---")
    if "surprise_dir" in panel.columns:
        dist = panel["surprise_dir"].value_counts()
        for d, n in dist.items():
            print(f"  {d}: {n:,}")

    print(f"\n--- Mean CAR by Surprise Direction ---")
    for direction in ["beat", "miss"]:
        sub = panel[panel["surprise_dir"] == direction]
        if sub.empty:
            continue
        print(f"  {direction.upper()} (n={len(sub):,}):")
        for h in CAR_HORIZONS:
            col = f"car_{h}d"
            if col in sub.columns:
                mean = sub[col].mean()
                if not np.isnan(mean):
                    print(f"    CAR_{h}d: {mean:+.2f}%")

    sent_bull = panel[panel["sent_score"] >= BULLISH_THRESHOLD]
    sent_bear = panel[panel["sent_score"] <= BEARISH_THRESHOLD]
    if len(sent_bull) > 3 and len(sent_bear) > 3:
        print(f"\n--- Mean CAR by Sentiment Bucket ---")
        print(f"  BULLISH (n={len(sent_bull):,}, sent>={BULLISH_THRESHOLD}):")
        for h in CAR_HORIZONS:
            col = f"car_{h}d"
            if col in sent_bull.columns:
                mean = sent_bull[col].mean()
                if not np.isnan(mean):
                    print(f"    CAR_{h}d: {mean:+.2f}%")
        print(f"  BEARISH (n={len(sent_bear):,}, sent<={BEARISH_THRESHOLD}):")
        for h in CAR_HORIZONS:
            col = f"car_{h}d"
            if col in sent_bear.columns:
                mean = sent_bear[col].mean()
                if not np.isnan(mean):
                    print(f"    CAR_{h}d: {mean:+.2f}%")


def main():
    parser = argparse.ArgumentParser(description="Earnings Sentiment Evaluation")
    parser.add_argument("--sentiment-window", type=int, default=DEFAULT_SENTIMENT_WINDOW,
                        help="Trading days to look back for sentiment before earnings")
    parser.add_argument("--car-window", type=int, default=5,
                        help="Symmetric CAR window in trading days")
    parser.add_argument("--symbols", nargs="*", default=None,
                        help="Filter to specific symbols")
    parser.add_argument("--start", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--export", default=None, help="Export panel to CSV")
    args = parser.parse_args()

    print("[earnings_sentiment_eval] building panel...")
    panel = build_panel(
        symbols=args.symbols, start=args.start, end=args.end,
        sentiment_window=args.sentiment_window, car_window=args.car_window,
    )

    if panel.empty:
        print("No data. Run the pipelines first.")
        return

    print_stats(panel)

    if args.export:
        panel.to_csv(args.export, index=False)
        print(f"\nExported to {args.export}")

    return panel


if __name__ == "__main__":
    main()
