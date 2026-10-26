#!/usr/bin/env python3
"""
Depth Audit -- how deep each table actually is, not just whether it exists.

validate.py answers "is this table well-formed?" and coverage_audit.py answers
"is every upstream series wired up?". Neither answers "is there enough of it to
use?", and that is the question a consumer asks. Real cases, both found by the
backtester's strategy catalog rather than here:

  - `options_history` has rows, a schema, passing validation -- and 4 symbols
    starting 2023-12. It read as option-history coverage until someone
    described it, and four strategy records were nearly marked testable on it.
  - a `fundamentals` / `fundamentals_quarterly` naming slip would have marked
    195 records untestable, silently, because "present" was the only check.

For every CATALOG table this reads the curated snapshot (what query.py serves)
and reports rows, the entity count (symbols / series / countries...), the date
span and the number of distinct dates. Tables a consumer depends on declare a
floor in DEPTH_FLOORS -- the depth that consumer NEEDS, not what the table
holds today -- and get a verdict against it. Every other table is listed as
UNDECLARED with its numbers, so the absence of a floor is visible rather than
read as health.

CLI:
  python depth_audit.py                    # full report
  python depth_audit.py --table options_history
  python depth_audit.py --fail-on-thin     # exit 1 if a declared floor fails
                                           # (for the weekly scheduled task)
  python depth_audit.py --root <dir>       # audit another clone's storage/curated
"""

import argparse
import datetime
import glob as _glob_mod
import os
import sys

import duckdb
import pandas as pd

import query as q
import validate as v

CURATED_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage", "curated")

# First column present wins. Order matters: `symbol` beats `country` on a
# per-symbol table that also carries a country attribute.
ENTITY_COLS = ("symbol", "ticker", "series_id", "cusip", "commodity", "index_code",
               "index_name", "pair", "ref_area", "country")

# What a consumer needs from a table, with the reason. Floors are requirements,
# not snapshots of today's contents: a table that meets its floor is usable for
# the stated purpose, one that misses it is not, however many rows it has.
#   min_entities   -- distinct values of the entity column
#   first_date_by  -- history must start on or before this date
#   min_rows       -- for tables with no entity column
#   max_stale_days -- newest date must be within this many days of today
#   known_gap      -- the floor is known to fail and why (no source, paid data):
#                     still reported, but does not trip --fail-on-thin, so the
#                     weekly flag means something new went wrong
DEPTH_FLOORS: dict[str, dict] = {
    "options_history": {
        "min_entities": 100, "first_date_by": "2010-01-01",
        "why": "option-strategy backtests need a multi-year chain history across "
               "a real universe (strategy catalog: put-write, VRP straddle, dispersion)",
        "known_gap": "2026-09-12: no free source; needs paid EOD chain data "
                     "(ORATS / IvyDB / Polygon) -- backtester TODO",
    },
    "short_interest": {
        "min_entities": 500,
        "why": "a short-interest factor ranks a cross-section; a watchlist cannot",
        "known_gap": "full-market NMS short interest needs registered FINRA API "
                     "credentials; yfinance snapshot is watchlist-only",
    },
    "yfinance_universe_prices": {
        # No freshness floor: yfinance_universe_backfill.py is a one-shot
        # backfill with no scheduled refresh (last run 2026-09-12), so a stale
        # verdict would be an alarm no job can clear.
        "min_entities": 2000, "first_date_by": "2000-01-01",
        "why": "strategy-catalog equity universe (us_equity_prices_daily, etf_prices_daily)",
    },
    "fundamentals_quarterly": {
        "min_entities": 500, "first_date_by": "2010-01-01",
        "why": "strategy-catalog us_equity_fundamentals_quarterly",
    },
    "prices": {
        "max_stale_days": 7,
        "why": "nightly SchwabUniverseIncrementalPrices task keeps the ~27.7k-symbol "
               "universe current",
    },
    "forex_rates": {
        "min_entities": 10, "first_date_by": "2000-01-01", "max_stale_days": 7,
        "why": "strategy-catalog fx_spot_rates_daily (carry, currency momentum)",
    },
    "policy_rates": {
        "min_entities": 10, "first_date_by": "1999-01-01",
        "why": "strategy-catalog policy_rates_multi_country (fx carry)",
    },
    "cboe_strategy_indices": {
        "min_entities": 11, "first_date_by": "2007-01-01", "max_stale_days": 7,
        "why": "strategy-catalog cboe_strategy_index_levels (put-write premium test)",
    },
    "cboe_volatility": {
        "first_date_by": "2004-01-01", "max_stale_days": 7,
        "why": "strategy-catalog vix_spot_daily",
    },
    "treasury_yield_curve": {
        "first_date_by": "2000-01-01",
        "why": "strategy-catalog risk_free_rate / treasury_bill_rate",
    },
    "agriculture": {
        "min_entities": 13, "first_date_by": "2015-01-31",
        "why": "backtester ag_seasonal: all 13 AgProvider symbols from 2015",
    },
}

