"""
Shared logging helper — structured, persisted error/run logs.

Pipelines and orchestration scripts call get_logger() instead of ad-hoc
print()/logging.basicConfig() calls. Console output stays ASCII-only (no
unicode box-drawing — see CLAUDE.md gotchas); every logger also writes to a
rotating file under storage/logs/<name>.log, so a failure is diagnosable
after the fact without rerunning live.
"""

import datetime
import logging
import logging.handlers
import os

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(REPO_ROOT, "storage", "logs")
FAILURE_DIR = os.path.join(LOG_DIR, "failures")

_CONSOLE_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_FILE_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured: set[str] = set()


def get_logger(
    name: str,
    *,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> logging.Logger:
    """
    Return a logger named `name` writing to console and to a rotating file
    at storage/logs/<name>.log (5MB x 5 backups).

    Idempotent — calling twice with the same name returns the same logger
    without duplicating handlers, so it's safe to call at module import time.
    """
    logger = logging.getLogger(name)
    if name in _configured:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt=_DATEFMT))
    logger.addHandler(console)

    os.makedirs(LOG_DIR, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, f"{name}.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATEFMT))
    logger.addHandler(file_handler)

    _configured.add(name)
    return logger


def log_pipeline_failure(name: str, detail: str) -> str:
    """
    Persist a failed run's full captured output to its own timestamped file
    under storage/logs/failures/ and return the path.

    Kept separate from the rotating per-logger file so one specific failure's
    full context survives log rotation and is easy to open/paste directly,
    instead of having to reconstruct it from an interleaved rotating log.
    """
    os.makedirs(FAILURE_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(FAILURE_DIR, f"{name}_{stamp}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(detail)
    return path
