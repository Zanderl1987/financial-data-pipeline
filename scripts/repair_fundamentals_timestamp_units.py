#!/usr/bin/env python3
"""
Rewrite raw fundamentals files whose timestamp columns are timestamp[ns] as
timestamp[us], so every file in the glob carries one type.

Why: raw files written before 2026-10-04 store period_end/start_date as
timestamp[us], later ones as timestamp[ns] (pandas' default resolution differs
between builds). DuckDB unifies a mixed glob to ns, and one filer's start_date
of 0202-02-01 (MIND Technology 10-Q, a typo for 2022) fits in us but not in
ns -- so every raw read of fundamentals_quarterly failed and curated.py had not
rebuilt it since 2026-10-03. fundamentals_pipeline._to_dates now pins us for
new files; this repairs the files already written.

Values are not changed. A file is rewritten only if every timestamp survives
the ns -> us cast exactly (checked by casting back and comparing), and the new
file replaces the old one atomically. Dry run unless --apply.

Usage:
  python scripts/repair_fundamentals_timestamp_units.py            # report only
  python scripts/repair_fundamentals_timestamp_units.py --apply
  python scripts/repair_fundamentals_timestamp_units.py --root <storage/raw/fundamentals>
"""

import argparse
import glob
import os
import sys

import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOT = os.path.join(REPO_ROOT, "storage", "raw", "fundamentals")


def _us_schema(schema: pa.Schema) -> "pa.Schema | None":
    """The schema with ns timestamps as us, or None if there are none."""
    fields, changed = [], False
    for field in schema:
        if pa.types.is_timestamp(field.type) and field.type.unit == "ns":
            fields.append(field.with_type(pa.timestamp("us", tz=field.type.tz)))
            changed = True
        else:
            fields.append(field)
    return pa.schema(fields, metadata=schema.metadata) if changed else None


def repair_file(path: str, apply: bool) -> "tuple[bool, str]":
    """(needs_repair, message). Rewrites in place when apply is True."""
    target = _us_schema(pq.read_schema(path))
    if target is None:
        return False, "already us"
    # ParquetFile, not pq.read_table: read_table on a path inside year=/month=
    # directories adds those Hive partitions as columns, changing the schema.
    table = pq.ParquetFile(path).read()
    converted = table.cast(target, safe=True)  # raises on any lossy value
    if not converted.cast(table.schema).equals(table):
        raise ValueError(f"{path}: values do not round-trip ns -> us -> ns")
    if apply:
        tmp = path + ".tmp"
        pq.write_table(converted, tmp, compression="snappy")
        if not pq.ParquetFile(tmp).read().equals(converted):
            os.remove(tmp)
            raise ValueError(f"{path}: rewritten file does not read back identically")
        os.replace(tmp, path)
    cols = [f.name for f in table.schema if pa.types.is_timestamp(f.type) and f.type.unit == "ns"]
    return True, f"{len(table):,} rows, ns -> us: {', '.join(cols)}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=DEFAULT_ROOT, help="raw fundamentals directory")
    ap.add_argument("--apply", action="store_true", help="rewrite files (default: report only)")
    args = ap.parse_args(argv)

    files = sorted(glob.glob(os.path.join(args.root, "**", "*.parquet"), recursive=True))
    n_fix = 0
    for path in files:
        needs, msg = repair_file(path, args.apply)
        if needs:
            n_fix += 1
            print(f"{'REWROTE' if args.apply else 'WOULD REWRITE'} {os.path.relpath(path, args.root)}: {msg}")
    verb = "rewrote" if args.apply else "would rewrite"
    print(f"\n{len(files)} files scanned, {verb} {n_fix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
