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
import subprocess
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

    def test_fail_when_upload_returns_zero_tables(self, monkeypatch):
        """
        upload_huggingface.main() has its own empty-folder guard (returns None),
        but this is a second, independent guard here in case that guard is ever
        bypassed or changed -- a stats dict with tables=0/files=[] must also FAIL,
        never PASS vacuously (missing = [] when stats["files"] == []).
        """
        monkeypatch.setenv("HF_TOKEN", "fake-token")
        empty_stats = {
            "repo_id": "ZanderL1337/financial-data-pipeline",
            "tables": 0,
            "rows": 0,
            "size_mb": 0.0,
            "files": [],
        }
        _patch_upload(monkeypatch, stats=empty_stats)
        _patch_hf_api(monkeypatch, remote_files=[])

        result = run_all.sync_huggingface(
            has_new_data=True, compact_enabled=True, dry_run=False, hf_sync_enabled=True,
        )
        assert result.status == "FAIL"
        assert "refusing to publish" in result.note

    def test_fail_note_truncates_with_count_when_many_files_missing(self, monkeypatch):
        stats = {
            "repo_id": "ZanderL1337/financial-data-pipeline",
            "tables": 7,
            "rows": 100,
            "size_mb": 1.0,
            "files": [f"table{i}/table{i}.parquet" for i in range(7)],
        }
        monkeypatch.setenv("HF_TOKEN", "fake-token")
        _patch_upload(monkeypatch, stats=stats)
        _patch_hf_api(monkeypatch, remote_files=[])  # none present remotely -- all 7 missing

        result = run_all.sync_huggingface(
            has_new_data=True, compact_enabled=True, dry_run=False, hf_sync_enabled=True,
        )
        assert result.status == "FAIL"
        assert "(+2 more)" in result.note


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


class TestCliWiringInProcess:
    """
    Both TestCliWiring tests above use --dry-run, which makes sync_huggingface()
    skip before hf_sync_enabled is ever consulted (see the `if dry_run:` guard
    at the top of sync_huggingface). That means an inverted bug at the call
    site in main() -- e.g. `hf_sync_enabled=args.no_hf_sync` instead of
    `not args.no_hf_sync` -- would pass the entire suite undetected.

    These tests monkeypatch run_all.sync_huggingface with a recorder and call
    run_all.main() in-process (sys.argv patched, --dry-run --stage 1 so no real
    pipeline subprocess executes) to assert on the actual keyword arguments the
    call site passes.
    """

    def _install_recorder(self, monkeypatch, captured, *, status="SKIP"):
        def _fake_sync_huggingface(*, has_new_data, compact_enabled, dry_run, hf_sync_enabled):
            captured["has_new_data"] = has_new_data
            captured["compact_enabled"] = compact_enabled
            captured["dry_run"] = dry_run
            captured["hf_sync_enabled"] = hf_sync_enabled
            return run_all.RunResult("hf_sync", status, 0.0, "faked by test recorder")

        monkeypatch.setattr(run_all, "sync_huggingface", _fake_sync_huggingface)

    def test_no_hf_sync_flag_passes_hf_sync_enabled_false(self, monkeypatch):
        captured = {}
        self._install_recorder(monkeypatch, captured)
        monkeypatch.setattr(
            sys, "argv",
            ["run_all.py", "--dry-run", "--stage", "1", "--no-hf-sync"],
        )

        rc = run_all.main()

        assert rc == 0
        assert captured["hf_sync_enabled"] is False
        assert captured["dry_run"] is True
        # compact_enabled/has_new_data are captured to prove the call site
        # passes all four arguments, not just hf_sync_enabled.
        assert captured["compact_enabled"] is True   # --no-compact not set
        assert "has_new_data" in captured

    def test_without_no_hf_sync_flag_passes_hf_sync_enabled_true(self, monkeypatch):
        captured = {}
        self._install_recorder(monkeypatch, captured)
        monkeypatch.setattr(sys, "argv", ["run_all.py", "--dry-run", "--stage", "1"])

        rc = run_all.main()

        assert rc == 0
        assert captured["hf_sync_enabled"] is True
        assert captured["dry_run"] is True
        assert captured["compact_enabled"] is True
        assert "has_new_data" in captured

    def test_hf_sync_fail_does_not_flip_overall_exit_code(self, monkeypatch):
        """
        Fix 2: hf_sync is an HF-side concern (rate limit, transient network
        error, expired token) and must not flip run_all.py's overall exit code
        -- that's what the daily accumulator scheduled task treats as "the
        whole data-collection run failed" (see AUTOMATION.md). Force hf_sync to
        FAIL while every pipeline result is DRY RUN (an accepted status) and
        confirm the process still exits 0.
        """
        captured = {}
        self._install_recorder(monkeypatch, captured, status="FAIL")
        monkeypatch.setattr(sys, "argv", ["run_all.py", "--dry-run", "--stage", "1"])

        rc = run_all.main()

        assert rc == 0
