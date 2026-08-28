"""Manually (re)build the pilot Iceberg tables from the curated snapshots.

Usage:
    C:\\ProgramData\\anaconda3\\python.exe migrate_pilot.py            # all pilot tables
    C:\\ProgramData\\anaconda3\\python.exe migrate_pilot.py --only macro,prices

Each pilot table is mirrored (full replace) from
storage/curated/<table>/<table>.parquet into the local Iceberg warehouse at
storage/iceberg/pilot/<table>/, written via pyiceberg with FsspecFileIO so the
URIs are readable by DuckDB's iceberg_scan. Run this after curated.py whenever
you want the Iceberg mirror refreshed.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import duckdb

import iceberg_pilot as P

CURATED_ROOT = Path(__file__).parent / "storage" / "curated"


def _curated_path(table: str) -> Path:
    return CURATED_ROOT / table / f"{table}.parquet"


def sync_table(table: str) -> int:
    path = _curated_path(table)
    if not path.exists():
        print(f"== {table}: curated snapshot missing ({path}) - SKIPPED")
        return 0
    t0 = time.time()
    rows = P.replace_from_parquet(f"{P.PILOT_NAMESPACE}.{table}", str(path))
    md = P.latest_metadata(f"{P.PILOT_NAMESPACE}.{table}")
    con = duckdb.connect()
    con.execute("INSTALL iceberg; LOAD iceberg")
    verified = con.execute("SELECT COUNT(*) FROM iceberg_scan('{}')".format(md)).fetchone()[0]
    print(f"== {table}: wrote {rows:,} rows in {time.time()-t0:.1f}s; verified {verified:,} via iceberg_scan")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild pilot Iceberg tables from curated snapshots")
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated subset of pilot tables to sync (default: all). "
        f"Valid: {', '.join(P.PILOT_TABLES)}",
    )
    args = parser.parse_args(argv)

    tables = list(P.PILOT_TABLES)
    if args.only:
        requested = [t.strip() for t in args.only.split(",") if t.strip()]
        invalid = [t for t in requested if t not in P.PILOT_TABLES]
        if invalid:
            print(f"Unknown pilot table(s): {', '.join(invalid)}")
            return 2
        tables = requested

    for table in tables:
        sync_table(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
