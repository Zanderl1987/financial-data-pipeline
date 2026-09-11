#!/usr/bin/env python3
"""
Earnings surprise event study (Alpha Vantage earnings history).

Question: does the market react to earnings surprises (beat/miss) on the
report date? If so, surprise direction/magnitude is a signal for
`signal_panel()`.

Design notes:
  * Events keyed on `reportedDate` (the actual release date), NOT the
    fiscal period end. The fiscal end is known in advance; the release
    date is when the surprise becomes public.
  * `entry_lag` depends on `reportTime`:
      - "pre-market"  -> entry_lag=0 (release before open, tradeable same day)
      - "post-market" -> entry_lag=1 (release after close, next day open)
    This is the same honesty principle as congressional/CA studies.
  * Quarterly reports only (annuals are noisier, often simultaneous with Q4).
  * Split by surprise direction: beat (surprise > 0) vs miss (surprise < 0).
    Magnitude tiers: |surprise| > median, > 75th pct.
  * Significance: pooled + date-level BH (same-day releases cluster).
  * Benchmark: SPY (abnormal returns).
"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import query as q                                   # noqa: E402
import event_backtest as eb                         # noqa: E402
from analytics.event_impact import _date_level_stats  # noqa: E402


def build_events(min_surprise_pct=0):
    df = q.load("alpha_vantage_earnings")
    n_all = len(df)

    # Types
    df["reportedDate"] = pd.to_datetime(df["reportedDate"], errors="coerce")
    df["surprisePct"] = pd.to_numeric(df["surprisePercentage"], errors="coerce")
    df["reportedEPS"] = pd.to_numeric(df["reportedEPS"], errors="coerce")
    df["estimatedEPS"] = pd.to_numeric(df["estimatedEPS"], errors="coerce")

    # Quarterly only, has surprise, has date
    df = df[(df["report_type"] == "quarterly")
            & df["surprisePct"].notna()
            & df["reportedDate"].notna()
            & df["ticker"].notna()]
    if min_surprise_pct:
        df = df[df["surprisePct"].abs() >= min_surprise_pct]

    # Direction
    df["direction"] = df["surprisePct"].map(lambda x: "beat" if x > 0 else "miss")

    # entry_lag by reportTime: pre-market=0 (same day), post-market=1 (next day)
    df["entry_lag"] = df["reportTime"].map({"pre-market": 0, "post-market": 1})
    df["entry_lag"] = df["entry_lag"].fillna(1).astype(int)  # default to 1 if unknown

    # Dedup: same symbol same date same direction -> one event (keep largest |surprise|)
    df["abs_surprise"] = df["surprisePct"].abs()
    events = (df.sort_values("abs_surprise", ascending=False)
                .groupby(["ticker", "reportedDate", "direction"])
                .first()
                .reset_index()
                .rename(columns={"ticker": "symbol", "reportedDate": "date"}))

    print(f"alpha_vantage_earnings rows: {n_all:,}")
    print(f"  -> quarterly + surprise + date: {len(df):,} rows")
    print(f"  -> deduped (symbol, date, direction) events: {len(events):,}")
    print(f"     beat {(events['direction']=='beat').sum():,}  miss {(events['direction']=='miss').sum():,}")
    print(f"  -> entry_lag 0 (pre-market): {(events['entry_lag']==0).sum():,}")
    print(f"  -> entry_lag 1 (post-market): {(events['entry_lag']==1).sum():,}")

    return events[["symbol", "date", "direction", "surprisePct", "entry_lag"]]


def run_side(events, direction, benchmark="SPY", window=(-10, 63)):
    """Run event_study for one direction with per-event entry_lag."""
    sub = events[events["direction"] == direction].copy()
    if sub.empty:
        return None, None

    # event_study accepts per-row entry_lag via the events frame
    res = eb.event_study(
        sub[["symbol", "date", "direction", "surprisePct", "entry_lag"]],
        window=window,
        benchmark=benchmark,
        entry_lag=1,  # ignored when events has entry_lag column
        price_table="prices",
    )
    print(f"  aligned {res.n_events:,} {direction} events to price store")

    dl = _date_level_stats(res)
    return res, dl


def report(direction, res, dl):
    if res is None or res.n_events == 0:
        print(f"\n  {direction.upper()}: no events survived alignment")
        return

    print(f"\n{'=' * 78}")
    p = res.params
    print(f"{direction.upper()} EARNINGS SURPRISE   {p['n_events_used']}/{p['n_events_in']} events used | "
          f"window {p['window']} | benchmark {p['benchmark']} | mixed entry_lag")
    print(f"{'=' * 78}")
    print("  POOLED (same-day releases are not independent)")
    print("  Horizon     n   mean%  median%   hit%    t   baseline%   edge%")
    for h, r in res.horizons.iterrows():
        if h not in res.car.columns:
            continue
        col = res.car[h].dropna()
        if col.empty:
            continue
        print(f"  {h:>7}  {r['n']:>5}  {r['mean_pct']:>6}  {r['median_pct']:>7}  "
              f"{r['hit_rate_pct']:>5}  {r['t_stat']:>5}  {r['baseline_pct']:>9}  "
              f"{r['edge_pct']:>6}")

    print("\n  DATE-LEVEL (one mean CAR per report date, BH-adjusted)")
    print("  Horizon  n_dates   mean%      t    p_value    p_adj   significant")
    for h, r in dl.iterrows():
        sig = "YES" if r["p_adj"] < 0.05 else "no"
        if sig == "YES":
            # beat should be positive, miss should be negative
            wrong = (direction == "beat" and r["mean_pct"] < 0) or \
                    (direction == "miss" and r["mean_pct"] > 0)
            if wrong:
                sig = "YES(wrong sign)"
        print(f"  {h:>7}  {r['n_dates']:>7}  {r['mean_pct']:>6}  "
              f"{r['t_stat']:>6}  {r['p_value']:>8.4f}  {r['p_adj']:>7.4f}   {sig}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-surprise", type=float, default=0,
                    help="minimum |surprise%%| to include (filter tiny beats)")
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--start", default=None,
                    help="only use reports on/after this date")
    args = ap.parse_args()

    events = build_events(min_surprise_pct=args.min_surprise)
    if args.start:
        events = events[events["date"] >= args.start]
        print(f"  -> after --start {args.start}: {len(events):,} events")

    print(f"\nBuilding close matrix for "
          f"{events['symbol'].nunique():,} symbols from `prices` ...")

    results = {}
    for direction in ("beat", "miss"):
        res, dl = run_side(events, direction, args.benchmark)
        results[direction] = (res, dl)
        report(direction, res, dl)

    print(f"\n{'=' * 78}")
    print("VERDICT")
    print(f"{'=' * 78}")
    any_real = False
    for direction, (res, dl) in results.items():
        if dl is None or dl.empty:
            print(f"  {direction.upper()}: no aligned events")
            continue
        good = dl[(dl["p_adj"] < 0.05)]
        if direction == "beat":
            good = good[good["mean_pct"] > 0]
        else:
            good = good[good["mean_pct"] < 0]
        if not good.empty:
            any_real = True
            hs = ", ".join(f"h{h} ({r['mean_pct']}%, p_adj={r['p_adj']:.4f})"
                           for h, r in good.iterrows())
            print(f"  {direction.upper()}: correctly-signed and significant at {hs}")
        else:
            print(f"  {direction.upper()}: nothing survives the date-level test.")
    if not any_real:
        print("\n  NULL RESULT. Do not wire this into signal_panel().")
    else:
        print("\n  Something survived. Before wiring, re-check with "
              "min_surprise>0 and on a held-out period.")


if __name__ == "__main__":
    main()