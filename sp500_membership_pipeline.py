#!/usr/bin/env python3
"""
S&P 500 Point-in-Time Membership Pipeline.

Reconstructs which symbols were actual S&P 500 members on any historical
date, fixing a real gap named in the 2026-09-05 backtest rigor audit: every
other universe in this repo (symbol_universe.csv, prices, index_members) is
a single CURRENT-DAY snapshot, so a backtest that filters to "S&P 500 names"
is silently using 2026's membership on 1999's data (index-inclusion
look-ahead) rather than the membership that actually applied on each date.

Two free, keyless Wikipedia sources combine to make this reconstructable:
  1. "List of S&P 500 companies" -- TODAY's 503 constituents (also the
     source for the existing index_constituents_pipeline.py snapshot).
  2. "Historical components of the S&P 500" -- a 407-row change log, one
     row per index reconstitution event (Effective Date, Added Ticker,
     Removed Ticker, Reason), covering 1976-07-01 to present. NOTE: the
     main "List of..." article used to embed this table directly (several
     older blog posts and community datasets assume that); as of this
     pipeline's build (2026-09-06) it has moved to its own article -- a
     plain `pd.read_html` against the main page now returns only the
     current-constituents table, confirmed live before writing this.

Reconstruction walks the change log from MOST RECENT to OLDEST, starting
from today's known membership and undoing one event at a time (see
reconstruct_membership()). A symbol still a member with no earlier removal
on record, or one whose earliest recorded event is a removal with no prior
addition, is LEFT-CENSORED at the log's own floor (1976-07-01) -- treated as
"a member since before this table's coverage begins," which is what every
comparable community reconstruction of this table does, not a gap specific
to this pipeline.

CONFIRMED LIVE 2026-09-06: the log itself has holes, in BOTH directions.

MCK (McKesson) and T (AT&T) are both current members whose ONLY appearance
anywhere in the 407-row log is a REMOVAL (1994-09-30 and 2005-11-18
respectively) with no later re-addition row -- Wikipedia's own history for
these two genuinely loses the thread. reconstruct_membership() repairs
this the conservative way (tiles a second "current, since the recorded
closure date" interval onto the gap rather than inventing an unknown real
re-entry date or dropping a verified-current member outright) -- see its
docstring. Treat `historical_constituents()` results for MCK/T (and any
other symbol this pattern is later found to affect) between their recorded
closure and their real, undocumented re-entry as approximate, not exact.

The mirror image (caught by code review 2026-09-06, not this pipeline's
own live verification): NCC (National City, added 1994-09-30 when MCK was
removed, acquired by PNC in 2008) is NOT a current member, but NCC's own
eventual removal is never logged as a `rem_ticker` anywhere in the table
either. An earlier version of this pipeline defaulted such a symbol's end
to the log's own most recent date -- which, since that date sits only
weeks before whenever the pipeline happens to run, silently claimed NCC
(and ~19 other symbols in the same bucket) as a real S&P 500 member for
essentially its entire multi-decade absence. Fixed: reconstruct_membership()
now DROPS an addition event entirely when the symbol isn't current AND no
later removal resolves it, rather than guessing an end date. This means
NCC's real 1994-2008 membership goes UNRECORDED (a real loss of history)
rather than wrongly extended to the present (a real corruption of every
downstream universe filter) -- the same "understate, never overstate"
principle as the MCK/T repair above, applied to the opposite-direction gap.

A milder, harmless variant of the same source looseness: IR (Ingersoll
Rand) has TWO "added" rows (2010-11-17, 2020-03-02) with no removal of IR
between them, likely an undocumented re-domiciling logged as a fresh add
rather than a swap -- this reconstructs as two overlapping open intervals
for the same still-current symbol. Functionally harmless (both agree IR is
a member; historical_constituents() de-duplicates the symbol), left as-is
rather than special-cased.

What this does NOT fix: PRICE HISTORY for names that are no longer public
at all (delisted/bankrupt, not just dropped from the S&P 500 while still
trading elsewhere). Vetted 2026-09-06 (data-source-vetting checklist):
yfinance returns EMPTY for genuinely-delisted tickers (MON, AYE, HNG all
0 rows) and, more dangerously, returns real-looking data for OTHER
currently-listed tickers that happen to reuse an old symbol (SUN, USL --
neither is the company that once traded under that ticker). NO-GO on
yfinance for this specific problem; no free alternative found. A backtest
using this membership table against `prices`/`yfinance_universe_prices`
still can't see a company that stopped trading entirely -- it can only
stop CREDITING S&P-500-conditioned analysis to a name for dates outside
its real membership window, and stop OMITTING a real member's dates just
because it isn't in the index today.

Output: storage/raw/sp500_membership/year=YYYY/month=MM/*.parquet
  Columns: symbol, start_date (NaT = left-censored, member since before
  1976-07-01), end_date (NaT = still a current member), fetched_at.

OPERATIONAL NOTE (code review 2026-09-06): this table is FULLY regenerated
every run, but curated.py's dedup is additive over the natural key
(symbol, start_date, end_date) across every raw file ever written -- it
never retracts a row whose key no longer appears in the latest run. Two
runs on the SAME day overwrite the same raw file (harmless), but if this
pipeline's own RECONSTRUCTION LOGIC changes on a later day, the old raw
file's now-superseded rows persist in curated output forever unless
deleted by hand. Delete prior files under storage/raw/sp500_membership/
before rerunning after any change to reconstruct_membership() itself.

CLI:
  python sp500_membership_pipeline.py             # incremental (only mode --
                                                    # the whole reconstruction
                                                    # is cheap and re-derived
                                                    # fresh every run)
  python sp500_membership_pipeline.py --backfill   # identical; no separate
                                                    # backfill depth to add
"""

