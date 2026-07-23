"""Create the shipping.* Iceberg tables with Snappy compression."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import (
    NestedField,
    StringType,
    DoubleType,
    DateType,
    TimestamptzType,
)

STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = STORAGE_ROOT / "iceberg"
CATALOG_DB = ICEBERG_WAREHOUSE / "constituents_catalog.db"

GSCPI_SCHEMA = Schema(
    NestedField(1,  "date",       DateType(),        required=True),
    NestedField(2,  "gscpi",      DoubleType(),      required=False),
    NestedField(3,  "source",     StringType(),      required=False),
    NestedField(4,  "fetched_at", TimestamptzType(), required=True),
)

FREIGHT_PPI_SCHEMA = Schema(
    NestedField(1,  "date",       DateType(),        required=True),
    NestedField(2,  "value",      DoubleType(),      required=True),
    NestedField(3,  "series_id",  StringType(),      required=True),
    NestedField(4,  "name",       StringType(),      required=False),
    NestedField(5,  "frequency",  StringType(),      required=False),
    NestedField(6,  "unit",       StringType(),      required=False),
    NestedField(7,  "fetched_at", TimestamptzType(), required=True),
)

TABLES = {
    "shipping.gscpi":       GSCPI_SCHEMA,
    "shipping.freight_ppi": FREIGHT_PPI_SCHEMA,
}


def main():
    catalog = load_catalog(
        "constituents",
        type="sql",
        uri=f"sqlite:///{CATALOG_DB.as_posix()}",
        warehouse=f"file://{ICEBERG_WAREHOUSE.as_posix()}",
    )

    try:
        catalog.create_namespace("shipping")
        print("Created namespace shipping")
    except Exception:
        print("Namespace shipping already exists")

    for identifier, schema in TABLES.items():
        try:
            catalog.load_table(identifier)
            print(f"Table {identifier} already exists. Skipping.")
        except Exception:
            catalog.create_table(
                identifier=identifier,
                schema=schema,
                properties={
                    "write.parquet.compression-codec": "snappy",
                    "format-version": "2",
                },
            )
            print(f"Created table {identifier} with Snappy compression.")


if __name__ == "__main__":
    main()
