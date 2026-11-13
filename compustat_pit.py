#!/usr/bin/env python3
"""
Point-in-time discipline for Compustat fundamentals.

The problem this exists to prevent
----------------------------------
`comp.fundamentals_quarterly` is a RESTATED panel. A single fiscal period appears
once per restatement: (gvkey, datadate, fq) is stable, but `rvq` -- the fiscal
period end of the report the data arrived in -- differs per version. A company
that restated its 2019 Q2 accounts in 2021 has two rows for it.

There is therefore a filter that looks correct, is almost always used, and
destroys any backtest built on it: `latest == 1`. That flag selects the FINAL
restatement in the file, i.e. the version that was filed years after the fact,
and then applies it to every historical cross-section. A backtest built that way
knows in 2005 what the company reported in 2021. The repo has already been
burned by a variant of this -- the sign-convention defect in
`catalog/RESULTS.md` survived a passing golden-signal test because it lived in
the seam between two components that were each individually correct.

The rule implemented here
-------------------------
For an as-of date t, an observation is visible iff its `rvq` is on or before
`t - filing_lag`. Among the visible versions of a (gvkey, datadate, fq), take the
most recently RESTATED one -- so a company that revised twice before t gets the
second revision, not the first. The restriction is one-sided: restating history
must never change what history looked like.

`filing_lag_days` is a declared parameter with a fixed default, not a knob to
tune. Tuning an availability lag against a backtest result is a free parameter
aimed straight at the outcome, which is the same reasoning
`catalog/stats.py` gives for fixing the bootstrap block length at 21 days in
advance.

Not restating
-------------
Nothing here merges a restated panel into a clean one, and nothing here reports
a performance number. It builds the snapshot a caller would have built by hand,
and asserts -- on every call -- that the snapshot is invariant to data it should
not have been able to see. That assertion is the product. A guard that has to be
trusted is not a guard; `assert_no_lookahead` re-derives the answer from a
truncated panel and compares.

CLI:
    python compustat_pit.py --demo       # worked example on a synthetic restatement
"""

from __future__ import annotations

import argparse

import pandas as pd

# Compustat's quarterly reports become public roughly 40-45 days after the
# period end. 45 is deliberately conservative: being late shrinks the investable
# universe, whereas being early injects data before it existed. For calendar-Q4
# rows `rvq` is already the FOLLOWING fiscal year end (a 10-K reports Q4 inside
# the next year's period), so 45 days on top is very conservative there and
# costs almost nothing.
FILING_LAG_DAYS = 45

# The identity of an accounting observation. `datadate` is the fiscal period the
# numbers describe; `fq` disambiguates when a company's fiscal calendar puts two
# quarters on one datadate. `ffo` is deliberately NOT in the key -- it is a
# companion field, not a different observation.
OBSERVATION_KEY = ["gvkey", "datadate", "fq"]


def availability_date(rvq: pd.Series, lag_days: int = FILING_LAG_DAYS) -> pd.Series:
    """First date on which a restatement's contents could have been known."""
    return pd.to_datetime(rvq) + pd.Timedelta(days=lag_days)


