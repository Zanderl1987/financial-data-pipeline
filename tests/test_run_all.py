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
