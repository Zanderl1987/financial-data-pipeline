"""
quota_tracker.py — API quota tracking for external data sources.

Tracks daily request counts per source to stay within API limits.
State is persisted to a JSON file so it survives process restarts.

Usage:
    from quota_tracker import QuotaTracker

    tracker = QuotaTracker()
    if tracker.is_over_quota("finnhub"):
        raise RuntimeError("Finnhub daily quota exhausted")

    # ... make API call ...
    tracker.record_request("finnhub")

CLI:
    python quota_tracker.py            # print current usage for all sources
    python quota_tracker.py --reset    # reset all counters
    python quota_tracker.py --reset finnhub  # reset one source
"""

import datetime
import json
import logging
import os
import sys

logger = logging.getLogger("quota_tracker")

_DEFAULT_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


class QuotaTracker:
    """Track daily API usage per data source.

    Quotas are keyed by date — counters reset automatically at midnight UTC.
    """

    QUOTAS: dict[str, dict[str, int | str]] = {
        "finnhub":       {"limit": 60,   "period": "minute"},
        "eia":           {"limit": 5000, "period": "day"},
        "fred":          {"limit": 120,  "period": "minute"},
        "alpha_vantage": {"limit": 25,   "period": "day"},
        "sec_edgar":     {"limit": 10,   "period": "second"},
    }

    def __init__(self, state_file: str | None = None) -> None:
        """Initialise tracker with optional custom state file path.

        Args:
            state_file: JSON file for persisting counters.  Defaults to
                        ``data/quota_state.json`` relative to the repo root.
        """
        self.state_file = state_file or os.path.join(
            _DEFAULT_STATE_DIR, "quota_state.json"
        )
        self._state: dict[str, dict] = self._load()

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        """Load persisted state from disk."""
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.debug("Could not load quota state (%s) — starting fresh", exc)
            return {}

    def _save(self) -> None:
        """Persist current state to disk."""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            logger.error("Failed to save quota state: %s", exc)

    # ── public API ───────────────────────────────────────────────────────

    def record_request(self, source: str) -> None:
        """Record one API request for the given source.

        Automatically resets the counter if the current date differs
        from the last recorded date.
        """
        today = datetime.date.today().isoformat()
        entry = self._state.get(source)

        if entry is None or entry.get("date") != today:
            self._state[source] = {"count": 1, "date": today}
        else:
            entry["count"] += 1

        self._save()

    def get_usage(self, source: str) -> dict[str, int]:
        """Get current usage for a source.

        Returns:
            Dict with ``count`` (requests today) and ``limit`` (daily cap).
        """
        today = datetime.date.today().isoformat()
        entry = self._state.get(source, {"count": 0, "date": today})

        # Counter is from a previous day — treat as zeroed.
        if entry.get("date") != today:
            count = 0
        else:
            count = entry.get("count", 0)

        limit = self.QUOTAS.get(source, {}).get("limit", 999)
        return {"count": count, "limit": int(limit)}

    def is_over_quota(self, source: str) -> bool:
        """Check if a source has reached or exceeded its daily quota."""
        usage = self.get_usage(source)
        return usage["count"] >= usage["limit"]

    def remaining(self, source: str) -> int:
        """Return the number of remaining requests for today."""
        usage = self.get_usage(source)
        return max(0, usage["limit"] - usage["count"])

    def reset(self, source: str | None = None) -> None:
        """Reset counters for one source or all sources.

        Args:
            source: If given, reset only that source.  Otherwise reset all.
        """
        if source:
            self._state.pop(source, None)
        else:
            self._state.clear()
        self._save()

    def summary(self) -> str:
        """Return a human-readable usage summary string."""
        lines: list[str] = []
        for name in sorted(self.QUOTAS):
            usage = self.get_usage(name)
            bar_len = 20
            used = min(usage["count"], usage["limit"])
            filled = int(bar_len * used / usage["limit"]) if usage["limit"] else 0
            bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
            lines.append(
                f"{name:15s}  [{bar}]  {usage['count']:>5d}/{usage['limit']}"
            )
        return "\n".join(lines)


if __name__ == "__main__":
    tracker = QuotaTracker()
    if "--reset" in sys.argv:
        idx = sys.argv.index("--reset")
        target = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        tracker.reset(target)
        print(f"Reset {target or 'all sources'}.")
    else:
        print(tracker.summary())