import argparse
import datetime
import time
from io import StringIO

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR = "storage/raw/sp500_membership"
CURRENT_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CHANGES_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
MAX_RETRIES = 3


def _get_with_retry(url: str) -> str:
    """GET url, retrying on 429/transient errors (same pattern as
    wikipedia_pipeline.py's _get_with_retry) -- the wiring checklist in
    CLAUDE.md requires this for every new pipeline; a plain requests.get
    had no backoff at all before this fix (caught by code review)."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                time.sleep(60 * attempt)
                continue
            r.raise_for_status()
            return r.text
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(10 * attempt)
    raise last_exc


def _norm_ticker(t) -> "str | None":
    """BRK.B-style dots -> dashes, matching this repo's existing convention
    (index_constituents_pipeline.py, symbol_universe.csv). Also strips
    footnote/reference artifacts a wiki-table cell occasionally carries --
    confirmed live 2026-09-06 on the 2013-12-02 spin-off row, whose cells
    render as literal "ALLE |" / "JCP |" (a trailing pipe from whatever
    wikitext template that row uses, not a pd.read_html bug -- verified
    against the raw fetched HTML). Taking only the first whitespace-split
    token discards that kind of trailing noise generically rather than
    special-casing these two tickers."""
    if t is None or (isinstance(t, float) and pd.isna(t)):
        return None
    s = str(t).strip().replace(".", "-").split()
    s = s[0] if s else ""
    return s if s and s.lower() != "nan" else None


def fetch_current() -> "set[str]":
    html = _get_with_retry(CURRENT_URL)
    df = pd.read_html(StringIO(html))[0]
    if "Symbol" not in df.columns:
        raise ValueError(
            f"expected a 'Symbol' column in the current-constituents table, "
            f"got {list(df.columns)} -- Wikipedia's page layout may have "
            f"changed; do not silently guess a column mapping")
    return {t for t in (_norm_ticker(x) for x in df["Symbol"]) if t}


def fetch_changes() -> pd.DataFrame:
    html = _get_with_retry(CHANGES_URL)
    df = pd.read_html(StringIO(html))[0]
    expected_cols = ["date", "add_ticker", "add_name", "rem_ticker",
                     "rem_name", "reason", "refs"]
    if len(df.columns) != len(expected_cols):
        # Fail loudly rather than blindly rename -- this table has already
        # moved to a different article once (see module docstring); a
        # future restructure with a different column count/order must not
        # silently corrupt reconstruct_membership() with mislabeled columns.
        raise ValueError(
            f"expected {len(expected_cols)} columns in the historical-"
            f"components change log, got {len(df.columns)}: "
            f"{list(df.columns)} -- Wikipedia's table layout may have "
            f"changed; update expected_cols after checking it by hand")
    df.columns = expected_cols
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["add_ticker"] = df["add_ticker"].map(_norm_ticker)
    df["rem_ticker"] = df["rem_ticker"].map(_norm_ticker)
    df = df.dropna(subset=["date"])
    return df[["date", "add_ticker", "rem_ticker"]].sort_values(
        "date", ascending=False).reset_index(drop=True)


def reconstruct_membership(current_tickers: "set[str]",
                           changes_desc: pd.DataFrame) -> pd.DataFrame:
    """
    (symbol, start_date, end_date) membership intervals from today's
    constituents + the descending-by-date change log.

    Walking newest-to-oldest, `open_end[symbol]` tracks the date at which a
    currently-open (going backward) interval will eventually close once we
    hit that symbol's ADD event: None means "open through today," a real
    date means "this symbol was removed on that date, so the interval
    being discovered now ends there." Every finalized interval is emitted
    the moment its ADD event is found; a symbol that never has an ADD event
    at or after the log's floor is left-censored (start_date=NaT) once the
    log is exhausted.
    """
    open_end: "dict[str, pd.Timestamp | None]" = {t: None for t in current_tickers}
    intervals = []
    # Sentinel distinguishing "no dict entry" (no later removal was ever
    # logged for this symbol) from a real `None`/date value.
    _no_later_event = object()

    for row in changes_desc.itertuples(index=False):
        d, add_t, rem_t = row.date, row.add_ticker, row.rem_ticker
        # pd.notna(), not `is not None`: a missing wiki-table cell survives
        # as float NaN through pd.read_html/pd.DataFrame construction, not
        # Python None -- `is not None` alone silently treats NaN as a real
        # ticker and corrupts open_end with a bogus NaN-keyed interval
        # (caught by test_worked_example_from_docstring's row count).
        if pd.notna(add_t):
            if add_t in current_tickers:
                end = open_end.pop(add_t, None)
                intervals.append({"symbol": add_t, "start_date": d, "end_date": end})
            else:
                # Fixed 2026-09-06 (code review caught it): a NON-current
                # symbol with no later logged removal (e.g. NCC/National
                # City, added 1994-09-30, acquired by PNC in 2008 with its
                # own removal never logged as a rem_ticker anywhere) used to
                # default `end` to the log's own most recent date -- which,
                # since that date sits only weeks before whenever this
                # pipeline runs, silently claimed the symbol as a current-ish
                # member for essentially its entire real, multi-decade
                # absence. Dropping the interval entirely is the safer
                # error: NCC's real 1994-2008 stint goes unrecorded rather
                # than wrongly extended to the present. See module docstring.
                end = open_end.pop(add_t, _no_later_event)
                if end is not _no_later_event:
                    intervals.append({"symbol": add_t, "start_date": d,
                                      "end_date": end})
        if pd.notna(rem_t):
            open_end[rem_t] = d

    # Repair a real gap confirmed live in the source table (2026-09-06): a
    # CURRENT member whose only recorded event is a removal with no later
    # ADD to reopen it -- MCK (closed 1994-09-30) and T (closed 2005-11-18)
    # are both S&P 500 members today with no re-addition row anywhere in
    # the 407-row log, so Wikipedia's own change history has a documented
    # hole for at least these two. Rather than silently dropping a
    # verified-current member or inventing an unknown re-entry date, tile a
    # second "current" interval onto the exact recorded closure date. This
    # UNDERSTATES true membership across the real, undocumented gap (the
    # name may have exited and re-entered more than once in between) but
    # never OVERSTATES it into dates before the log's own last recorded
    # action -- the safer error direction for a universe filter, and the
    # two intervals tile exactly at the boundary with no coverage gap.
    #
    # Only symbols with NO open interval yet qualify -- a symbol like DOW
    # (removed 2017-09-01 for the DWDP merger, correctly re-added
    # 2019-04-02) already got its legitimate "current" row from the loop
    # above; the 2017 closure is real, historical, and belongs to a
    # DIFFERENT, already-finished stint. Repairing it too would fabricate a
    # THIRD, spurious "current since 2017" row alongside the correct one.
    already_open = {iv["symbol"] for iv in intervals if iv["end_date"] is None}
    for sym in current_tickers:
        if sym not in already_open and open_end.get(sym) is not None:
            intervals.append({"symbol": sym, "start_date": open_end[sym],
                              "end_date": None})

    for sym, end in open_end.items():
        intervals.append({"symbol": sym, "start_date": pd.NaT, "end_date": end})

    return pd.DataFrame(intervals).sort_values(
        ["symbol", "start_date"], na_position="first").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="S&P 500 point-in-time membership reconstruction")
    parser.add_argument("--backfill", action="store_true",
                        help="No-op: the reconstruction is always full and "
                             "cheap, there is no separate backfill depth")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    mode = "backfill" if args.backfill else "incremental"
    print(f"S&P 500 Membership Pipeline  mode={mode}\n")

    current = fetch_current()
    print(f"  current constituents: {len(current)}")
    changes = fetch_changes()
    print(f"  change-log rows: {len(changes)} "
         f"({changes['date'].min().date()} .. {changes['date'].max().date()})")

    membership = reconstruct_membership(current, changes)
    membership["fetched_at"] = now.isoformat()
    n_censored = membership["start_date"].isna().sum()
    n_current = membership["end_date"].isna().sum()
    print(f"  {len(membership)} intervals, {membership['symbol'].nunique()} "
         f"distinct symbols ({n_censored} left-censored at the log floor, "
         f"{n_current} still open/current)")

    path = write_partitioned(membership, BASE_DIR,
                             f"sp500_membership_{mode}_{now.strftime('%Y%m%d')}.parquet")
    print(f"  -> {path}")
    print("\n--- S&P 500 MEMBERSHIP PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