BAD_VERDICTS = ("NO SNAPSHOT", "EMPTY", "THIN", "SHALLOW", "STALE")


def _quote(path: str) -> str:
    return "'" + path.replace("\\", "/").replace("'", "''") + "'"


def _raw_files(table: str, root: str = CURATED_ROOT) -> int:
    """Raw files behind a table, so NO SNAPSHOT says whether curated was never built.

    Resolved against the raw directory beside ``root`` (storage/raw next to
    storage/curated), not this module's own storage, so --root audits another
    clone completely.
    """
    pattern = q.CATALOG.get(table)
    raw_root = q._STORAGE_ROOT.replace("\\", "/")
    if not pattern or "*" not in pattern or not pattern.startswith(raw_root):
        return 0
    local = os.path.join(os.path.dirname(os.path.abspath(root)), "raw",
                         pattern[len(raw_root):].lstrip("/"))
    return len(_glob_mod.glob(local.replace("/", os.sep), recursive=True))


def measure(table: str, root: str = CURATED_ROOT, con=None, today=None) -> dict:
    """Depth numbers for one table's curated snapshot."""
    today = today or datetime.date.today()
    path = os.path.join(root, table, f"{table}.parquet")
    row = {"table": table, "rows": None, "entity_col": None, "entities": None,
           "date_col": None, "first": None, "last": None, "years": None,
           "dates": None, "future": None}
    if not os.path.exists(path):
        return row
    own = con is None
    con = con or duckdb.connect()
    try:
        src = f"read_parquet({_quote(path)})"
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
        entity = next((c for c in ENTITY_COLS if c in cols), None)
        date_col = v.SCHEMAS.get(table, {}).get("date_col")
        if date_col not in cols:
            date_col = "date" if "date" in cols else None
        parts = ["count(*)"]
        parts.append(f'count(DISTINCT "{entity}")' if entity else "NULL")
        if date_col:
            d = f'TRY_CAST("{date_col}" AS DATE)'
            parts += [f"min({d})", f"max({d})", f"count(DISTINCT {d})",
                      f"count(*) FILTER (WHERE {d} > DATE '{today.isoformat()}')"]
        else:
            parts += ["NULL", "NULL", "NULL", "NULL"]
        n, n_ent, first, last, n_dates, n_future = con.execute(
            f"SELECT {', '.join(parts)} FROM {src}").fetchone()
    finally:
        if own:
            con.close()
    row.update(rows=int(n), entity_col=entity, date_col=date_col,
               entities=None if n_ent is None else int(n_ent),
               first=first, last=last,
               dates=None if n_dates is None else int(n_dates),
               future=None if n_future is None else int(n_future))
    if first is not None and last is not None:
        row["years"] = round((last - first).days / 365.25, 1)
    return row