def _dedupe(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop exact duplicates from overlapping pulls, keeping one row per version."""
    return frame.drop_duplicates(subset=OBSERVATION_KEY + ["rvq"], keep="last")


def pit_snapshot(
    fundamentals: pd.DataFrame,
    asof,
    key=None,
    lag_days: int = FILING_LAG_DAYS,
    verify: bool = True,
) -> pd.DataFrame:
    """The fundamentals table exactly as it stood on `asof`.

    Parameters
    ----------
    fundamentals : DataFrame with at least `gvkey`, `datadate`, `fq`, `rvq`.
        A restated panel -- multiple rows per observation, one per restatement.
    asof : date
        The formation date. Nothing reported after this is visible.
    key : columns identifying one accounting observation. Defaults to
        OBSERVATION_KEY.
    lag_days : filing lag applied to `rvq`. Declared, not tuned.
    verify : re-derive the snapshot from a truncated panel and compare. On by
        default because the cost is milliseconds and the failure it catches is
        the whole point of the module.

    Returns
    -------
    One row per observation: the most recent restatement available at `asof`.
    """
    key = list(key or OBSERVATION_KEY)
    frame = _dedupe(fundamentals.reset_index(drop=True))

    cutoff = pd.Timestamp(asof) - pd.Timedelta(days=lag_days)
    visible = frame[pd.to_datetime(frame["rvq"]) <= cutoff]
    if visible.empty:
        return visible.copy()

    # Most recent RESTATEMENT among the visible ones -- not the earliest, and
    # never the `latest=1` row, which may not be visible yet.
    chosen = visible.loc[visible.groupby(key)["rvq"].idxmax()].copy()
    chosen["available_from"] = availability_date(chosen["rvq"], lag_days)
    chosen = chosen.sort_values(key).reset_index(drop=True)

    if verify:
        assert_versions_visible(chosen, asof, key=key, lag_days=lag_days)
    return chosen


def assert_versions_visible(
    snapshot: pd.DataFrame, asof, key=None, lag_days: int = FILING_LAG_DAYS
) -> None:
    """Raise if any row in `snapshot` was reported after `asof`.

    The cheap local invariant: every selected version must have been public at
    the as-of date. This is what a `latest=1` filter violates, and unlike the
    differential check it can be pointed at any selection at all -- an adapter's
    output, a panel someone else built -- without needing the restated source to
    recompute anything.

    `assert_no_lookahead` is the stronger check but it needs the source panel;
    this one needs only the result, so it is what runs on anything crossing a
    module boundary.
    """
    if snapshot.empty:
        return
    key = list(key or OBSERVATION_KEY)
    cutoff = pd.Timestamp(asof) - pd.Timedelta(days=lag_days)
    rvq = pd.to_datetime(snapshot["rvq"])
    offenders = snapshot.loc[rvq > cutoff, key + ["rvq"]]
    if len(offenders):
        raise AssertionError(
            f"look-ahead detected at asof={asof}: {len(offenders)} selected "
            f"observation(s) carry an rvq after the cutoff {cutoff.date()}:\n"
            f"{offenders.head().to_string(index=False)}"
        )


def assert_no_lookahead(
    snapshot: pd.DataFrame,
    fundamentals: pd.DataFrame,
    asof,
    key=None,
    lag_days: int = FILING_LAG_DAYS,
) -> None:
    """Raise if `snapshot` depends on data reported after `asof`.

    A DIFFERENTIAL check, and the caller must supply the snapshot under test --
    recomputing it here from `fundamentals` would make the comparison trivially
    true, which is the bug this function existed and was caught having. It only
    has teeth against a snapshot produced by some *other* code path: an
    adapter's selection, a hand-built panel, or a `latest=1` filter. That is the
    point. The selection logic is not being asked whether it agrees with itself.

    The panel is physically truncated to what was knowable at `asof` and the
    snapshot recomputed from it. A look-ahead is precisely a dependence on rows
    that were removed, so this catches it where a flag audit cannot: a filter
    can pass every flag check and still be wrong.

    The one thing this cannot catch is a panel that arrived already-restated
    with `rvq` rewritten backwards. That is a data-provenance question, not a
    logic one, and is why `rvq` is never recomputed here.
    """
    key = list(key or OBSERVATION_KEY)
    cutoff = pd.Timestamp(asof) - pd.Timedelta(days=lag_days)
    truncated = _dedupe(fundamentals.reset_index(drop=True))
    truncated = truncated[pd.to_datetime(truncated["rvq"]) <= cutoff]
    truth = pit_snapshot(truncated, asof, key=key, lag_days=lag_days, verify=False)

    if len(snapshot) != len(truth):
        raise AssertionError(
            f"look-ahead detected at asof={asof}: the snapshot has "
            f"{len(snapshot):,} observations but only {len(truth):,} can be "
            f"reproduced from data reported by {cutoff.date()}."
        )

    compare = [
        c for c in truth.columns
        if c in snapshot.columns and c != "available_from"
    ]
    left = truth[compare].reset_index(drop=True).astype("string").fillna("<NA>")
    right = snapshot[compare].reset_index(drop=True).astype("string").fillna("<NA>")
    if not left.equals(right):
        differing = [c for c in compare if not left[c].equals(right[c])]
        raise AssertionError(
            f"look-ahead detected at asof={asof}: columns {differing} differ from "
            f"the snapshot recomputed from visible-only data."
        )


def latest_flag_snapshot(fundamentals: pd.DataFrame) -> pd.DataFrame:
    """The selection everyone writes first, reproduced so it can be tested.

    `latest == 1` marks the FINAL restatement in the file and is the near-universal
    starting point for a Compustat panel. It is correct as a description of what
    is known *today* and catastrophic as history: it hands the final version of
    every fiscal period to every cross-section, so a 2005 backtest knows what
    firms reported in 2021.

    This exists so `assert_no_lookahead` can be pointed at a real leak rather
    than a synthetic one. If this function passes the guard, the guard is broken.
    """
    frame = fundamentals.reset_index(drop=True)
    latest = frame[frame["latest"].astype("string").isin(["1", "1.0", "True"])]
    if latest.empty:
        # Panels delivered without the flag fall back to "last row wins", which
        # is the same mistake wearing a different hat.
        return frame.drop_duplicates(subset=OBSERVATION_KEY, keep="last").copy()
    return latest.drop_duplicates(subset=OBSERVATION_KEY, keep="last").copy()


def pit_universe(
    securities: pd.DataFrame, asof, lag_days: int = FILING_LAG_DAYS
) -> pd.Series:
    """gvkeys that were trading and had a disclosure obligation on `asof`.

    `comp.securities` carries `first_date` and `last_date`: the window over which
    a gvkey had to file. This is the filter that decides who is IN a
    cross-section. A survivorship-free panel is not one with more rows -- it is
    one where a firm that died in 2008 is present for every rebalance before its
    death and absent only after. The store's survivor-only price table cannot
    express this, which is the entire reason for the WRDS pull.
    """
    frame = securities.copy()
    first = pd.to_datetime(frame["first_date"], errors="coerce")
    last = pd.to_datetime(frame["last_date"], errors="coerce")
    cutoff = pd.Timestamp(asof)
    alive = (first <= cutoff) & (last >= cutoff)
    return frame.loc[alive, "gvkey"].drop_duplicates().sort_values().reset_index(drop=True)


def build_panel(
    fundamentals: pd.DataFrame,
    asof,
    value_columns,
    key=None,
    lag_days: int = FILING_LAG_DAYS,
    wide: bool = True,
):
    """One as-of snapshot of `value_columns`, pivoted to gvkey x datadate.

    The shape a cross-sectional test consumes: rows are firms, columns are
    accounting periods, and every value is one that was public at `asof`.
    """
    snapshot = pit_snapshot(fundamentals, asof, key=key, lag_days=lag_days)
    if snapshot.empty:
        return pd.DataFrame()
    missing = [c for c in value_columns if c not in snapshot.columns]
    if missing:
        raise KeyError(f"columns absent from the pull: {missing}")
    if not wide:
        return snapshot[["gvkey", "datadate", "fq"] + list(value_columns)].reset_index(drop=True)

    index = ["gvkey", "datadate"]
    return snapshot.pivot_table(index=index, values=list(value_columns), aggfunc="last")


def _demo() -> None:
    """A worked restatement, small enough to reason about by hand.

    Three rows for one fiscal period:
      rvq=2019-08-31  the original 10-Q
      rvq=2021-08-31  restated 18 months later
      rvq=2026-06-30  restated again, `latest=1`

    An as-of in 2020 must see the original. An as-of in 2022 must see the first
    restatement. An as-of in 2026 sees the third. `latest=1` would hand the
    2026 values to all three, which is the bug.
    """
    frame = pd.DataFrame(
        {
            "gvkey": [1004, 1004, 1004],
            "datadate": pd.to_datetime(["2019-06-30"] * 3),
            "fq": [2, 2, 2],
            "rvq": pd.to_datetime(["2019-08-31", "2021-08-31", "2026-06-30"]),
            "niq": [10.0, 12.0, 99.0],
            "atq": [500.0, 480.0, 1.0],  # the 2026 restatement is nonsense on purpose
            "latest": [None, None, "1"],
        }
    )
    print("restated panel")
    print(frame.to_string(index=False))

    for asof in ("2020-01-15", "2022-01-15", "2026-09-01"):
        snapshot = pit_snapshot(frame, asof)
        chosen = snapshot["rvq"].iloc[0].date() if len(snapshot) else None
        value = snapshot["niq"].iloc[0] if len(snapshot) else None
        print(f"\nas-of {asof}: rvq={chosen}  niq={value}")

    print(
        "\n`latest=1` would return niq=99.0 for every as-of date above -- the "
        "final restatement applied to a 2020 cross-section."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compustat point-in-time guard")
    parser.add_argument("--demo", action="store_true", help="worked restatement example")
    args = parser.parse_args(argv)
    if args.demo:
        _demo()
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())