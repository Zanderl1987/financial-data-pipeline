"""
test_logging.py — verify the shared logging framework (logging_utils.py) and
its integration into run_all.py's failure handling.

Tests do NOT touch the real storage/logs/ directory — LOG_DIR/FAILURE_DIR are
monkeypatched to a tmp_path per test.
"""

import logging
import logging.handlers
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import logging_utils as lu
import run_all as ra
from run_all import PipelineSpec


@pytest.fixture(autouse=True)
def _isolated_log_dirs(tmp_path, monkeypatch):
    """
    Redirect LOG_DIR/FAILURE_DIR to a scratch dir for every test in this file.

    run_all.py's module-level `log` is bound once at import time (before this
    fixture runs) to a RotatingFileHandler already pointed at the real
    storage/logs/run_all.log — patching lu.LOG_DIR alone wouldn't stop
    ra.run_pipeline()'s log.error()/log.exception() calls from landing there.
    Rebind ra.log to a fresh logger created after the patch so nothing in this
    file touches the real repo's log directory.
    """
    monkeypatch.setattr(lu, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(lu, "FAILURE_DIR", str(tmp_path / "failures"))
    monkeypatch.setattr(ra, "log", lu.get_logger(_fresh_logger_name("run_all_test")))
    yield


def _fresh_logger_name(name: str) -> str:
    """Return a logger name guaranteed not already in lu._configured."""
    n = 0
    candidate = name
    while candidate in lu._configured:
        n += 1
        candidate = f"{name}_{n}"
    return candidate


# ── get_logger ───────────────────────────────────────────────────────────────

class TestGetLogger:
    def test_writes_to_file_under_log_dir(self, tmp_path):
        name = _fresh_logger_name("test_writes")
        logger = lu.get_logger(name)
        logger.error("boom %d", 42)
        for h in logger.handlers:
            h.flush()

        log_file = tmp_path / f"{name}.log"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "boom 42" in content
        assert "ERROR" in content
        assert name in content  # file format includes logger name

    def test_has_console_and_file_handlers(self):
        name = _fresh_logger_name("test_handlers")
        logger = lu.get_logger(name)
        kinds = {type(h).__name__ for h in logger.handlers}
        assert "StreamHandler" in kinds
        assert "RotatingFileHandler" in kinds

    def test_idempotent_no_duplicate_handlers(self):
        name = _fresh_logger_name("test_idempotent")
        logger1 = lu.get_logger(name)
        n_handlers = len(logger1.handlers)
        logger2 = lu.get_logger(name)
        assert logger2 is logger1
        assert len(logger2.handlers) == n_handlers

    def test_rotating_file_handler_size_limits(self):
        name = _fresh_logger_name("test_rotation_config")
        logger = lu.get_logger(name)
        file_handler = next(
            h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        assert file_handler.maxBytes == 5 * 1024 * 1024
        assert file_handler.backupCount == 5

    def test_console_level_filters_below_threshold(self, capsys):
        name = _fresh_logger_name("test_console_level")
        logger = lu.get_logger(name, console_level=logging.WARNING)
        logger.info("should not print to console")
        logger.warning("should print to console")
        err = capsys.readouterr().err
        assert "should not print to console" not in err
        assert "should print to console" in err

    def test_file_level_defaults_to_debug(self, tmp_path):
        name = _fresh_logger_name("test_file_debug")
        logger = lu.get_logger(name, console_level=logging.CRITICAL)
        logger.debug("debug detail")
        for h in logger.handlers:
            h.flush()
        content = (tmp_path / f"{name}.log").read_text(encoding="utf-8")
        assert "debug detail" in content


# ── log_pipeline_failure ───────────────────────────────────────────────────────

class TestLogPipelineFailure:
    def test_writes_content_and_returns_path(self, tmp_path):
        path = lu.log_pipeline_failure("some_pipeline", "traceback goes here")
        assert os.path.exists(path)
        assert "some_pipeline" in os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            assert f.read() == "traceback goes here"

    def test_lands_under_failures_subdir(self, tmp_path):
        path = lu.log_pipeline_failure("x", "detail")
        assert os.path.dirname(path) == str(tmp_path / "failures")

    def test_distinct_calls_get_distinct_files(self, tmp_path):
        p1 = lu.log_pipeline_failure("dup", "first")
        p2 = lu.log_pipeline_failure("dup", "second")
        # Either distinct filenames, or (same-second collision) the later
        # write's content — never a mix/corruption of both.
        if p1 == p2:
            with open(p2, encoding="utf-8") as f:
                assert f.read() == "second"
        else:
            with open(p1, encoding="utf-8") as f:
                assert f.read() == "first"
            with open(p2, encoding="utf-8") as f:
                assert f.read() == "second"


# ── run_all.py integration: failures get persisted, not just a one-liner ──────

class TestRunPipelineFailureLogging:
    def _write_script(self, tmp_path, body: str) -> str:
        script = tmp_path / "fake_pipeline.py"
        script.write_text(body, encoding="utf-8")
        return str(script)

    def test_nonzero_exit_persists_output_and_notes_log_path(self, tmp_path, monkeypatch):
        script_path = self._write_script(
            tmp_path,
            "import sys\n"
            "print('doing work')\n"
            "print('uh oh', file=sys.stderr)\n"
            "sys.exit(1)\n",
        )
        spec = PipelineSpec(
            name="fake_pipeline", file=os.path.basename(script_path), desc="",
            stage=1, tables=["macro"],
        )
        monkeypatch.setattr(ra, "REPO_ROOT", str(tmp_path))
        result = ra.run_pipeline(spec, backfill=False, dry_run=False, validate=False)

        assert result.status == "FAIL"
        assert "exit 1" in result.note
        assert "log:" in result.note
        log_path = result.note.split("log: ", 1)[1]
        assert os.path.exists(log_path)
        content = open(log_path, encoding="utf-8").read()
        assert "doing work" in content
        assert "uh oh" in content

    def test_timeout_persists_partial_output(self, tmp_path, monkeypatch):
        script_path = self._write_script(
            tmp_path,
            "import time, sys\n"
            "print('partial progress', flush=True)\n"
            "time.sleep(5)\n",
        )
        spec = PipelineSpec(
            name="fake_slow_pipeline", file=os.path.basename(script_path), desc="",
            stage=1, tables=["macro"], timeout=1,
        )
        monkeypatch.setattr(ra, "REPO_ROOT", str(tmp_path))
        result = ra.run_pipeline(spec, backfill=False, dry_run=False, validate=False)

        assert result.status == "FAIL"
        assert "timed out" in result.note
        assert "log:" in result.note
        log_path = result.note.split("log: ", 1)[1]
        assert os.path.exists(log_path)

    def test_success_does_not_create_failure_log(self, tmp_path, monkeypatch):
        script_path = self._write_script(tmp_path, "print('all good')\n")
        spec = PipelineSpec(
            name="fake_ok_pipeline", file=os.path.basename(script_path), desc="",
            stage=1, tables=["macro"],
        )
        monkeypatch.setattr(ra, "REPO_ROOT", str(tmp_path))
        result = ra.run_pipeline(spec, backfill=False, dry_run=False, validate=False)

        assert result.status == "PASS"
        failures_dir = tmp_path / "failures"
        assert not failures_dir.exists() or not list(failures_dir.iterdir())
