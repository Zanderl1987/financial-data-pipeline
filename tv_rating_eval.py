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


def run_transition_study(symbols, start: str = "1990-01-01",
                         end: "str | None" = None,
                         benchmark: "str | None" = BENCHMARK,
                         window: "tuple[int, int]" = (0, 21),
                         entry_lag: int = 1, min_events: int = 5,
                         price_table: str = PRICE_TABLE):
    """
    For every distinct (from_label, to_label) rating transition seen across
    `symbols`' full history, compute the average cumulative-return path via
    event_backtest.event_study(). Transition types with fewer than
    `min_events` occurrences are skipped (too little data to trust a mean).

    `start` defaults to "1990-01-01" (NOT None) deliberately:
    event_backtest.rating_changes() only scans full history when at least
    one of date/start/end is given: passing all-None triggers its
    "latest transition only" mode, which would silently return one row per
    symbol instead of the full transition history this study needs.

    Returns (paths, summary):
      paths   -- tidy DataFrame: from_label, to_label, rel_day, mean_car_pct, n
      summary -- {"from_label->to_label": {horizon_str: {...event_study
                 horizons row...}}}
    """
    changes = eb.rating_changes(symbols, start=start, end=end, price_table=price_table)
    path_rows = []
    summary = {}
    if changes.empty:
        return pd.DataFrame(columns=["from_label", "to_label", "rel_day",
                                     "mean_car_pct", "n"]), summary

    for (frm, to), grp in changes.groupby(["from_label", "to_label"]):
        if len(grp) < min_events:
            continue
        res = eb.event_study(grp[["symbol", "date"]], window=window,
                             benchmark=benchmark, entry_lag=entry_lag,
                             price_table=price_table)
        key = f"{frm}->{to}"
        for rel_day, val in res.mean_car.items():
            path_rows.append({"from_label": frm, "to_label": to,
                              "rel_day": int(rel_day),
                              "mean_car_pct": round(100 * float(val), 3),
                              "n": res.n_events})
        summary[key] = {str(h): row.to_dict() for h, row in res.horizons.iterrows()}

    paths = pd.DataFrame(path_rows, columns=["from_label", "to_label", "rel_day",
                                             "mean_car_pct", "n"])
    return paths, summary


def _crossed_up(s: pd.Series, level: float) -> pd.Series:
    return (s >= level) & (s.shift(1) < level)


def _crossed_down(s: pd.Series, level: float) -> pd.Series:
    return (s <= level) & (s.shift(1) > level)


_TRADE_COLS = ["symbol", "side", "entry_signal_date", "entry_date", "entry_price",
              "exit_signal_date", "exit_date", "exit_price", "days_held",
              "pnl_dollars", "pnl_pct"]


