"""
storage_monitor.py — Storage size monitoring for the Parquet data store.

Tracks total size, per-table size, and file counts.  Generates
JSON reports that can be consumed by dashboards or alerting.

Usage (CLI):
    python storage_monitor.py            # print top-10 tables by size
    python storage_monitor.py --json     # dump full stats as JSON
    python storage_monitor.py --save     # write storage_report.json

Usage (API):
    from storage_monitor import get_storage_stats, save_stats_report

    stats = get_storage_stats()
    print(stats["total_size_mb"])
"""

import datetime
import glob
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
STORAGE_RAW = os.path.join(REPO_ROOT, "storage", "raw")


def get_storage_stats(storage_root: str | None = None) -> dict:
    """Compute storage statistics for all Parquet directories.

    Args:
        storage_root: Override the default ``storage/raw`` path.

    Returns:
        Dict with ``total_size_mb``, ``total_files``, ``tables`` (per-table
        breakdown), and ``timestamp``.
    """
    root = storage_root or STORAGE_RAW
    tables: dict[str, dict] = {}
    total_size = 0
    total_files = 0

    if not os.path.isdir(root):
        return {
            "total_size_mb": 0,
            "total_files": 0,
            "tables": tables,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    for entry in sorted(os.listdir(root)):
        entry_path = os.path.join(root, entry)
        if not os.path.isdir(entry_path):
            continue

        parquet_files = glob.glob(
            os.path.join(entry_path, "**", "*.parquet"), recursive=True
        )
        size = sum(os.path.getsize(f) for f in parquet_files)

        tables[entry] = {
            "files": len(parquet_files),
            "size_bytes": size,
            "size_mb": round(size / 1_048_576, 2),
        }
        total_size += size
        total_files += len(parquet_files)

    return {
        "total_size_mb": round(total_size / 1_048_576, 2),
        "total_files": total_files,
        "tables": tables,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def save_stats_report(path: str | None = None) -> str:
    """Save storage stats to a JSON file and return the file path.

    Args:
        path: Override destination; defaults to ``storage/storage_report.json``.

    Returns:
        Absolute path of the written report.
    """
    stats = get_storage_stats()
    dest = path or os.path.join(REPO_ROOT, "storage", "storage_report.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    return dest


def print_summary(stats: dict | None = None) -> None:
    """Print a human-readable summary to stdout."""
    if stats is None:
        stats = get_storage_stats()

    print(f"Total: {stats['total_size_mb']:.1f} MB across {stats['total_files']} files")
    ranked = sorted(
        stats["tables"].items(),
        key=lambda item: item[1]["size_bytes"],
        reverse=True,
    )
    for name, info in ranked[:10]:
        print(f"  {name:30s}  {info['size_mb']:8.1f} MB  ({info['files']} files)")


if __name__ == "__main__":
    stats = get_storage_stats()
    if "--json" in sys.argv:
        print(json.dumps(stats, indent=2))
    elif "--save" in sys.argv:
        path = save_stats_report()
        print(f"Report saved to {path}")
    else:
        print_summary(stats)
