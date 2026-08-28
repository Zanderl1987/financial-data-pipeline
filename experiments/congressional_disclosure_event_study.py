#!/usr/bin/env python3
"""
Disclosure-date event study for congressional_trades.

Question: does the market move after a member of Congress DISCLOSES a trade?

Design notes that matter:

  * Events are keyed on `disclosure_date`, NEVER `transaction_date`. The two
    differ by up to 45 days by statute (median 17 days in this data), so the
    transaction date is not public information when it happens -- keying off it
    is textbook look-ahead. This is the same error that reversed `oil_shock`
    to null on 2026-07-07.

  * `entry_lag=1`. A filing is only known to have landed "sometime that day",
    so day 0 is not tradeable. Entering at the next close is the earliest
    honest fill.

  * Buys and sells are tested separately and in opposite directions. Pooling
    them would cancel any real effect.

  * Significance is reported BOTH pooled (event_study's own t-stat) and
    date-level. Many members disclose on the same day, and same-day
    disclosures are not independent draws, so the pooled t-stat overstates
    significance. `analytics.event_impact._date_level_stats` re-aggregates to
    one mean CAR per disclosure date and applies a Benjamini-Hochberg
    correction across horizons -- that is the number to believe.

  * Only common stock is used (House asset_type "ST", Senate "Stock").
    Options, bonds and municipal securities carry a ticker but their price
    reaction is not the equity's.

Usage:
  python experiments/congressional_disclosure_event_study.py
  python experiments/congressional_disclosure_event_study.py --min-gap-days 5
"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import query as q                                   # noqa: E402
import event_backtest as eb                         # noqa: E402
from analytics.event_impact import _date_level_stats  # noqa: E402

EQUITY_ASSET_TYPES = {"ST", "Stock"}

BUY_TYPES = {"p", "purchase"}
SELL_TYPES = {"s", "s (partial)", "sale (full)", "sale (partial)"}


def classify(txn_type):
    """Map both chambers' transaction vocabularies onto buy/sell."""
    if not isinstance(txn_type, str):
        return None
    t = txn_type.strip().lower()
    if t in BUY_TYPES:
        return "buy"
    if t in SELL_TYPES:
        return "sell"
    return None          # exchanges and anything unrecognized


def build_events(min_gap_days=0):
    df = q.load("congressional_trades")
    n_all = len(df)

    df = df[df["asset_type"].isin(EQUITY_ASSET_TYPES)]
    df = df[df["ticker"].notna() & df["disclosure_date"].notna()]
    df["side"] = df["transaction_type"].map(classify)
    df = df[df["side"].notna()]

    # One member disclosing the same ticker twice in one filing is one event
    # for price purposes; so is two members disclosing it the same day. Keep a
    # count so we can see how concentrated each event is.
    events = (df.groupby(["ticker", "disclosure_date", "side"])
                .agg(n_filings=("doc_id", "nunique"),
                     n_members=("member_name", "nunique"))
                .reset_index()
                .rename(columns={"ticker": "symbol",
                                 "disclosure_date": "date"}))
    print(f"congressional_trades rows: {n_all:,}")
    print(f"  -> equity + ticker + classified side: {len(df):,} rows")
    print(f"  -> deduped (symbol, disclosure_date, side) events: {len(events):,}")
    print(f"     buys {int((events['side'] == 'buy').sum()):,}  "
          f"sells {int((events['side'] == 'sell').sum()):,}")
    return events


class _SideResult:
    """
    A per-side view of a combined EventStudyResult.

    Only what report() and _date_level_stats() actually touch: .events, .car,
    .horizons (index + pooled stats) and .params. Recomputing the pooled
    horizon stats here rather than re-running event_study() is what lets the
    expensive close-matrix build happen exactly once.
    """

    def __init__(self, res, mask, side):
        self.events = res.events[mask].reset_index(drop=True)
        self.car = res.car[mask].reset_index(drop=True)
        self.baseline = res.baseline
        self.params = dict(res.params)
        self.params["n_events_used"] = len(self.car)
        self.params["n_events_in"] = int(mask.sum())
        self.params["side"] = side
        self.horizons = self._horizons(res.horizons.index)

    @property
    def n_events(self):
        return len(self.car)

    def _horizons(self, index):
        rows = {}
        for h in index:
            if h not in self.car.columns:
                continue
            col = self.car[h].dropna()
            if col.empty:
                continue
            n = len(col)
            mean, sd = float(col.mean()), float(col.std(ddof=1))
            t = mean / (sd / (n ** 0.5)) if n > 1 and sd > 0 else float("nan")
            base = float(self.baseline.get(h, float("nan")))
            rows[h] = {
                "n": n,
                "mean_pct": round(100 * mean, 2),
                "median_pct": round(100 * float(col.median()), 2),
                "hit_rate_pct": round(100 * float((col > 0).mean()), 1),
                "t_stat": round(t, 2),
                "baseline_pct": round(100 * base, 2),
                "edge_pct": round(100 * (mean - base), 2),
            }
        return pd.DataFrame.from_dict(rows, orient="index")


