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
