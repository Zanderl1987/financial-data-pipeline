"""Create the constituents.securities Iceberg table."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import (
    NestedField,
    StringType,
    LongType,
    DoubleType,
    BooleanType,
    TimestamptzType,
)

STORAGE_ROOT = Path(__file__).parent / "storage" / "iceberg"
CATALOG_DB = STORAGE_ROOT / "constituents_catalog.db"

SECURITIES_SCHEMA = Schema(
    NestedField(1,  "symbol",              StringType(),       required=True),
    NestedField(2,  "company_name",        StringType(),       required=False),
    NestedField(3,  "asset_type",          StringType(),       required=False),
    NestedField(4,  "sector",              StringType(),       required=False),
    NestedField(5,  "industry",            StringType(),       required=False),
    NestedField(6,  "exchange",            StringType(),       required=False),
    NestedField(7,  "currency",            StringType(),       required=False),
    NestedField(8,  "country",             StringType(),       required=False),
    NestedField(9,  "market_cap",          DoubleType(),       required=False),
    NestedField(10, "shares_outstanding",  DoubleType(),       required=False),
    NestedField(11, "ipo_date",            StringType(),       required=False),
    NestedField(12, "cik",                 LongType(),         required=False),
    NestedField(13, "is_sp500",            BooleanType(),      required=False),
    NestedField(14, "is_nasdaq100",        BooleanType(),      required=False),
    NestedField(15, "is_dji30",            BooleanType(),      required=False),
    NestedField(16, "is_russell3000",      BooleanType(),      required=False),
    NestedField(17, "is_russell2000",      BooleanType(),      required=False),
    NestedField(18, "is_wilshire5000",     BooleanType(),      required=False),
    NestedField(19, "primary_source",      StringType(),       required=False),
    NestedField(20, "last_refreshed",      TimestamptzType(),  required=True),
)


def main():
    catalog = load_catalog(
        "constituents",
        type="sql",
        uri=f"sqlite:///{CATALOG_DB.as_posix()}",
        warehouse=f"file://{STORAGE_ROOT.as_posix()}",
    )

    identifier = "constituents.securities"
    try:
        catalog.load_table(identifier)
        print(f"Table {identifier} already exists. Skipping creation.")
        return
    except Exception:
        pass

    catalog.create_table(
        identifier=identifier,
        schema=SECURITIES_SCHEMA,
        properties={
            "write.parquet.compression-codec": "snappy",
            "format-version": "2",
        },
    )
    print(f"Created table {identifier}")


if __name__ == "__main__":
    main()
