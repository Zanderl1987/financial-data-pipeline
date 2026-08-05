"""Pilot Iceberg write/read helpers.

A few CATALOG tables (prices, macro, fundamentals_annual, fundamentals_quarterly)
are mirrored into a local Iceberg warehouse so query.py can read them via real
`iceberg_scan` calls. Everything else stays partitioned Parquet.

Why this module exists:
- The default PyArrowFileIO writes two-slash `file://C:/...` URIs into table
  metadata + manifest files. DuckDB's `iceberg_scan` cannot open those on Windows.
- PyIceberg's `FsspecFileIO` with a THREE-slash warehouse (`file:///C:/...`)
  writes URIs that BOTH pyiceberg and DuckDB 1.5.4 can read. Forcing it via
  `py-io-impl=pyiceberg.io.fsspec.FsspecFileIO` is the key.

Only the pilot tables use this catalog. The pre-existing constituents/shipping
Iceberg tables keep their (DuckDB-unreadable) two-slash URIs; they are not part
of this pilot.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.catalog import load_catalog as _pyiceberg_load_catalog

_STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = _STORAGE_ROOT / "iceberg"
PILOT_CATALOG_DB = ICEBERG_WAREHOUSE / "pilot_catalog.db"
PILOT_NAMESPACE = "pilot"

# Tables mirrored to Iceberg by migrate_pilot.py. Keep in sync with query.py.
PILOT_TABLES = ("prices", "macro", "fundamentals_annual", "fundamentals_quarterly")

# DuckDB 1.5.4's iceberg_scan reads by metadata.json path (read-only); writes
# require a REST catalog, which we don't have. pyiceberg does the writing.
_IO_PROP = {"py-io-impl": "pyiceberg.io.fsspec.FsspecFileIO"}


def _warehouse_uri() -> str:
    """Three-slash file URI so both pyiceberg and DuckDB can open written paths."""
    return "file:///" + ICEBERG_WAREHOUSE.as_posix().replace("\\", "/")


def _table_dir(identifier: str) -> Path:
    namespace, _, name = identifier.partition(".")
    return ICEBERG_WAREHOUSE / namespace / name


def load_catalog():
    return _pyiceberg_load_catalog(
        "pilot",
        type="sql",
        uri=f"sqlite:///{PILOT_CATALOG_DB.as_posix().replace(os.sep, '/')}",
        warehouse=_warehouse_uri(),
        **_IO_PROP,
    )


def latest_metadata(identifier: str) -> str | None:
    """Return the newest *.metadata.json for a pilot table, or None if absent.

    DuckDB's iceberg_scan takes this path directly.
    """
    matches = glob.glob(
        str(_table_dir(identifier) / "metadata" / "*.metadata.json").replace("\\", "/"),
        recursive=False,
    )
    return sorted(matches)[-1].replace("\\", "/") if matches else None


def ensure_table(catalog, identifier: str, arrow_schema: pa.Schema):
    """Create the pilot table if it does not exist yet."""
    try:
        return catalog.load_table(identifier)
    except Exception:
        pass
    try:
        catalog.create_namespace(PILOT_NAMESPACE, properties={})
    except Exception:
        pass  # namespace already exists
    from pyiceberg.io.pyarrow import _pyarrow_to_schema_without_ids

    ice_schema = _pyarrow_to_schema_without_ids(arrow_schema)
    return catalog.create_table(
        identifier=identifier,
        schema=ice_schema,
        properties={
            "write.parquet.compression-codec": "snappy",
            "format-version": "2",
        },
    )


def _schema_matches(table, arrow_schema: pa.Schema) -> bool:
    """True if the existing Iceberg table's field names/types match the parquet."""
    from pyiceberg.io.pyarrow import _pyarrow_to_schema_without_ids

    want = _pyarrow_to_schema_without_ids(arrow_schema)
    have = table.schema()
    if len(want.fields) != len(have.fields):
        return False
    for a, b in zip(want.fields, have.fields):
        if a.name != b.name or str(a.field_type) != str(b.field_type):
            return False
    return True


def replace_from_parquet(identifier: str, parquet_path: str, batch_rows: int = 200_000) -> int:
    """Stream-replace a pilot table with the contents of a local parquet file.

    Full-replace semantics (mirrors the deduplicated curated snapshot): the first
    batch is written via overwrite (AlwaysTrue filter) and subsequent batches via
    append, all inside ONE transaction so the whole sync is a single snapshot.
    Returns the number of rows written.

    If the parquet schema no longer matches the existing table (e.g. curated.py
    normalization changed a column type), the table is dropped and recreated so
    the mirror always tracks the curated snapshot exactly.
    """
    catalog = load_catalog()
    pf = pq.ParquetFile(parquet_path)
    arrow_schema = pf.schema_arrow
    table = ensure_table(catalog, identifier, arrow_schema)

    if not _schema_matches(table, arrow_schema):
        print(f"  Schema changed — dropping + recreating {identifier}")
        catalog.drop_table(identifier)
        table = ensure_table(catalog, identifier, arrow_schema)

    total = 0
    first = True
    with table.transaction() as txn:
        for batch in pf.iter_batches(batch_size=batch_rows):
            arrow = pa.Table.from_batches([batch], schema=arrow_schema)
            if first:
                txn.overwrite(arrow)  # AlwaysTrue -> replaces all existing data
                first = False
            else:
                txn.append(arrow)
            total += arrow.num_rows
    return total