def simulate_trades(cache: "dict[str, pd.DataFrame]",
                    notional: float = NOTIONAL) -> pd.DataFrame:
    """
    Rule-based long/short simulation on rating_all, one position per symbol
    at a time (no pyramiding; an entry signal while already in a position
    is ignored -- a position only closes via its own exit condition, and a
    new entry cannot start before the day after the previous trade's exit
    execution):

      long entry:  rating_all crosses UP through BULL_MIN (+0.5)
      long exit:   first later day rating_all < EXIT_LONG_MAX (+0.1)
      short entry: rating_all crosses DOWN through BEAR_MAX (-0.5)
      short exit:  first later day rating_all > EXIT_SHORT_MIN (-0.1)

    Both entry and exit execute at the NEXT trading day's close after the
    signal is observed (no same-day action). A position with no qualifying
    exit before the data ends is dropped (still open, not a realized P&L) --
    and blocks any further entries for that symbol, since it's still
    (unrealizedly) open.

    Returns one row per realized trade -- see _TRADE_COLS.
    """
    rows = []
    for sym, d in cache.items():
        rating = d["rating_all"]
        close = d["close"]
        n = len(d)

        long_entries = _crossed_up(rating, BULL_MIN).to_numpy()
        short_entries = _crossed_down(rating, BEAR_MAX).to_numpy()
        exit_long_cond = (rating < EXIT_LONG_MAX).to_numpy()
        exit_short_cond = (rating > EXIT_SHORT_MIN).to_numpy()

        entry_positions = sorted(
            [(i, "long") for i in np.flatnonzero(long_entries)] +
            [(i, "short") for i in np.flatnonzero(short_entries)]
        )

        next_free = 0
        for sig_i, side in entry_positions:
            if sig_i < next_free:
                continue                       # already in a position
            entry_i = sig_i + 1
            if entry_i >= n:
                continue                       # no next close to enter at
            entry_price = close.iloc[entry_i]
            if not np.isfinite(entry_price) or entry_price <= 0:
                continue

            exit_cond = exit_long_cond if side == "long" else exit_short_cond
            exit_sig_i = None
            for j in range(entry_i + 1, n):
                if exit_cond[j]:
                    exit_sig_i = j
                    break
            if exit_sig_i is None:
                next_free = n                  # rest of history: still open
                continue
            exit_i = exit_sig_i + 1
            if exit_i >= n:
                next_free = n
                continue
            exit_price = close.iloc[exit_i]
            if not np.isfinite(exit_price) or exit_price <= 0:
                next_free = exit_i + 1
                continue

            pct = (exit_price / entry_price - 1.0) if side == "long" else \
                  (1.0 - exit_price / entry_price)
            rows.append({
                "symbol": sym, "side": side,
                "entry_signal_date": d.index[sig_i], "entry_date": d.index[entry_i],
                "entry_price": float(entry_price),
                "exit_signal_date": d.index[exit_sig_i], "exit_date": d.index[exit_i],
                "exit_price": float(exit_price), "days_held": exit_i - entry_i,
                "pnl_dollars": round(notional * pct, 2), "pnl_pct": round(100 * pct, 3),
            })
            next_free = exit_i + 1

    return pd.DataFrame(rows, columns=_TRADE_COLS)


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate the TV rating replica vs forward returns")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated symbol subset (default: full "
                             "tiingo_prices universe)")
    parser.add_argument("--start", default="1990-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--benchmark", default=BENCHMARK)
    parser.add_argument("--min-names", type=int, default=5)
    args = parser.parse_args()

    syms = args.symbols.split(",") if args.symbols else universe()
    print(f"[tv_rating_eval] building signal cache for {len(syms)} symbols...")
    cache = build_signal_cache(syms, start=args.start, end=args.end)
    print(f"  {len(cache)} symbols had usable price history")
    if not cache:
        print("No usable symbols. Check price_table / date range.")
        return

    print("[tv_rating_eval] building return panel...")
    panel = build_return_panel(cache, benchmark=args.benchmark)
    print(f"  {len(panel):,} symbol-day rows")

    print("[tv_rating_eval] level-IC evaluation...")
    level_ic = {sig: evaluate_signal(panel, sig, min_names=args.min_names)
               for sig in SIGNALS}
    for sig, results in level_ic.items():
        print(f"\n=== {sig} ===")
        hdr = (f"{'h':>3} {'n':>6} {'pooledIC':>9} {'p':>7} {'dailyIC':>8} "
              f"{'t':>6} {'days':>5} {'bull%':>7} {'bear%':>7} {'spread%':>8} {'t':>6}")
        print(hdr)
        for h, r in results.items():
            print(f"{h:>3} {r['n']:>6} {r.get('pooled_ic', float('nan')):>9} "
                  f"{r.get('pooled_p', float('nan')):>7} "
                  f"{str(r.get('mean_daily_ic', '-')):>8} {str(r.get('ic_t_stat', '-')):>6} "
                  f"{str(r.get('ic_days', '-')):>5} "
                  f"{str(r.get('bull_mean_pct', '-')):>7} {str(r.get('bear_mean_pct', '-')):>7} "
                  f"{str(r.get('spread_pct', '-')):>8} {str(r.get('spread_t', '-')):>6}")

    print("\n[tv_rating_eval] transition study...")
    paths, transition_summary = run_transition_study(
        syms, start=args.start, end=args.end, benchmark=args.benchmark)
    print(f"  {len(transition_summary)} transition types qualified")

    print("[tv_rating_eval] trade simulation...")
    trades = simulate_trades(cache)
    if len(trades):
        win_rate = 100 * (trades["pnl_dollars"] > 0).mean()
        print(f"  {len(trades)} realized trades | win rate {win_rate:.1f}%")
    else:
        print("  0 realized trades")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "ic_stats.json"), "w") as f:
        json.dump({"level_ic": level_ic, "transition_stats": transition_summary}, f,
                  indent=2, default=str)
    panel.to_parquet(os.path.join(OUT_DIR, "panel.parquet"), index=False)
    paths.to_parquet(os.path.join(OUT_DIR, "transitions.parquet"), index=False)
    trades.to_parquet(os.path.join(OUT_DIR, "trades.parquet"), index=False)
    print(f"\n-> wrote artifacts to {OUT_DIR}/")

    print("\nGuide: |IC| < 0.02 = noise; 0.02-0.05 weak-but-real if t>=2; "
         ">0.05 on daily data is suspicious (hunt for a leak). Need t-stat >= 2 "
         "across >= ~250 days to call anything significant. Sign flips across "
         "horizons = noise, not momentum-then-reversal.")


if __name__ == "__main__":
    main()