def _slice_side(res, side):
    return _SideResult(res, (res.events["side"] == side).to_numpy(), side)


def report(label, res, direction):
    """Print the pooled horizons table plus the honest date-level stats."""
    p = res.params
    print(f"\n{'=' * 78}")
    print(f"{label}   {p['n_events_used']}/{p['n_events_in']} events used | "
          f"window {p['window']} | benchmark {p['benchmark']} | "
          f"entry_lag {p['entry_lag']}")
    print(f"{'=' * 78}")
    if res.n_events == 0:
        print("  no events survived alignment to the price store")
        return None

    print("  POOLED (optimistic -- same-day disclosures are not independent)")
    print("  Horizon     n   mean%  median%   hit%    t   baseline%   edge%")
    for h, r in res.horizons.iterrows():
        print(f"  {h:>7}  {r['n']:>5}  {r['mean_pct']:>6}  {r['median_pct']:>7}  "
              f"{r['hit_rate_pct']:>5}  {r['t_stat']:>5}  {r['baseline_pct']:>9}  "
              f"{r['edge_pct']:>6}")

    dl = _date_level_stats(res)
    print("\n  DATE-LEVEL (honest -- one mean CAR per disclosure date, BH-adjusted)")
    print("  Horizon  n_dates   mean%      t    p_value    p_adj   significant")
    for h, r in dl.iterrows():
        sig = "YES" if r["p_adj"] < 0.05 else "no"
        # A significant result in the WRONG direction is not a finding.
        if sig == "YES":
            wrong = (direction == "buy" and r["mean_pct"] < 0) or \
                    (direction == "sell" and r["mean_pct"] > 0)
            if wrong:
                sig = "YES(wrong sign)"
        print(f"  {h:>7}  {r['n_dates']:>7}  {r['mean_pct']:>6}  "
              f"{r['t_stat']:>6}  {r['p_value']:>8.4f}  {r['p_adj']:>7.4f}   {sig}")
    return dl


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-gap-days", type=int, default=0,
                    help="drop events within N calendar days of the previous "
                         "event for the same symbol")
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--start", default=None,
                    help="only use disclosures on/after this date")
    args = ap.parse_args()

    events = build_events()
    if args.start:
        events = events[events["date"] >= args.start]
        print(f"  -> after --start {args.start}: {len(events):,} events")

    # One study over BOTH sides, split afterwards. event_study() carries extra
    # columns (here `side`) through to res.events, so nothing is lost -- and
    # calling it twice would rebuild the whole close matrix twice, which is by
    # far the dominant cost. Pinning price_table avoids load_close() probing
    # every price table per symbol (~5x fewer queries); `prices` alone already
    # covers 90% of these rows.
    print(f"\nBuilding close matrix for "
          f"{events['symbol'].nunique():,} symbols from `prices` "
          f"(this is the slow step)...")
    res_all = eb.event_study(
        events[["symbol", "date", "side", "n_filings", "n_members"]],
        window=(-10, 63),
        benchmark=args.benchmark,
        entry_lag=1,               # a filing is not tradeable on its own day
        price_table="prices",
        min_gap_days=args.min_gap_days,
    )
    print(f"  aligned {res_all.n_events:,} events to the price store")

    results = {}
    for side in ("buy", "sell"):
        sub = _slice_side(res_all, side)
        results[side] = (sub, report(f"DISCLOSED {side.upper()}", sub, side))

    print(f"\n{'=' * 78}")
    print("VERDICT")
    print(f"{'=' * 78}")
    any_real = False
    for side, (res, dl) in results.items():
        if dl is None or dl.empty:
            continue
        good = dl[(dl["p_adj"] < 0.05)]
        if side == "buy":
            good = good[good["mean_pct"] > 0]
        else:
            good = good[good["mean_pct"] < 0]
        if not good.empty:
            any_real = True
            hs = ", ".join(f"h{h} ({r['mean_pct']}%, p_adj={r['p_adj']:.4f})"
                           for h, r in good.iterrows())
            print(f"  {side.upper()}: correctly-signed and significant at {hs}")
        else:
            print(f"  {side.upper()}: nothing survives the date-level test.")
    if not any_real:
        print("\n  NULL RESULT. Do not wire this into signal_panel().")
    else:
        print("\n  Something survived. Before wiring anything into signal_panel(),"
              "\n  re-check with min_gap_days>0 and on a held-out period.")


if __name__ == "__main__":
    main()
