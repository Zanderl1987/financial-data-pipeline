# HF Sync Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically sync the curated dataset to HuggingFace at the end of every `run_all.py` run, verify the upload actually landed, and surface the result in the existing run summary — with zero new duplicate-detection logic (the existing `curated.dedup()` guarantee is reused, not rebuilt).

**Architecture:** `upload_huggingface.py`'s `main()` gains a return value (stats dict instead of `None`). A new `sync_huggingface()` function in `run_all.py` decides whether to sync (dry-run / flag / compact-disabled / no-new-data / missing-token gates), calls `upload_huggingface.main()`, then verifies via `HfApi().list_repo_files()` that every local curated table made it into the remote file listing. The result becomes an ordinary `RunResult` appended to `run_all.py`'s existing results list, so it shows up in the printed summary table exactly like a pipeline.

**Tech Stack:** Python, `huggingface_hub` (`HfApi`), `pytest` with `monkeypatch` for test doubles (no real network calls in tests).

## Global Constraints

- Python interpreter for all commands: `C:\ProgramData\anaconda3\python.exe` (bare `python` is a broken MS Store stub on this machine).
- Run all commands from repo root: `C:\Users\zande\PycharmProjects\financial-data-pipeline`.
- ASCII-only in any CLI print output (Windows cp1252 terminal crashes on non-ASCII characters like ═ ▶ ✓).
- Never let the HF sync step raise an exception that crashes the whole `run_all.py` run — always resolve to a `RunResult`.
- Do not modify `curated.dedup()` or its natural-key registry (`curated.KEYS`) — duplicate safety is already correct and tested in `tests/test_curated.py`; this plan only carries that guarantee forward to HF, it doesn't rebuild it.

---

### Task 1: `upload_huggingface.main()` returns sync stats

**Files:**
- Modify: `upload_huggingface.py:206-207` (end of `main()`)
- Test: `tests/test_upload_huggingface.py` (new)

**Interfaces:**
- Consumes: nothing new — same `STORAGE_ROOT`, `HfApi`, `login` module-level names already in `upload_huggingface.py`.
- Produces: `upload_huggingface.main(repo_name: str = "financial-data-pipeline", private: bool = False) -> dict | None`. Returns `None` only when no token is set (existing early-return branch at line 107-109, unchanged). On success returns:
  ```python
  {
      "repo_id": str,        # e.g. "ZanderL1337/financial-data-pipeline"
      "tables": int,         # number of parquet files uploaded
      "rows": int,           # total row count across all tables
      "size_mb": float,      # total size in MB
      "files": list[str],    # relative paths (forward-slash, relative to STORAGE_ROOT),
                             # e.g. "prices/prices.parquet" -- these are the paths
                             # Task 2 will check for in the remote file listing
  }
  ```
  Task 2 depends on exactly these five keys.

- [ ] **Step 1: Write the failing test**

Create `tests/test_upload_huggingface.py`:

```python
"""
test_upload_huggingface.py — upload_huggingface.main() returns sync stats
that later steps (the HF sync verification in run_all.py) depend on.

No real network calls: HfApi/login are replaced with no-op doubles.
"""

import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import upload_huggingface


class _FakeApi:
    def __init__(self, *args, **kwargs):
        pass

    def create_repo(self, *args, **kwargs):
        pass

    def upload_folder(self, *args, **kwargs):
        pass


def _write_fake_table(root, table_name: str, df: pd.DataFrame) -> None:
    table_dir = root / table_name
    table_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(table_dir / f"{table_name}.parquet")


def test_main_returns_stats_dict(tmp_path, monkeypatch):
    _write_fake_table(tmp_path, "prices", pd.DataFrame({
        "symbol": ["AAPL", "MSFT"], "close": [1.0, 2.0],
    }))
    _write_fake_table(tmp_path, "macro", pd.DataFrame({
        "series_id": ["GDP"], "value": [1.0],
    }))

    monkeypatch.setattr(upload_huggingface, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(upload_huggingface, "HfApi", _FakeApi)
    monkeypatch.setattr(upload_huggingface, "login", lambda token: None)
    monkeypatch.setenv("HF_TOKEN", "fake-token-for-test")

    result = upload_huggingface.main(repo_name="test-repo", private=True)

    assert result["repo_id"] == "ZanderL1337/test-repo"
    assert result["tables"] == 2
    assert result["rows"] == 3  # 2 prices rows + 1 macro row
    assert result["size_mb"] > 0
    assert set(result["files"]) == {"prices/prices.parquet", "macro/macro.parquet"}


def test_main_returns_none_without_token(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_huggingface, "STORAGE_ROOT", tmp_path)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)

    result = upload_huggingface.main()

    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_upload_huggingface.py -v`
