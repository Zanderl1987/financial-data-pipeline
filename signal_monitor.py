"""
Signal Health Monitor:
  A maintained backtest on configured TA signals. Each run re-scores every
  configured (signal, symbols) pair over several trailing windows using
  event_backtest.technical_events() + scenario() (reused as-is — no new
  backtest math here) and appends one row per (signal, window) to a parquet
  history so accuracy drift is visible run-over-run.

  Trailing windows restrict the *event dates* used, not the price history
  loaded — so 200-day SMA warm-up is never truncated by a short window.

CLI:
  python signal_monitor.py                 # run + append today's scores
  python signal_monitor.py --config path.json
  python signal_monitor.py --history 10    # print last 10 stored runs

Output:
  storage/raw/signal_monitor/year=YYYY/month=MM/signal_health_{YYYYMMDD}.parquet

Schema:
  run_date | signal | window | symbols_key | n_trades | win_rate_pct |
  avg_return_pct | profit_factor | car21_mean_pct | car21_tstat |
  holding_days | fetched_at
"""

import os
import json
import argparse
import datetime

import pandas as pd

import query as q
import event_backtest as eb
from storage_utils import write_partitioned

OUTPUT_DIR = os.path.join("storage", "raw", "signal_monitor")
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "signal_monitor_config.json")

_WINDOW_DAYS = {"3y": 365 * 3, "1y": 365, "180d": 180}


def _load_config(path):
    with open(path, "r") as f:
        return json.load(f)


def _expand_symbols(spec):
    if spec == "watchlist":
        from tiingo_pipeline import DEFAULT_SYMBOLS
        return list(DEFAULT_SYMBOLS)
    return list(spec)


def _symbols_key(spec, symbols):
    if spec == "watchlist":
        return "watchlist"
    return ",".join(sorted(symbols))


def _score_window(events, holding_days, side, run_date_ts, window_name):
    """Run scenario() + event_study() over `events` (already date-filtered
    for this window) and return one metrics row, or None if too thin to score."""
    if events.empty:
        return None
    try:
        sc = eb.scenario(events, holding_days=holding_days, side=side)
    except Exception:
        return None
    if sc.metrics.get("n_trades", 0) == 0:
        return None
    car21_mean, car21_tstat = None, None
    try:
        es = eb.event_study(events, window=(0, 21))
        if 21 in es.horizons.index:
            row = es.horizons.loc[21]
            sign = -1.0 if side == "short" else 1.0
            car21_mean = round(sign * row["mean_pct"], 2)
            car21_tstat = round(sign * row["t_stat"], 2)
    except Exception:
        pass
    return {
        "window": window_name,
        "n_trades": sc.metrics["n_trades"],
        "win_rate_pct": sc.metrics["win_rate_pct"],
        "avg_return_pct": sc.metrics["avg_return_pct"],
        "profit_factor": sc.metrics["profit_factor"],
        "car21_mean_pct": car21_mean,
        "car21_tstat": car21_tstat,
        "holding_days": holding_days,
    }


def run(config, run_date_ts):
    windows = config.get("windows", ["full"])
    thresholds = config.get("decline_thresholds", {})
    drop_pts = thresholds.get("win_rate_drop_pts", 10)
    pf_floor = thresholds.get("profit_factor_floor", 1.0)
    min_trades = thresholds.get("min_trades_for_flag", 10)

    rows = []
    reports = []
    for sig in config["signals"]:
        name = sig["name"]
        symbols = _expand_symbols(sig["symbols"])
        key = _symbols_key(sig["symbols"], symbols)
        holding_days = sig.get("holding_days", 21)
        side = sig.get("side", "long")

        events = eb.technical_events(symbols, signal=name)
        if events.empty:
            print(f"  {name}: no events found for {len(symbols)} symbol(s), skipping.")
            continue

        by_window = {}
        for w in windows:
            if w == "full":
                sub = events
            else:
                days = _WINDOW_DAYS.get(w)
                if days is None:
                    continue
                cutoff = run_date_ts - pd.Timedelta(days=days)
                sub = events[pd.to_datetime(events["date"]) >= cutoff]
            metrics = _score_window(sub, holding_days, side, run_date_ts, w)
            if metrics is None:
                continue
            by_window[w] = metrics
            rows.append({
                "run_date": run_date_ts.strftime("%Y-%m-%d"),
                "signal": name,
                "symbols_key": key,
                "fetched_at": datetime.datetime.utcnow().isoformat(),
                **metrics,
            })

        flag = ""
        full_m, y1_m = by_window.get("full"), by_window.get("1y")
        if full_m and y1_m and y1_m["n_trades"] >= min_trades:
            win_drop = full_m["win_rate_pct"] - y1_m["win_rate_pct"]
            if win_drop > drop_pts or y1_m["profit_factor"] < pf_floor:
                flag = "  ** DEGRADED **"
        reports.append((name, by_window, flag))

    return rows, reports


def _print_report(reports):
    print(f"\n{'SIGNAL':<18} {'WINDOW':<8} {'N':>5} {'WIN%':>7} {'AVG%':>7} {'PF':>6}")
    for name, by_window, flag in reports:
        for w, m in by_window.items():
            print(f"{name:<18} {w:<8} {m['n_trades']:>5} {m['win_rate_pct']:>7} "
                  f"{m['avg_return_pct']:>7} {m['profit_factor']:>6}")
        if flag:
            print(f"{name}{flag}")


def _print_history(n):
    df = q.load("signal_health")
    if df.empty:
        print("signal_health is empty — run signal_monitor.py first.")
        return
    df = df.sort_values("run_date")
    runs = sorted(df["run_date"].unique())[-n:]
    df = df[df["run_date"].isin(runs)]
    piv = df.pivot_table(index=["signal", "window"], columns="run_date",
                         values="win_rate_pct")
    print(f"\nWin-rate % by run (last {len(runs)} run(s)):")
    print(piv.to_string())


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--history", type=int,
                        help="print the last N stored runs instead of running a new one")
    args = parser.parse_args()

    if args.history:
        _print_history(args.history)
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    config = _load_config(args.config)
    run_date_ts = pd.Timestamp(datetime.date.today())

    dr = q.date_range("tiingo_prices")
    max_price_date = dr["max_date"].iloc[0] if not dr.empty else "unknown"
    print(f"Signal Health Monitor  run_date={run_date_ts.date()}  "
          f"tiingo_prices max_date={max_price_date}")

    rows, reports = run(config, run_date_ts)
    if not rows:
        print("No signals produced scoreable events. Nothing written.")
        return

    df = pd.DataFrame(rows)
    stamp = run_date_ts.strftime("%Y%m%d")
    filename = write_partitioned(df, OUTPUT_DIR, f"signal_health_{stamp}.parquet")

    _print_report(reports)
    print(f"\n--- SIGNAL MONITOR COMPLETE ---")
    print(f"Saved {len(df)} row(s) -> {filename}")


if __name__ == "__main__":
    main()