def judge(row: dict, floor: "dict | None", today=None) -> tuple[str, str]:
    """(verdict, note) for one measured table against its declared floor."""
    today = today or datetime.date.today()
    if row["rows"] is None:
        n_raw = row.get("raw_files") or 0
        # Raw files without a snapshot mean curated.py never built this table,
        # and query.py silently falls back to the (duplicated) raw glob.
        return "NO SNAPSHOT", (f"{n_raw} raw files but no curated snapshot" if n_raw else "")
    if row["rows"] == 0:
        return "EMPTY", "curated snapshot has 0 rows"
    future = (f"{row['future']} rows dated after today (max {row['last']})"
              if row.get("future") else "")
    if not floor:
        return "UNDECLARED", future
    misses = []
    if "min_entities" in floor:
        if row["entities"] is None:
            misses.append(("THIN", "no entity column to count"))
        elif row["entities"] < floor["min_entities"]:
            misses.append(("THIN", f"{row['entities']} {row['entity_col']} < "
                                   f"{floor['min_entities']} needed"))
    if "min_rows" in floor and row["rows"] < floor["min_rows"]:
        misses.append(("THIN", f"{row['rows']} rows < {floor['min_rows']} needed"))
    if "first_date_by" in floor:
        need = datetime.date.fromisoformat(floor["first_date_by"])
        if row["first"] is None:
            misses.append(("SHALLOW", "no parseable date column"))
        elif row["first"] > need:
            misses.append(("SHALLOW", f"starts {row['first']}, needs {need} or earlier"))
    if "max_stale_days" in floor and row["last"] is not None:
        age = (today - row["last"]).days
        if age > floor["max_stale_days"]:
            misses.append(("STALE", f"newest date {row['last']} is {age} days old, "
                                    f"allowed {floor['max_stale_days']}"))
    if not misses:
        return "OK", future
    verdict = next(k for k in ("THIN", "SHALLOW", "STALE") if any(m[0] == k for m in misses))
    note = "; ".join(m[1] for m in misses) + f" -- {floor['why']}"
    if floor.get("known_gap"):
        note = f"(known gap: {floor['known_gap']}) {note}"
    return verdict, f"{note}; {future}" if future else note


def audit(tables=None, root: str = CURATED_ROOT, floors=None) -> pd.DataFrame:
    floors = DEPTH_FLOORS if floors is None else floors
    tables = list(tables) if tables else sorted(q.CATALOG)
    con = duckdb.connect()
    rows = []
    try:
        for table in tables:
            try:
                row = measure(table, root, con)
                if row["rows"] is None:
                    row["raw_files"] = _raw_files(table, root)
            except Exception as exc:  # one unreadable snapshot must not hide the rest
                row = {"table": table, "rows": None}
                row["verdict"], row["note"] = "ERROR", f"{type(exc).__name__}: {exc}"
                rows.append(row)
                continue
            row["verdict"], row["note"] = judge(row, floors.get(table))
            rows.append(row)
    finally:
        con.close()
    order = {k: i for i, k in enumerate(("ERROR",) + BAD_VERDICTS + ("OK", "UNDECLARED"))}
    df = pd.DataFrame(rows)
    df["_o"] = df["verdict"].map(order)
    return df.sort_values(["_o", "table"]).drop(columns="_o").reset_index(drop=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Depth, not presence: audit curated table depth")
    ap.add_argument("--table", action="append", help="audit only this table (repeatable)")
    ap.add_argument("--root", default=CURATED_ROOT, help="storage/curated directory to audit")
    ap.add_argument("--fail-on-thin", action="store_true",
                    help="exit 1 if any table with a declared floor fails it")
    args = ap.parse_args(argv)

    report = audit(args.table, args.root)
    with pd.option_context("display.width", 250, "display.max_rows", None,
                           "display.max_colwidth", 120):
        print(report.drop(columns=[c for c in ("note", "raw_files") if c in report]).to_string(index=False))
    flagged = report[report["note"].fillna("") != ""]
    if not flagged.empty:
        print("\nNotes:")
        for _, r in flagged.iterrows():
            print(f"  {r['table']} [{r['verdict']}]: {r['note']}")

    declared = report[report["table"].isin(DEPTH_FLOORS)]
    known = {t for t, f in DEPTH_FLOORS.items() if f.get("known_gap")}
    failed = declared[(declared["verdict"] != "OK") & ~declared["table"].isin(known)]
    counts = report["verdict"].value_counts()
    print("\n" + "  |  ".join(f"{counts.get(k, 0)} {k}" for k in
                              ("OK", "THIN", "SHALLOW", "STALE", "EMPTY", "NO SNAPSHOT", "ERROR", "UNDECLARED")))
    n_known = int(((declared["verdict"] != "OK") & declared["table"].isin(known)).sum())
    print(f"{len(failed)} DECLARED FLOOR(S) FAILED ({n_known} known gap(s) not counted)")
    return 1 if (args.fail_on_thin and len(failed)) else 0


if __name__ == "__main__":
    sys.exit(main())