Expected: FAIL on `test_main_returns_stats_dict` with `TypeError: 'NoneType' object is not subscriptable` (since `main()` currently returns `None`).

- [ ] **Step 3: Add the return statement**

In `upload_huggingface.py`, after the existing lines:
```python
    print(f"\nDone! Dataset: https://huggingface.co/datasets/{repo_id}")
    print(f"  Load with: ds = load_dataset('{repo_id}')")
```
add:
```python

    return {
        "repo_id": repo_id,
        "tables": len(parquet_files),
        "rows": total_rows,
        "size_mb": total_size_mb,
        "files": [
            str(pf.relative_to(STORAGE_ROOT)).replace(os.sep, "/")
            for pf in parquet_files
        ],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_upload_huggingface.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add upload_huggingface.py tests/test_upload_huggingface.py
git commit -m "feat: return sync stats from upload_huggingface.main()"
```

---

### Task 2: `sync_huggingface()` decision + verification logic in `run_all.py`

**Files:**
- Modify: `run_all.py` — add new function right after `compact_curated()` (currently ending at `run_all.py:1055`, right before the `# ── Entry point ──` comment at `run_all.py:1057`)
- Test: `tests/test_run_all.py` (new)

**Interfaces:**
- Consumes: `upload_huggingface.main() -> dict | None` (Task 1's return contract — keys `repo_id`, `tables`, `rows`, `size_mb`, `files`), `RunResult` dataclass already defined at `run_all.py:889-895` (`name: str, status: str, duration: float, note: str, val_warnings: int = 0`), `huggingface_hub.HfApi().list_repo_files(repo_id, repo_type="dataset") -> list[str]`.
- Produces: `sync_huggingface(has_new_data: bool, compact_enabled: bool, dry_run: bool, hf_sync_enabled: bool) -> RunResult`. Task 3 calls this with `has_new_data = any(r.status == "PASS" for r in results)`, `compact_enabled = compact`, `dry_run = args.dry_run`, `hf_sync_enabled = not args.no_hf_sync`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_all.py`:

```python
"""
test_run_all.py — gating and verification logic for the automated HF sync
step (sync_huggingface). No real network calls: upload_huggingface.main and
huggingface_hub.HfApi are both replaced with test doubles via monkeypatch,
injected through sys.modules / attribute patching so run_all's *lazy*
imports (`import upload_huggingface`, `from huggingface_hub import HfApi`,
done inside the function to avoid a hard dependency at run_all import time)
pick them up.
"""

import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import run_all

STATS = {
    "repo_id": "ZanderL1337/financial-data-pipeline",
    "tables": 2,
    "rows": 100,
    "size_mb": 1.0,
    "files": ["prices/prices.parquet", "macro/macro.parquet"],
}


class _FakeHfApi:
    def __init__(self, remote_files):
        self._remote_files = remote_files

    def list_repo_files(self, repo_id, repo_type="dataset"):
        return self._remote_files


def _patch_upload(monkeypatch, stats=None, raises=None):
    def _main():
        if raises:
            raise raises
        return stats

    monkeypatch.setitem(sys.modules, "upload_huggingface", types.SimpleNamespace(main=_main))


def _patch_hf_api(monkeypatch, remote_files):
    monkeypatch.setattr("huggingface_hub.HfApi", lambda: _FakeHfApi(remote_files))


class TestGating:
    """Each precondition should SKIP the sync without calling upload at all."""

    def test_dry_run_skips(self, monkeypatch):
        result = run_all.sync_huggingface(
            has_new_data=True, compact_enabled=True, dry_run=True, hf_sync_enabled=True,
        )
        assert result.status == "SKIP"
        assert "dry run" in result.note

    def test_no_hf_sync_flag_skips(self, monkeypatch):
        result = run_all.sync_huggingface(
            has_new_data=True, compact_enabled=True, dry_run=False, hf_sync_enabled=False,
        )
        assert result.status == "SKIP"
        assert "--no-hf-sync" in result.note

    def test_no_compact_skips(self, monkeypatch):
        result = run_all.sync_huggingface(
            has_new_data=True, compact_enabled=False, dry_run=False, hf_sync_enabled=True,
        )
        assert result.status == "SKIP"
        assert "--no-compact" in result.note

    def test_no_new_data_skips(self, monkeypatch):
        result = run_all.sync_huggingface(
            has_new_data=False, compact_enabled=True, dry_run=False, hf_sync_enabled=True,
        )
        assert result.status == "SKIP"
        assert "nothing new" in result.note

    def test_missing_token_skips(self, monkeypatch):
        monkeypatch.delenv("HF_TOKEN", raising=False)
        monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
        result = run_all.sync_huggingface(
            has_new_data=True, compact_enabled=True, dry_run=False, hf_sync_enabled=True,
        )
        assert result.status == "SKIP"
        assert "HF_TOKEN" in result.note


class TestUploadAndVerify:
    def test_pass_when_all_files_present_remotely(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "fake-token")
        _patch_upload(monkeypatch, stats=STATS)
        _patch_hf_api(monkeypatch, remote_files=STATS["files"])

        result = run_all.sync_huggingface(
            has_new_data=True, compact_enabled=True, dry_run=False, hf_sync_enabled=True,
        )
        assert result.status == "PASS"

    def test_fail_when_file_missing_remotely(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "fake-token")
        _patch_upload(monkeypatch, stats=STATS)
        _patch_hf_api(monkeypatch, remote_files=["prices/prices.parquet"])  # macro missing

        result = run_all.sync_huggingface(
            has_new_data=True, compact_enabled=True, dry_run=False, hf_sync_enabled=True,
        )
        assert result.status == "FAIL"
        assert "macro/macro.parquet" in result.note

    def test_fail_when_upload_raises(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "fake-token")
        _patch_upload(monkeypatch, raises=RuntimeError("network down"))
        _patch_hf_api(monkeypatch, remote_files=[])

        result = run_all.sync_huggingface(
            has_new_data=True, compact_enabled=True, dry_run=False, hf_sync_enabled=True,
        )
        assert result.status == "FAIL"
        assert "network down" in result.note

    def test_fail_when_upload_returns_none(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "fake-token")
        _patch_upload(monkeypatch, stats=None)  # e.g. token vanished between check and call
        _patch_hf_api(monkeypatch, remote_files=[])

        result = run_all.sync_huggingface(
            has_new_data=True, compact_enabled=True, dry_run=False, hf_sync_enabled=True,
        )
        assert result.status == "FAIL"
        assert "no stats" in result.note
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_run_all.py -v`
Expected: FAIL with `AttributeError: module 'run_all' has no attribute 'sync_huggingface'` (function doesn't exist yet).

- [ ] **Step 3: Implement `sync_huggingface()`**

In `run_all.py`, insert the following function immediately after `compact_curated()` (i.e. right before the `# ── Entry point ──` comment currently at line 1057):

```python
def sync_huggingface(
    has_new_data: bool,
    compact_enabled: bool,
    dry_run: bool,
    hf_sync_enabled: bool,
) -> RunResult:
    """
    Push the recompacted curated snapshot to the public HuggingFace dataset
    and verify the upload actually landed remotely.

    Only ever meaningful when curated data was just recompacted this run
    (compact_enabled) -- that ordering is what keeps this safe from
    publishing stale data, on top of curated.dedup()'s own key-uniqueness
    guarantee (see tests/test_curated.py). This function adds no new
    duplicate-detection logic of its own.
    """
    if dry_run:
        return RunResult("hf_sync", "SKIP", 0.0, "dry run, skipping sync")
    if not hf_sync_enabled:
        return RunResult("hf_sync", "SKIP", 0.0, "--no-hf-sync set, skipping sync")
    if not compact_enabled:
        return RunResult("hf_sync", "SKIP", 0.0, "--no-compact set, skipping sync")
    if not has_new_data:
        return RunResult("hf_sync", "SKIP", 0.0, "no pipeline passed, nothing new to sync")
    if not (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")):
        return RunResult("hf_sync", "SKIP", 0.0, "no HF_TOKEN/HUGGINGFACE_TOKEN set")

    start = time.time()
    try:
        import upload_huggingface
        from huggingface_hub import HfApi

        stats = upload_huggingface.main()
        if stats is None:
            return RunResult(
                "hf_sync", "FAIL", time.time() - start,
                "upload_huggingface.main() returned no stats",
            )

        remote_files = set(HfApi().list_repo_files(stats["repo_id"], repo_type="dataset"))
        missing = sorted(f for f in stats["files"] if f not in remote_files)
        duration = time.time() - start

        if missing:
            note = f"{len(missing)} table(s) missing remotely: {', '.join(missing[:5])}"
            return RunResult("hf_sync", "FAIL", duration, note)

        print("\n-- HuggingFace Sync --")
        print(f"  {stats['tables']} tables, {stats['rows']:,} rows, "
              f"{stats['size_mb']:.1f} MB, verified remotely.")
        return RunResult("hf_sync", "PASS", duration, "")
    except Exception as exc:  # noqa: BLE001 -- never let HF sync sink a run
        return RunResult("hf_sync", "FAIL", time.time() - start, f"sync error: {exc}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_run_all.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add run_all.py tests/test_run_all.py
git commit -m "feat: add sync_huggingface() gating and verification logic to run_all.py"
```

---

### Task 3: Wire `--no-hf-sync` flag and call site into `run_all.py main()`

**Files:**
- Modify: `run_all.py:20` (docstring usage examples), `run_all.py:1089-1092` (argparse, add flag after `--no-compact`), `run_all.py:1143-1150` (call `sync_huggingface`, append result, before `_print_summary`)
- Test: `tests/test_run_all.py` (append to file created in Task 2)

**Interfaces:**
- Consumes: `sync_huggingface(...)` from Task 2 (exact signature above), `args.no_hf_sync: bool` (new argparse field), existing `args.dry_run`, `compact`, `results`, `PIPELINES` all already in scope in `main()`.
- Produces: nothing new consumed by later tasks — this is the last task in the plan.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_run_all.py`:

```python
import subprocess


class TestCliWiring:
    def test_no_hf_sync_flag_is_recognized(self):
        """
        --dry-run makes every pipeline a no-op (see run_pipeline's dry_run
        branch, run_all.py:935-938) so this exercises the real argparse +
        main() wiring end-to-end with no network calls and no subprocess
        pipeline execution.
        """
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "run_all.py"),
             "--dry-run", "--stage", "1", "--no-hf-sync"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "hf_sync" in result.stdout

    def test_dry_run_shows_hf_sync_skip_reason(self):
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "run_all.py"),
             "--dry-run", "--stage", "1"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "hf_sync" in result.stdout
        assert "dry run" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_run_all.py::TestCliWiring -v`
Expected: FAIL — `--no-hf-sync` is an unrecognized argument (argparse `error: unrecognized arguments: --no-hf-sync`) and/or "hf_sync" never appears in stdout since the call site doesn't exist yet.

- [ ] **Step 3: Add the docstring line, argparse flag, and call site**

In `run_all.py`, update the module docstring (around line 20) by adding a line after the existing `--no-compact` line:
```python
  python run_all.py --no-hf-sync           # skip post-run HuggingFace dataset sync
