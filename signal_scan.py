"""
signal_scan.py — CLI: which symbols changed their TA rating bucket?

Thin wrapper around event_backtest.rating_changes() / tv_snapshot_changes()
so a daily scan doesn't require opening a Python shell. Writes nothing to
storage — this is an analysis tool, not a pipeline (not in run_all.py).

Usage
-----
    python signal_scan.py                          # latest changes, watchlist universe
    python signal_scan.py --date 2026-06-15        # specific day
    python signal_scan.py --symbols AAPL NVDA MSFT # explicit universe
    python signal_scan.py --upgrades               # upgrades only
    python signal_scan.py --downgrades              # downgrades only
    python signal_scan.py --min-step 2              # only big jumps (e.g. neutral -> strong_buy)
    python signal_scan.py --source tv               # diff stored tv_ratings snapshots instead
    python signal_scan.py --history 30              # all changes in the last N days
"""

import argparse

import event_backtest as eb


def _watchlist():
    from tiingo_pipeline import DEFAULT_SYMBOLS
    return DEFAULT_SYMBOLS


def _direction(args):
    if args.upgrades and args.downgrades:
        return None
    if args.upgrades:
        return "up"
    if args.downgrades:
        return "down"
    return None


def _print_table(df):
    if df.empty:
        print("No rating changes found.")
        return
    print(f"{'SYMBOL':<8} {'CHANGE':<24} {'SCORE':<22} {'DATE'}")
    for _, row in df.iterrows():
        change = f"{row['from_label']} -> {row['to_label']}"
        score = f"{row['from_score']:+.2f} -> {row['to_score']:+.2f}"
        date = str(row["date"].date()) if hasattr(row["date"], "date") else str(row["date"])
        print(f"{row['symbol']:<8} {change:<24} {score:<22} {date}")
    print(f"\n{len(df)} change(s).")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="scan this trading day only (YYYY-MM-DD)")
    parser.add_argument("--symbols", nargs="+", help="explicit symbol universe")
    parser.add_argument("--upgrades", action="store_true", help="only show upgrades")
    parser.add_argument("--downgrades", action="store_true", help="only show downgrades")
    parser.add_argument("--min-step", type=int, default=1,
                        help="minimum bucket jump (1=adjacent, 2=e.g. neutral->strong_buy)")
    parser.add_argument("--source", choices=["local", "tv"], default="local",
                        help="'local' = our replica over stored OHLCV (default); "
                             "'tv' = diff TradingView's own daily snapshots")
    parser.add_argument("--history", type=int,
                        help="show all changes in the last N days instead of a single day")
    args = parser.parse_args()

    direction = _direction(args)

    if args.source == "tv":
        try:
            df = eb.tv_snapshot_changes(date=args.date, direction=direction,
                                        min_step=args.min_step)
        except RuntimeError as e:
            print(str(e))
            return
        print(f"TradingView snapshot diff (source=tv_snapshot)")
        _print_table(df)
        return

    symbols = args.symbols or _watchlist()

    if args.history:
        import datetime
        import pandas as pd
        end_ts = pd.to_datetime(args.date) if args.date else pd.Timestamp(datetime.date.today())
        start_ts = end_ts - pd.Timedelta(days=args.history)
        df = eb.rating_changes(symbols, start=start_ts.strftime("%Y-%m-%d"),
                               end=end_ts.strftime("%Y-%m-%d"),
                               direction=direction, min_step=args.min_step)
        print(f"Scanning {len(symbols)} symbol(s), last {args.history} day(s):")
        _print_table(df)
        return

    df = eb.rating_changes(symbols, date=args.date, direction=direction,
                           min_step=args.min_step)
    label = args.date or "latest day"
    print(f"Scanning {len(symbols)} symbol(s), {label}:")
    _print_table(df)


if __name__ == "__main__":
    main()
