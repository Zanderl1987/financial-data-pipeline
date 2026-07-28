"""
Shared storage helper — Hive-partitioned Parquet writes.

All pipelines call write_partitioned() instead of df.to_parquet() directly.
Files land at: output_dir/year=YYYY/month=MM/filename
DuckDB reads these back with hive_partitioning=True (see query.py).
"""

import glob as _glob
import os
import datetime

import pandas as pd


def write_partitioned(df: pd.DataFrame, output_dir: str, filename: str) -> str:
    """
    Write df to a Hive-partitioned directory and return the full output path.

    Partition key: fetched_at column (ISO timestamp) if present, else UTC now.
    Creates: output_dir/year=YYYY/month=MM/filename
    """
    if "fetched_at" in df.columns:
        ts = pd.to_datetime(df["fetched_at"], errors="coerce", utc=True).max()
    else:
        ts = pd.NaT

    if pd.isna(ts):
        now = datetime.datetime.utcnow()
        year, month = now.year, now.month
    else:
        year, month = int(ts.year), int(ts.month)

    partition_dir = os.path.join(output_dir, f"year={year}", f"month={month:02d}")
    os.makedirs(partition_dir, exist_ok=True)

    filepath = os.path.join(partition_dir, filename)
    df.to_parquet(filepath, index=False, compression="snappy")
    return filepath


def find_parquet_files(directory: str) -> list[str]:
    """
    Return all .parquet files under directory (recursive), sorted OLDEST to
    NEWEST by modification time.

    Filenames aren't a reliable date proxy -- see validate.py's _latest_file()
    for the same bug this mirrors. Sort by mtime so any future "pick the
    latest file" caller doesn't inherit that trap.
    """
    pattern = os.path.join(directory, "**", "*.parquet")
    files = _glob.glob(pattern, recursive=True)
    return sorted(files, key=os.path.getmtime)
