#!/usr/bin/env python3
"""
status.py — one-command freshness dashboard for every CATALOG table.

Combines three already-existing signals (no new data logic):
  - row count            : query.py's DuckDB view over ALL files (q.tables())
  - max data date         : query.py's date_range() (tables with a 'date' col)
  - days since last write : mtime of the newest file on disk (validate.py's
                             _latest_file(), the same helper validate_all()
                             uses so "latest" always means the same thing)

CLI:
  python status.py                    # every table, sorted stalest-first
  python status.py --stale-days 3     # only tables not written to in >= N days
  python status.py --table prices     # single-table detail
"""

import argparse
import datetime
import os

import pandas as pd

import query as q
from validate import _latest_file


def build_status() -> pd.DataFrame:
    date_ranges = q.date_range().set_index("table")["max_date"].to_dict()
    now = datetime.datetime.now()

    rows = []
    for table, glob_path in sorted(q.CATALOG.items()):
        files = _latest_file(glob_path)
        if not files:
            rows.append({
                "table": table, "rows": 0, "max_date": "",
                "days_stale": None, "status": "NO DATA",
            })
            continue
        try:
            count = q._con().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            count = -1
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(files[-1]))
        rows.append({
            "table":      table,
            "rows":       count,
            "max_date":   date_ranges.get(table, ""),
            "days_stale": round((now - mtime).total_seconds() / 86400, 1),
            "status":     "PASS" if count > 0 else "EMPTY",
        })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="CATALOG freshness dashboard")
    parser.add_argument("--table", help="Show detail for a single table")
    parser.add_argument("--stale-days", type=float, default=None,
                         help="Only show tables not written to in >= N days")
    args = parser.parse_args()

    df = build_status()

    if args.table:
        if args.table not in set(df["table"]):
            print(f"'{args.table}' not in CATALOG.")
            return
        print(df[df["table"] == args.table].to_string(index=False))
        return

    if args.stale_days is not None:
        df = df[(df["days_stale"].isna()) | (df["days_stale"] >= args.stale_days)]

    df = df.sort_values(
        by="days_stale", ascending=False, na_position="first"
    )
    print(df.to_string(index=False))

    no_data = (df["status"] == "NO DATA").sum()
    empty = (df["status"] == "EMPTY").sum()
    print(f"\n{len(df)} tables shown | {no_data} NO DATA | {empty} EMPTY | "
          f"{len(df) - no_data - empty} PASS")


if __name__ == "__main__":
    main()
