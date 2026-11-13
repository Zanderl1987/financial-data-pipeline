"""
test_hf_license_guard.py -- the publisher must refuse non-redistributable data.

CRSP, Compustat and IBES prohibit redistribution. This repo publishes
`storage/curated/` to a PUBLIC HuggingFace dataset under CC BY 4.0 while its
README asserts "data sourced from public APIs and government databases". So a
licensed table landing in the published tree is a licence breach and a false
licence statement, and the guard has to hold for the whole snapshot.

`--private` is not sufficient cover and the tests below encode why: a private
dataset repo is still redistribution to a third-party host, and this repo has
already shipped a real upload to a public repo by accident once (the
create_repo(exist_ok=True) no-op documented in upload_huggingface.py).

No network: HfApi/login are no-op doubles and the guard must fire before either
is reached.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import upload_huggingface as hf


class TestRestrictedReason:
    @pytest.mark.parametrize(
        "name",
        [
            "crsp_daily",
            "crsp_msenames",
            "crsp_ccm",
            "comp_fundamentals_quarterly",
            "compustat_securities",
            "comp_g_link",
            "comp_gdxr",
            "ibesfn_p",
            "ibessub_p",
            "msenames",
            "wrds_manifest",
        ],
    )
    def test_licensed_names_are_refused(self, name):
        assert hf.restricted_reason(Path(f"storage/curated/{name}.parquet"))

    @pytest.mark.parametrize(
        "name",
        [
            "prices",
            "yfinance_universe_prices",
            "tiingo_prices",
            "policy_rates",
            "fundamentals_quarterly",
            "options_history",
            "congressional_trades",
            "cboe_strategy_indices",
        ],
    )
    def test_public_pipeline_names_are_allowed(self, name):
        """A false positive costs a rename; these must not trip it."""
        assert hf.restricted_reason(Path(f"storage/curated/{name}.parquet")) is None

    def test_prefix_is_matched_at_a_word_boundary(self):
        """`comp` must catch comp_fundamentals but not `compustat`-free names
        like `comparative_sector_weights`."""
        assert hf.restricted_reason(Path("storage/curated/comp_fundamentals.parquet"))
        assert hf.restricted_reason(Path("storage/curated/composition_weights.parquet")) is None

    def test_a_licensed_root_nested_under_published_is_refused_by_path(self, tmp_path):
        """The path gate is independent of the name gate.

        A licensed root mounted inside storage/curated would be swept up by the
        glob regardless of what its files are called, so the path check has to
        stand on its own.
        """
        published = tmp_path / "curated"
        licensed = published / "licensed"
        licensed.mkdir(parents=True)
        stray = licensed / "anything.parquet"
        stray.write_bytes(b"")

        original = hf.LICENSED_ROOT
        try:
            hf.LICENSED_ROOT = licensed
            reason = hf.restricted_reason(stray)
        finally:
            hf.LICENSED_ROOT = original
        assert reason and "non-redistributable" in reason


class TestPublisherRefuses:
    """`main()` must abort on a violation, and before login()."""

    @staticmethod
    def _run(tmp_path, monkeypatch, files, private=False):
        """Point the publisher at a temp tree, run main(), report what happened."""
        published = tmp_path / "curated"
        published.mkdir(parents=True, exist_ok=True)
        for name in files:
            pd.DataFrame({"x": [1, 2, 3]}).to_parquet(published / f"{name}.parquet")

        calls = {"login": 0, "upload": 0}

        def fake_login(*args, **kwargs):
            calls["login"] += 1

        class FakeApi:
            def create_repo(self, *args, **kwargs):
                pass

            def dataset_info(self, repo_id):
                return SimpleNamespace(private=private)

            def update_repo_settings(self, **kwargs):
                pass

            def upload_folder(self, *args, **kwargs):
                calls["upload"] += 1

        monkeypatch.setattr(hf, "STORAGE_ROOT", published)
        monkeypatch.setattr(hf, "login", fake_login)
        monkeypatch.setattr(hf, "HfApi", lambda *a, **k: FakeApi())
        monkeypatch.setattr(hf, "remote_row_counts", lambda *a, **k: {})
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "fake-token")

        # Skip the commit/push half of main(); this is about the gate.
        monkeypatch.setattr(hf, "count_rows", lambda p: 3)
        result = hf.main()
        return result, calls

    def test_licensed_table_aborts_the_whole_snapshot(self, tmp_path, monkeypatch, capsys):
        _result, calls = self._run(tmp_path, monkeypatch, ["prices", "crsp_daily"])
        out = capsys.readouterr().out
        assert "refusing to publish" in out
        assert "crsp_daily" in out
        assert calls["login"] == 0, "must abort before authenticating"

    def test_clean_snapshot_still_publishes(self, tmp_path, monkeypatch, capsys):
        _result, calls = self._run(tmp_path, monkeypatch, ["prices", "policy_rates"])
        assert "refusing to publish" not in capsys.readouterr().out
        assert calls["login"] == 1

    def test_abort_happens_even_with_private_true(self, tmp_path, monkeypatch, capsys):
        """--private is not an escape hatch, so it must not change the verdict."""
        _result, calls = self._run(tmp_path, monkeypatch, ["comp_gdxr"], private=True)
        assert "refusing to publish" in capsys.readouterr().out
        assert calls["login"] == 0

    def test_every_violation_is_named(self, tmp_path, monkeypatch, capsys):
        """Hiding all but one would leave the rest to be 'discovered' later."""
        self._run(tmp_path, monkeypatch, ["crsp_daily", "comp_securities", "ibesfn_p"])
        out = capsys.readouterr().out
        for name in ("crsp_daily", "comp_securities", "ibesfn_p"):
            assert name in out


class TestLicensedRootIsolation:
    def test_licensed_root_is_not_the_published_root(self):
        assert Path(hf.LICENSED_ROOT).resolve() != Path(hf.STORAGE_ROOT).resolve()

    def test_licensed_root_is_not_nested_inside_the_published_root(self):
        """Nesting would put licensed data one `curated.py` rebuild away from the
        publisher, which globs recursively."""
        published = Path(hf.STORAGE_ROOT).resolve()
        licensed = Path(hf.LICENSED_ROOT).resolve()
        assert published not in licensed.parents