```

Add the argparse argument right after the existing `--no-compact` block (`run_all.py:1089-1092`):
```python
    parser.add_argument(
        "--no-hf-sync", action="store_true",
        help="Skip post-run HuggingFace dataset sync.",
    )
```

Replace the existing block:
```python
    if compact and not args.dry_run:
        spec_by_name = {p.name: p for p in PIPELINES}
        passed_specs = [spec_by_name[r.name] for r in results if r.status == "PASS"]
        compact_curated(passed_specs)

    _print_summary(results, args.backfill, start_time)
```
with:
```python
    if compact and not args.dry_run:
        spec_by_name = {p.name: p for p in PIPELINES}
        passed_specs = [spec_by_name[r.name] for r in results if r.status == "PASS"]
        compact_curated(passed_specs)

    has_new_data = any(r.status == "PASS" for r in results)
    hf_result = sync_huggingface(
        has_new_data=has_new_data,
        compact_enabled=compact,
        dry_run=args.dry_run,
        hf_sync_enabled=not args.no_hf_sync,
    )
    results.append(hf_result)

    _print_summary(results, args.backfill, start_time)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/test_run_all.py -v`
Expected: all tests in the file PASS (5 gating + 4 upload/verify + 2 CLI wiring = 11 tests).

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `C:\ProgramData\anaconda3\python.exe -m pytest tests/ -v`
Expected: all tests PASS (previous suite count plus the 11 new tests from Tasks 1-3 across `tests/test_upload_huggingface.py` and `tests/test_run_all.py`).

- [ ] **Step 6: Commit**

```bash
git add run_all.py tests/test_run_all.py
git commit -m "feat: wire automated HF sync into run_all.py's --no-hf-sync flag and main()"
```

---

## Manual verification (not automated — requires the real HF_TOKEN already in `.env`)

After all three tasks are committed, do one real run to confirm the live wiring works end-to-end against the actual HuggingFace repo:

```
C:\ProgramData\anaconda3\python.exe run_all.py --only commodity_macro
```

Expected: the run summary's last row is `hf_sync` with status `PASS` and no note (or `SKIP`/`FAIL` with a clear reason if a gate tripped — e.g. `SKIP` if `commodity_macro` happened not to PASS). Confirm on https://huggingface.co/datasets/ZanderL1337/financial-data-pipeline that the commit timestamp updated.
