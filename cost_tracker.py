"""
cost_tracker.py — Per-pipeline API cost tracking.

Maintains a registry of API call costs and accumulates running totals
per pipeline and per run.  Persists state to a JSON file.

Cost registry:
    Some APIs charge per-call (e.g. Finnhub), some are free but have
    rate limits, and some are flat-rate.  This module tracks the
    monetary cost of calls where applicable.

Usage (CLI):
    python cost_tracker.py                    # show run totals
    python cost_tracker.py --history          # show all runs
    python cost_tracker.py --reset            # clear all data

Usage (API):
    from cost_tracker import CostTracker

    tracker = CostTracker()
    tracker.record_call("finnhub", calls=1)
    tracker.record_call("fred", calls=5)
    tracker.record_run(run_id="run_2026_07_27", pipelines={...})
    print(tracker.summary())
"""

import datetime
import json
import os
import sys

logger = __import__("logging").getLogger("cost_tracker")

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.join(REPO_ROOT, "data")


# ── Cost registry ───────────────────────────────────────────────────────────
# Cost per API call in USD (0 = free)
API_COSTS: dict[str, dict] = {
    "finnhub":         {"cost_per_call": 0.0,    "free_tier_calls": 60,   "notes": "60 calls/min free"},
    "fred":            {"cost_per_call": 0.0,    "free_tier_calls": 120,  "notes": "Unlimited free"},
    "eia":             {"cost_per_call": 0.0,    "free_tier_calls": 5000, "notes": "Free with API key"},
    "alpha_vantage":   {"cost_per_call": 0.0,    "free_tier_calls": 25,   "notes": "25 calls/day free"},
    "sec_edgar":       {"cost_per_call": 0.0,    "free_tier_calls": 999,  "notes": "Free, 10 req/s"},
    "yfinance":        {"cost_per_call": 0.0,    "free_tier_calls": 999,  "notes": "Unofficial, free"},
    "schwab":          {"cost_per_call": 0.0,    "free_tier_calls": 999,  "notes": "Brokerage API, free"},
    "coingecko":       {"cost_per_call": 0.0,    "free_tier_calls": 30,   "notes": "30 calls/min free"},
    "tiingo":          {"cost_per_call": 0.0,    "free_tier_calls": 500,  "notes": "Free tier available"},
    "cftc":            {"cost_per_call": 0.0,    "free_tier_calls": 999,  "notes": "Public data, free"},
    "stocktwits":      {"cost_per_call": 0.0,    "free_tier_calls": 999,  "notes": "Scraped, free"},
    "sec_edgar_f4":    {"cost_per_call": 0.0,    "free_tier_calls": 999,  "notes": "EDGAR, free"},
    "finra_ats":       {"cost_per_call": 0.0,    "free_tier_calls": 999,  "notes": "Public data, free"},
}


class CostTracker:
    """Track API costs across pipeline runs.

    Persists to ``data/cost_state.json``.
    """

    def __init__(self, state_file: str | None = None) -> None:
        self.state_file = state_file or os.path.join(DEFAULT_STATE_DIR, "cost_state.json")
        self._state: dict = self._load()
        # Accumulators for current run
        self._current_calls: dict[str, int] = {}
        self._current_cost: float = 0.0

    def _load(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"runs": {}, "totals": {"total_calls": 0, "total_cost_usd": 0.0}}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            logger.error("Failed to save cost state: %s", exc)

    def record_call(self, source: str, calls: int = 1) -> float:
        """Record API calls and return their cost.

        Args:
            source: API source name (must be in API_COSTS).
            calls: Number of API calls made.

        Returns:
            Cost in USD for this batch of calls.
        """
        cost_info = API_COSTS.get(source, {"cost_per_call": 0.0})
        cost = cost_info["cost_per_call"] * calls
        self._current_calls[source] = self._current_calls.get(source, 0) + calls
        self._current_cost += cost
        return cost

    def record_run(self, run_id: str, pipelines: dict | None = None) -> dict:
        """Record the cost summary for a completed run.

        Args:
            run_id: Unique run identifier.
            pipelines: Optional dict of pipeline metrics from RunTracker.

        Returns:
            Run cost summary dict.
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        run_summary = {
            "run_id": run_id,
            "timestamp": now,
            "calls_by_source": dict(self._current_calls),
            "total_calls": sum(self._current_calls.values()),
            "total_cost_usd": round(self._current_cost, 6),
        }

        self._state["runs"][run_id] = run_summary
        self._state["totals"]["total_calls"] += run_summary["total_calls"]
        self._state["totals"]["total_cost_usd"] = round(
            self._state["totals"]["total_cost_usd"] + self._current_cost, 6
        )
        self._save()

        # Reset per-run accumulators
        self._current_calls.clear()
        self._current_cost = 0.0

        return run_summary

    def get_totals(self) -> dict:
        """Get cumulative totals across all runs."""
        return self._state.get("totals", {"total_calls": 0, "total_cost_usd": 0.0})

    def get_run_history(self, limit: int = 20) -> list[dict]:
        """Get recent run summaries."""
        runs = self._state.get("runs", {})
        sorted_runs = sorted(runs.values(), key=lambda r: r.get("timestamp", ""), reverse=True)
        return sorted_runs[:limit]

    def get_source_totals(self) -> dict[str, int]:
        """Get cumulative calls per source."""
        totals: dict[str, int] = {}
        for run in self._state.get("runs", {}).values():
            for source, calls in run.get("calls_by_source", {}).items():
                totals[source] = totals.get(source, 0) + calls
        return totals

    def summary(self) -> str:
        """Human-readable summary string."""
        totals = self.get_totals()
        source_totals = self.get_source_totals()
        recent = self.get_run_history(limit=5)

        lines = [
            f"\n{'=' * 50}",
            f"  COST TRACKER SUMMARY",
            f"{'=' * 50}",
            f"  Total API calls: {totals['total_calls']:,}",
            f"  Total cost:      ${totals['total_cost_usd']:.4f}",
            "",
            "  Calls by source:",
        ]
        for src in sorted(source_totals, key=source_totals.get, reverse=True):
            cost = API_COSTS.get(src, {}).get("cost_per_call", 0.0) * source_totals[src]
            lines.append(f"    {src:20s}  {source_totals[src]:>8,} calls  ${cost:.4f}")

        if recent:
            lines.append(f"\n  Last run: {recent[0].get('timestamp', '?')[:16]}")
            lines.append(f"    {recent[0]['total_calls']} calls, ${recent[0]['total_cost_usd']:.4f}")

        return "\n".join(lines)

    def reset(self) -> None:
        """Clear all tracked data."""
        self._state = {"runs": {}, "totals": {"total_calls": 0, "total_cost_usd": 0.0}}
        self._current_calls.clear()
        self._current_cost = 0.0
        self._save()


if __name__ == "__main__":
    tracker = CostTracker()
    if "--reset" in sys.argv:
        tracker.reset()
        print("Cost tracker reset.")
    elif "--history" in sys.argv:
        for run in tracker.get_run_history(limit=20):
            print(f"  {run['timestamp'][:16]}  {run['total_calls']:>5} calls  ${run['total_cost_usd']:.4f}")
    else:
        print(tracker.summary())
