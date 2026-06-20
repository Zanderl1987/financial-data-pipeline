"""
test_runner.py — smoke-tests for the unified pipeline runner.

No pipelines are actually executed. Tests verify:
  - The pipeline registry is consistent and complete
  - CLI args parse correctly (dry-run, --only, --skip, --stage)
  - Env-var skip logic works correctly
  - All registered pipeline files exist on disk
"""

import sys
import os
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import run_all as ra
from run_all import PIPELINES, PipelineSpec, _check_env, RunResult


# ── Registry integrity ────────────────────────────────────────────────────────

class TestPipelineRegistry:
    def test_all_pipelines_have_required_fields(self):
        for p in PIPELINES:
            assert p.name,  f"Pipeline missing name: {p}"
            assert p.file,  f"{p.name}: missing file"
            assert p.desc,  f"{p.name}: missing desc"
            assert p.stage in (1, 2, 3), f"{p.name}: stage must be 1, 2, or 3"
            assert p.tables, f"{p.name}: tables list is empty"
            assert p.timeout > 0, f"{p.name}: timeout must be positive"

    def test_pipeline_names_are_unique(self):
        names = [p.name for p in PIPELINES]
        dupes = [n for n in names if names.count(n) > 1]
        assert not dupes, f"Duplicate pipeline names: {dupes}"

    def test_all_pipeline_files_exist(self):
        missing = [
            p.name for p in PIPELINES
            if not os.path.exists(os.path.join(REPO_ROOT, p.file))
        ]
        assert not missing, f"Pipeline files missing on disk: {missing}"

    def test_stages_cover_1_through_3(self):
        stages = {p.stage for p in PIPELINES}
        assert stages == {1, 2, 3}, f"Expected stages 1, 2, 3 — got {sorted(stages)}"

    def test_stage_ordering_is_monotone(self):
        """Pipelines must be listed in non-decreasing stage order."""
        stages = [p.stage for p in PIPELINES]
        assert stages == sorted(stages), "PIPELINES must be ordered by stage"

    def test_all_tables_are_in_catalog(self):
        import query as q
        not_in_catalog = []
        for p in PIPELINES:
            for t in p.tables:
                if t not in q.CATALOG:
                    not_in_catalog.append((p.name, t))
        assert not not_in_catalog, (
            f"Pipeline tables not registered in CATALOG: {not_in_catalog}"
        )

    def test_derived_pipelines_in_stage_3(self):
        """synthetic_options and news_sentiment must be stage 3 (depend on prior outputs)."""
        stage3 = {p.name for p in PIPELINES if p.stage == 3}
        assert "synthetic_options" in stage3
        assert "news_sentiment" in stage3


# ── Env-var skip logic ────────────────────────────────────────────────────────

class TestEnvCheck:
    def test_no_requires_always_passes(self, monkeypatch):
        spec = PipelineSpec(
            name="test", file="x.py", desc="", stage=1,
            tables=["futures"], requires_env=[],
        )
        assert _check_env(spec) is None

    def test_present_env_var_passes(self, monkeypatch):
        monkeypatch.setenv("MY_TEST_KEY", "abc")
        spec = PipelineSpec(
            name="test", file="x.py", desc="", stage=1,
            tables=["futures"], requires_env=["MY_TEST_KEY"],
        )
        assert _check_env(spec) is None

    def test_missing_env_var_returns_reason(self, monkeypatch):
        monkeypatch.delenv("DEFINITELY_NOT_SET_XYZ", raising=False)
        spec = PipelineSpec(
            name="test", file="x.py", desc="", stage=1,
            tables=["futures"], requires_env=["DEFINITELY_NOT_SET_XYZ"],
        )
        reason = _check_env(spec)
        assert reason is not None
        assert "DEFINITELY_NOT_SET_XYZ" in reason

    def test_partial_env_missing_skips(self, monkeypatch):
        monkeypatch.setenv("SCHWAB_API_KEY", "present")
        monkeypatch.delenv("SCHWAB_APP_SECRET", raising=False)
        spec = PipelineSpec(
            name="test", file="x.py", desc="", stage=2,
            tables=["prices"], requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
        )
        reason = _check_env(spec)
        assert reason is not None
        assert "SCHWAB_APP_SECRET" in reason


# ── CLI argument parsing ──────────────────────────────────────────────────────

class TestCliFiltering:
    def _filtered(self, argv: list[str]) -> list[str]:
        """Parse argv and return the names of pipelines that would run."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--backfill",    action="store_true")
        parser.add_argument("--stage",       type=int, choices=[1, 2, 3])
        parser.add_argument("--only")
        parser.add_argument("--skip")
        parser.add_argument("--dry-run",     action="store_true")
        parser.add_argument("--no-validate", action="store_true")
        args = parser.parse_args(argv)

        pipelines = list(PIPELINES)
        if args.stage:
            pipelines = [p for p in pipelines if p.stage == args.stage]
        if args.only:
            only_set  = {n.strip() for n in args.only.split(",")}
            pipelines = [p for p in pipelines if p.name in only_set]
        if args.skip:
            skip_set  = {n.strip() for n in args.skip.split(",")}
            pipelines = [p for p in pipelines if p.name not in skip_set]
        return [p.name for p in pipelines]

    def test_no_filter_returns_all(self):
        names = self._filtered([])
        assert len(names) == len(PIPELINES)

    def test_stage_filter_returns_only_that_stage(self):
        names = self._filtered(["--stage", "1"])
        expected = {p.name for p in PIPELINES if p.stage == 1}
        assert set(names) == expected

    def test_only_filter_limits_to_specified(self):
        names = self._filtered(["--only", "commodity_macro,gas_prices"])
        assert set(names) == {"commodity_macro", "gas_prices"}

    def test_skip_filter_excludes_specified(self):
        names = self._filtered(["--skip", "fundamentals"])
        assert "fundamentals" not in names
        assert len(names) == len(PIPELINES) - 1

    def test_stage_and_skip_combine(self):
        names = self._filtered(["--stage", "1", "--skip", "fundamentals"])
        stage1 = {p.name for p in PIPELINES if p.stage == 1}
        expected = stage1 - {"fundamentals"}
        assert set(names) == expected


# ── Dry-run produces RunResult ────────────────────────────────────────────────

class TestDryRun:
    def test_dry_run_returns_dry_run_status(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "fake")
        spec = next(p for p in PIPELINES if p.name == "commodity_macro")
        result = ra.run_pipeline(spec, backfill=False, dry_run=True, validate=False)
        assert result.status == "DRY RUN"
        assert result.name == "commodity_macro"
        assert result.duration == 0.0

    def test_dry_run_skip_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        spec = next(p for p in PIPELINES if p.name == "commodity_macro")
        result = ra.run_pipeline(spec, backfill=False, dry_run=True, validate=False)
        assert result.status == "SKIP"
