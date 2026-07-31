"""Shared Iceberg table maintenance helpers.

Root-level utility, same tier as storage_utils.py / logging_utils.py.
"""

import datetime
import logging


def expire_old_snapshots(table, retain_days: int = 30, log: logging.Logger | None = None) -> int:
    """Expire snapshots older than retain_days, always keeping the current snapshot.

    This trims the logical snapshot history recorded in the table's metadata.json
    (bounds read overhead and the length of the snapshot log). pyiceberg's Python
    client (0.11.1) does not delete the now-orphaned manifest/data files from disk
    on expiry -- there is no GC/remove-orphan-files support yet -- so this does not
    shrink on-disk file count, only future snapshot-log growth.
    """
    table.refresh()
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=retain_days)
    before = len(list(table.snapshots()))
    table.maintenance.expire_snapshots().older_than(cutoff).commit()
    table.refresh()
    after = len(list(table.snapshots()))
    expired = before - after
    if log and expired:
        log.info(
            "[Iceberg] Expired %d snapshots older than %d days (%d -> %d)",
            expired, retain_days, before, after,
        )
    return expired
