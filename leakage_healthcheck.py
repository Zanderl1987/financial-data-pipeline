r"""
leakage_healthcheck.py -- standing entry-lag leakage health check across the
repo's event_backtest.scenario()-based studies.

evaluation/leakage_probe.py's entry_lag_leakage() is a one-switch ablation:
same-bar (entry_lag=0, leaky) vs next-bar (entry_lag=1, the documented safe
default) execution on event_backtest.scenario(). It was built and verified
ad hoc against one synthetic fixture and one real dataset (2026-09-02). This
script is the "run it across the roster, not just a one-off probe"
follow-up from work-notes/financial-data-pipeline/TASKS.md's Backtesting
Engine Improvements section: every registered event-driven study gets
checked, on demand or from a scheduled run, so a same-bar-execution
regression in any of them is caught rather than rediscovered later.

SCOPE, deliberately narrow -- only studies that go through
event_backtest.scenario() have an entry_lag switch to misconfigure at all:
  * TV catalog strategies (evaluation/trades.py's TradeRule engine) are OUT
    OF SCOPE. That engine hardcodes next-close execution (see trades.py's
    own module docstring: "Entry timing is never trusted to the rule") --
    there is no entry_lag parameter there to ablate. This is a stronger
    guarantee than a health check could give, not a gap in this script's
    coverage.
  * California Form 700 disclosures are OUT OF SCOPE for now -- the filings
    disclose entity NAMES, not tickers, and no name-to-ticker resolution
    exists yet in this repo. Add it here once one does.
  * Congressional trades (STOCK Act, real tickers, disclosure_date-keyed) is
    the one ready roster entry today. Add more studies to ROSTER as they are
    built -- each entry only needs a zero-arg callable returning a
    [symbol, date] DataFrame plus the scenario() kwargs that study uses.

Usage:
  C:\ProgramData\anaconda3\python.exe leakage_healthcheck.py
  C:\ProgramData\anaconda3\python.exe leakage_healthcheck.py --threshold 0.05
"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from evaluation import leakage_probe as lp                    # noqa: E402


def _congressional_events(side: str) -> pd.DataFrame:
    from experiments.congressional_disclosure_event_study import build_events

    events = build_events()
    events = events[events["side"] == side]
    return events[["symbol", "date"]].reset_index(drop=True)


# name -> (zero-arg events builder returning [symbol, date], scenario kwargs
# held fixed across the safe/leaky runs). Add a study here once it has a
# real [symbol, date] event table and goes through event_backtest.scenario()
# or event_study() with an entry_lag it actually relies on.
ROSTER = {
    "congressional_disclosures_buy": (
        lambda: _congressional_events("buy"),
        dict(holding_days=21, price_table="prices")),
    "congressional_disclosures_sell": (
        lambda: _congressional_events("sell"),
        dict(holding_days=21, price_table="prices")),
}


def run_one(name: str, events_builder, scenario_kwargs: dict) -> dict:
    events = events_builder()
    n_events = len(events)
    if n_events == 0:
        return {"name": name, "n_events": 0, "reason": "no events"}
    out = lp.entry_lag_leakage(events, **scenario_kwargs)
    out["name"] = name
    out["n_events"] = n_events
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threshold", type=float, default=0.10,
                    help="flag a study when the leaky-minus-safe per-trade "
                         "risk-adjusted return exceeds this (default 0.10, "
                         "same units as leakage_probe's per-trade metric -- "
                         "not annualized, see leakage_probe._scenario_sharpe)")
    args = ap.parse_args(argv)

    print("Entry-lag leakage health check (evaluation/leakage_probe.py)")
    print("=" * 78)
    any_flagged = False
    for name, (builder, kwargs) in ROSTER.items():
        try:
            out = run_one(name, builder, kwargs)
        except Exception as exc:   # a probe that cannot even run is itself a finding
            print(f"{name}: ERROR {type(exc).__name__}: {exc}")
            any_flagged = True
            continue
        if out.get("reason"):
            print(f"{name}: SKIP ({out['reason']})")
            continue
        infl = out["inflation"]
        flag = infl is not None and infl > args.threshold
        any_flagged = any_flagged or flag
        mark = "FLAG" if flag else "ok"
        print(f"{name}: n_events={out['n_events']} safe={out['safe_metric']} "
              f"leaky={out['leaky_metric']} inflation={infl} [{mark}]")
    print("=" * 78)
    if any_flagged:
        print(f"X one or more studies show entry-lag leakage inflation above "
              f"the {args.threshold} threshold -- investigate before "
              f"trusting their reported edge.")
        return 1
    print("+ no entry-lag leakage flagged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
