#!/usr/bin/env python3
"""
Disclosure-date event study for California Form 700 Schedule A-1 investments.

Question: does the market move when a CA state legislator/staff first
discloses a stock holding?

Design notes that matter (mirrors experiments/
congressional_disclosure_event_study.py, which is the sibling with the full
history):

  * Form 700 discloses entity NAMES, not tickers. The `name_to_ticker`
    resolver maps business_entity -> symbol against the `securities` reference
    table + a curated canonical table. Token-tier matches require the candidate
    to carry index-membership flags (require_listed=True) so junk legacy
    listings (e.g. "Duke Energy CORP" -> DUUKU) cannot collide with the real
    series.

  * Events are keyed on `filed_date` (the public disclosure date), NEVER on
    `acquired_date`. acquisition happened before filing by an unknown interval;
    keying off it is textbook look-ahead. `entry_lag=1` -- a filing is only
    known "sometime that day".

  * "First appearance" event: for each (filer, symbol) pair we keep only the
    EARLIEST filed_date on which that holding appears. Annual filings re-report
    the same holdings every year, so only a symbol's debut for that filer can
    plausibly be news. Rows where nature_of_investment is not "Stock" are out
    (ETFs, funds, crypto, gift-grabs of noise) -- the study covers common
    stocks only.

  * Significance is reported BOTH pooled (event_study's own t-stat) and
    date-level. Many filers file on the same day, and same-day disclosures are
    not independent draws. `analytics.event_impact._date_level_stats`
    re-aggregates to one mean CAR per disclosure date and applies a
    Benjamini-Hochberg correction across horizons -- that is the number to
    believe.

Usage:
  python experiments/california_disclosures_event_study.py
  python experiments/california_disclosures_event_study.py --min-gap-days 5
"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import query as q                                   # noqa: E402
import event_backtest as eb                         # noqa: E402
from name_to_ticker import NameResolver             # noqa: E402
from analytics.event_impact import _date_level_stats  # noqa: E402

FILER_KEY = ["filer_last_name", "filer_first_name", "agency", "position"]


def build_events():
    df = q.load("california_disclosures")
    n_all = len(df)

    # Common stock disclosures only; the filed_date is the public event date.
    df = df[df["nature_of_investment"].fillna("").astype(str)
              .str.contains("Stock", case=False, na=False)].copy()
    df["filed_date"] = pd.to_datetime(df["filed_date"], errors="coerce")
    df = df[df["filed_date"].notna() & df["business_entity"].notna()]
    df = df[df["business_entity"].astype(str).str.len() >= 4]
    n_stock = len(df)

    resolver = NameResolver().load()
    res = resolver.resolve_frame(df["business_entity"],
                                 require_listed=True)
    res.index = df.index
    n_resolved = int(res["symbol"].notna().sum())
    cov = resolver.coverage(df["business_entity"])
    df["symbol"] = res["symbol"]
    df["method"] = res["method"]
    df = df[df["symbol"].notna()].copy()
    n_resolved = len(df)

    # A filer's FIRST disclosure of a symbol is the news; annual refilings
    # re-report the same holding and are not new information.
    first = (df.groupby(FILER_KEY + ["symbol"])["filed_date"]
               .transform("min"))
    df["known_first"] = df["filed_date"] == first

    events = (df[df["known_first"]]
                .groupby(["symbol", "filed_date"])
                .agg(n_filings=("index_id", "nunique"),
                     n_filers=("filer_last_name",
                               lambda s: s.nunique()))
                .reset_index()
                .rename(columns={"filed_date": "date"}))
    events["date"] = events["date"].dt.strftime("%Y-%m-%d")

    print(f"california_disclosures rows: {n_all:,}")
    print(f"  -> nature_of_investment ~ Stock: {n_stock:,} rows")
    cov = resolver.coverage(df["business_entity"])
    print(f"     resolution coverage: {cov['pct_rows']}% of Stock rows | "
          f"{cov['resolved_distinct']}/{cov['distinct']} distinct entities "
          f"{cov['methods']}")
    print(f"  -> resolved to a US-listed ticker: {n_resolved:,} rows")
    print(f"  -> first-appearance rows: {int(df['known_first'].sum()):,}")
    print(f"  -> deduped (symbol, filed_date) events: {len(events):,} "
          f"across {events['symbol'].nunique():,} symbols")
    return events, df


def report(res):
    p = res.params
    print(f"\n{'=' * 78}")
    print(f"HOLDING DISCLOSED   {p['n_events_used']}/{p['n_events_in']} "
          f"events used | window {p['window']} | benchmark {p['benchmark']} | "
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
        if sig == "YES" and r["mean_pct"] < 0:
            sig = "YES(wrong sign)"
        print(f"  {h:>7}  {r['n_dates']:>7}  {r['mean_pct']:>6}  "
              f"{r['t_stat']:>6}  {r['p_value']:>8.4f}  {r['p_adj']:>7.4f}   {sig}")
    return dl


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-gap-days", type=int, default=0)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--start", default=None,
                    help="only use disclosures on/after this date")
    ap.add_argument("--dump-events", default=None,
                    help="CSV path to write the resolved first-appearance "
                         "event detail (symbol/date/filer/method)")
    args = ap.parse_args()

    events, detail = build_events()
    if args.dump_events:
        detail[FILER_KEY + ["index_id", "filed_date", "business_entity",
                            "symbol", "method", "known_first"]
               ].to_csv(args.dump_events, index=False)
        print(f"  -> wrote event detail to {args.dump_events}")
    if args.start:
        n0 = len(events)
        events = events[events["date"] >= args.start]
        print(f"  -> after --start {args.start}: {len(events):,} events "
              f"(was {n0:,})")

    print(f"\nBuilding close matrix for "
          f"{events['symbol'].nunique():,} symbols from `prices` "
          f"(this is the slow step)...")
    res_all = eb.event_study(
        events[["symbol", "date", "n_filings", "n_filers"]],
        window=(-10, 63),
        benchmark=args.benchmark,
        entry_lag=1,
        price_table="prices",
        min_gap_days=args.min_gap_days,
    )
    print(f"  aligned {res_all.n_events:,} events to the price store")

    dl = report(res_all)

    print(f"\n{'=' * 78}")
    print("VERDICT")
    print(f"{'=' * 78}")
    if dl is None or dl.empty:
        print("  no events survived alignment -- nothing to test.")
        return
    good = dl[(dl["p_adj"] < 0.05) & (dl["mean_pct"] > 0)]
    if not good.empty:
        hs = ", ".join(f"h{h} ({r['mean_pct']}%, p_adj={r['p_adj']:.4f})"
                       for h, r in good.iterrows())
        print(f"  holdings disclosure: correctly-signed and significant at {hs}")
    else:
        print("  holdings disclosure: nothing survives the date-level test.")
    print("\n  NULL PREDISPOSITION check: a significant negative reading "
          "would not be wired into anything.")


if __name__ == "__main__":
    main()