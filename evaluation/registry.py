"""
evaluation/registry.py -- append-only parquet store of evaluation results.

One row per (run, evaluation, horizon, statistic). horizon=-1 means "no
horizon" (portfolio- or trade-level statistics). The registry is the memory
of every signal ever evaluated: baselines() answers "what did this signal
score last time", population() answers "how many trials has this research
program run" (the honest N for deflated Sharpe).

NOTE: no `year`/`month` columns ever (Hive partition shadowing) -- the date
range lives in the `date_range` string.
"""

import hashlib
import os
import time
import uuid

import pandas as pd

REG_PATH = os.path.join("storage", "eval_registry", "results.parquet")


class RegistryLockTimeout(RuntimeError):
    """Raised when append() cannot secure the registry's file lock within the
    timeout -- a held lock means another writer is mid-append; waiting past
    ~30s almost always means a crashed holder left a live-looking lock."""


def _pid_alive(pid: "int | None") -> bool:
    """True if process `pid` exists.

    NOT os.kill(pid, 0): on Windows that call is not a liveness probe -- it
    sends CTRL_C_EVENT to the process' console (silently killing concurrent
    writers mid-append) and raises SystemError instead of ProcessLookupError,
    so a crashed holder can never be reclaimed. Use the Win32 OpenProcess /
    GetExitCodeProcess pair instead, which is a true existence check."""

    def _win_alive(unchecked: int) -> bool:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                      wintypes.BOOL(False),
                                      wintypes.DWORD(unchecked))
        if not handle:
            # Access denied (exists but owned by another user) -- be
            # conservative and treat it as alive rather than reclaiming a
            # live lock.
            return True
        try:
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            if not ok:
                return True
            return bool(code.value == STILL_ACTIVE)
        finally:
            kernel32.CloseHandle(handle)

    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        try:
            return _win_alive(pid)
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user.
        return True
    return True


def _lock_path(path: str) -> str:
    return path + ".lock"


def _acquire_lock(path: str, timeout: float = 60.0):
    """Cross-process exclusive lock for the registry file, via an
    O_CREAT|O_EXCL lockfile. append() is read-modify-write (load whole
    results.parquet, concat, os.replace) so two concurrent writers can lose
    each other's rows; the lock serializes them.

    Stale-lock recovery: if the lockfile names a PID that no longer exists,
    the lock is reclaimed. A live-crashed holder (PID reused, or a file the
    OS refuses to tell us about) is surfaced as RegistryLockTimeout rather
    than silently corrupting the registry.
    """
    lock = _lock_path(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}\n".encode("ascii"))
            return fd
        except FileExistsError:
            pid = None
            try:
                with open(lock, "r", encoding="ascii") as f:
                    pid = int(f.read().split()[0])
            except (ValueError, IndexError, OSError, FileNotFoundError):
                pid = None
            if not _pid_alive(pid):
                try:
                    os.remove(lock)
                except OSError:
                    pass
                continue
            if time.time() > deadline:
                raise RegistryLockTimeout(
                    f"registry lock {lock} held by live pid {pid} for "
                    f">{timeout:.0f}s")
            time.sleep(0.25)


def _release_lock(fd, path: str) -> None:
    try:
        os.close(fd)
    finally:
        try:
            os.remove(_lock_path(path))
        except OSError:
            pass

COLUMNS = [
    "run_id", "input_name", "input_type", "evaluation", "horizon",
    "statistic", "value", "n", "universe_hash", "date_range", "created_at",
    "execution_hash",
]

_KEY = ["input_name", "evaluation", "horizon", "statistic"]

#: Rows written before `execution_hash` existed (W1 Step B, 2026-08-17). They
#: are labeled "unknown" rather than "legacy" ON PURPOSE: most predate any cost
#: model, but the tv_strategy_catalog_stage3 rows were produced net of 10 bps a
#: side via a monkeypatch, so a blanket "legacy" would be a false claim about
#: history. Anything comparing execution semantics across the registry must
#: treat "unknown" as missing data, not as a value.
UNKNOWN_EXECUTION = "unknown"


def universe_hash(symbols) -> str:
    """Order- and case-insensitive 12-hex digest of a symbol universe."""
    joined = ",".join(sorted({str(s).upper() for s in symbols}))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalize(rows: pd.DataFrame) -> pd.DataFrame:
    rows = rows.copy()
    if "execution_hash" not in rows.columns:
        # Added in W1 Step B. Defaulted rather than required so every existing
        # caller keeps working; callers that know their ExecutionConfig should
        # pass evaluation.execution.config_hash(cfg) explicitly.
        rows["execution_hash"] = UNKNOWN_EXECUTION
    missing = [c for c in COLUMNS if c not in rows.columns]
    if missing:
        raise ValueError(f"registry rows missing columns: {missing}")
    out = rows[COLUMNS].copy()
    out["horizon"] = pd.to_numeric(out["horizon"]).fillna(-1).astype("int64")
    out["value"] = pd.to_numeric(out["value"], errors="coerce").astype(float)
    out["n"] = pd.to_numeric(out["n"]).fillna(0).astype("int64")
    for col in ("run_id", "input_name", "input_type", "evaluation",
                "statistic", "universe_hash", "date_range", "created_at"):
        out[col] = out[col].astype(str)
    return out


