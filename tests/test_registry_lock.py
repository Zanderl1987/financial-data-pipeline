"""
Tests for the registry's cross-process append lock (added 2026-08-30 so two
concurrent Stage-3 batches can write the same results.parquet without losing
each other's rows).

append() is read-modify-write: load the whole file, concat, os.replace. Two
writers racing can each read the pre-append file and clobber the other's rows.
The lockfile (O_CREAT|O_EXCL) serializes them; single-writer callers hold it
for microseconds and see no contention. These tests spawn real subprocesses
that call registry.append() directly to prove the race is closed.
"""

import json
import os
import subprocess
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation import registry as ev_registry  # noqa: E402

_APPENDER = r"""
import json, os, sys
import pandas as pd
sys.path.insert(0, {root!r})
from evaluation import registry as ev_registry
path = {path!r}
rows = ev_registry.load(path)
if rows.empty:
    existing = []
else:
    existing = json.loads(rows[["input_name", "statistic"]].to_json(orient="records"))
counter = {counter!r}
header = {header!r}
new = []
for i in range({n!r}):
    new.append({{
        "run_id": counter, "input_name": f"pine_writer{{i}}",
        "input_type": "trade_rule", "evaluation": "registry_lock_test",
        "horizon": -1, "statistic": "pnl_p", "value": 0.05,
        "n": 1, "universe_hash": "locktest", "date_range": "x",
        "created_at": "2026-01-01T00:00:00", "execution_hash": header,
    }})
ev_registry.append(pd.DataFrame(new), path=path)
print(f"appended {{len(new)}}", flush=True)
"""


def _rows_by_writer(path):
    df = ev_registry.load(path)
    if df.empty:
        return {}
    sub = df[df["evaluation"] == "registry_lock_test"]
    sub["writer"] = sub["input_name"].str.extract(r"writer(\d+)").iloc[:, 0].astype(int)
    return {int(i): g for i, g in sub.groupby("writer")}


def test_serial_appends_accumulate():
    """Two sequential appends must both survive (a row lost here is a plain
    append bug, independent of concurrency)."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "results.parquet")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        py = _APPENDER.format(root=root, path=path, counter="seq-a", header="h1", n=5)
        subprocess.run([sys.executable, "-c", py], check=True, capture_output=True)
        subprocess.run([sys.executable, "-c", py], check=True, capture_output=True)
        rows = _rows_by_writer(path)
        assert sum(len(g) for g in rows.values()) == 10


def test_parallel_appenders_lose_no_rows():
    """Two subprocesses appending at once (no coordination, locks in the way)
    must each keep their full row set -- this is the race that lost rows
    before the lock."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "results.parquet")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        procs = []
        for c in ("par-a", "par-b"):
            py = _APPENDER.format(root=root, path=path, counter=c, header="h1", n=30)
            procs.append(subprocess.Popen([sys.executable, "-c", py],
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE))
        out = [p.communicate() for p in procs]
        for p, (so, se) in zip(procs, out):
            assert p.returncode == 0, se.decode()
        rows = _rows_by_writer(path)
        assert sum(len(g) for g in rows.values()) == 60, \
            f"lost rows under concurrency: {len(rows)} writers, " \
            f"{sum(len(g) for g in rows.values())}/60 present"


def test_append_still_writes_after_lock_holder_exits():
    """Lockfile from a completed writer must be cleaned up so later appends
    are unblocked (no stale lock accumicates between batches)."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "results.parquet")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        py = _APPENDER.format(root=root, path=path, counter="once", header="h1", n=2)
        subprocess.run([sys.executable, "-c", py], check=True, capture_output=True)
        assert not os.path.exists(ev_registry._lock_path(path))