def load(path: str = REG_PATH) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_parquet(path)
    if "execution_hash" not in df.columns:
        # Registry files written before W1 Step B. See UNKNOWN_EXECUTION.
        df["execution_hash"] = UNKNOWN_EXECUTION
    return df


def append(rows: pd.DataFrame, path: str = REG_PATH) -> int:
    """Append rows atomically (write temp, os.replace). Returns rows added.

    Serialized with a cross-process lockfile so two concurrent writers (e.g.
    two Stage-3 batches running in parallel) cannot lose each other's rows in
    the read-modify-write. Single-writer callers take and release the lock
    with no contention."""
    rows = _normalize(rows)
    fd = _acquire_lock(path)
    try:
        existing = load(path)
        combined = (pd.concat([existing, rows], ignore_index=True)
                    if not existing.empty else rows)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        combined.to_parquet(tmp, index=False)
        os.replace(tmp, path)
        return len(rows)
    finally:
        _release_lock(fd, path)


def baselines(input_name=None, path: str = REG_PATH) -> pd.DataFrame:
    """Latest row per (input_name, evaluation, horizon, statistic)."""
    reg = load(path)
    if reg.empty:
        return reg
    if input_name is not None:
        reg = reg[reg["input_name"] == input_name]
    if reg.empty:
        return reg
    return (reg.sort_values("created_at")
               .groupby(_KEY, as_index=False)
               .tail(1)
               .reset_index(drop=True))


def compare(rows: pd.DataFrame, path: str = REG_PATH, tol: float = 0.005,
            allow_universe_mismatch: bool = False) -> pd.DataFrame:
    """
    Compare fresh rows against stored baselines on the same key. Refuses to
    compare across different universes (a coverage difference masquerades as
    a skill difference) unless allow_universe_mismatch=True.
    """
    rows = _normalize(rows)
    base = baselines(path=path)
    if base.empty:
        out = rows.copy()
        out["baseline"] = float("nan")
        out["diff"] = float("nan")
        out["within_tol"] = False
        return out
    b = base[_KEY + ["value", "universe_hash"]].rename(
        columns={"value": "baseline", "universe_hash": "baseline_universe_hash"})
    out = rows.merge(b, on=_KEY, how="left")
    matched = out["baseline_universe_hash"].notna()
    mismatch = matched & (out["universe_hash"] != out["baseline_universe_hash"])
    if mismatch.any() and not allow_universe_mismatch:
        bad = out.loc[mismatch, _KEY].to_dict("records")[:3]
        raise ValueError(
            f"universe_hash mismatch vs baseline for {bad} -- results are not "
            "comparable across universes; pass allow_universe_mismatch=True "
            "to override")
    out["diff"] = out["value"] - out["baseline"]
    out["within_tol"] = out["diff"].abs() <= tol
    out.loc[out["baseline"].isna(), "within_tol"] = False
    return out.drop(columns=["baseline_universe_hash"])


def population(statistic: str, path: str = REG_PATH,
               exclude_input_name=None) -> list:
    """Latest value per input_name for one statistic (deflated-Sharpe trials).

    exclude_input_name, if given, drops that input_name's own rows before
    taking the latest-per-name population. Callers who are about to append
    their own just-computed value to this population (e.g. DSR trials) pass
    their own name here -- otherwise a re-run of an already-registered
    signal double-counts its own prior entry (stale value + current value).
    """
    reg = load(path)
    if reg.empty:
        return []
    sub = reg[(reg["statistic"] == statistic) & reg["value"].notna()]
    if exclude_input_name is not None:
        sub = sub[sub["input_name"] != exclude_input_name]
    if sub.empty:
        return []
    latest = (sub.sort_values("created_at")
                 .groupby("input_name", as_index=False)
                 .tail(1))
    return [float(v) for v in latest["value"]]


def summary(path: str = REG_PATH) -> str:
    """One-screen ASCII summary (the registry's CLI export)."""
    reg = load(path)
    if reg.empty:
        return f"registry empty ({path})"
    lines = [f"{len(reg)} rows, {reg['input_name'].nunique()} inputs, "
             f"{reg['run_id'].nunique()} runs ({path})"]
    per = (reg.groupby("input_name")
              .agg(rows=("run_id", "size"), runs=("run_id", "nunique"),
                   latest=("created_at", "max")))
    for name, r in per.iterrows():
        lines.append(f"  {name}: {r['rows']} rows, {r['runs']} runs, "
                     f"latest {str(r['latest'])[:19]}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(summary(sys.argv[1] if len(sys.argv) > 1 else REG_PATH))
