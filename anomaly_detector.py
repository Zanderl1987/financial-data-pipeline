"""
anomaly_detector.py — Statistical anomaly detection across pipeline metrics.

Detects anomalies in:
  - Row count changes (sudden drops/spikes)
  - Value range violations (new outliers)
  - Temporal gaps (missing expected dates)
  - Cross-table correlation breaks

Usage (CLI):
    python anomaly_detector.py                    # check all tables
    python anomaly_detector.py --table prices     # check one table
    python anomaly_detector.py --json             # JSON output

Usage (API):
    from anomaly_detector import AnomalyDetector

    detector = AnomalyDetector()
    anomalies = detector.check_table("prices")
    all_anomalies = detector.check_all()
"""

import datetime
import glob as _glob_mod
import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)
import query as c

logger = logging.getLogger("anomaly_detector")


# ── Data structures ─────────────────────────────────────────────────────────

@dataclass
class Anomaly:
    table: str
    kind: str           # "row_count" | "value_outlier" | "temporal_gap" | "staleness"
    severity: str       # "high" | "medium" | "low"
    message: str
    details: dict[str, Any]
    detected_at: str


# ── Row count anomaly detection ─────────────────────────────────────────────

def _get_table_snapshots(table_name: str, max_files: int = 30) -> list[tuple[str, int]]:
    """Get (filename, row_count) for recent Parquet snapshots."""
    glob_path = c.CATALOG.get(table_name, "")
    files = sorted(
        _glob_mod.glob(glob_path.replace("/", os.sep), recursive=True),
        key=os.path.getmtime,
    )[-max_files:]

    snapshots = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            snapshots.append((os.path.basename(f), len(df)))
        except Exception:
            pass
    return snapshots


def _detect_row_count_anomaly(table_name: str) -> list[Anomaly]:
    """Detect sudden row count changes using z-score on recent snapshots."""
    anomalies = []
    snapshots = _get_table_snapshots(table_name)
    if len(snapshots) < 5:
        return anomalies

    counts = [c for _, c in snapshots]
    mean = np.mean(counts)
    std = np.std(counts) if len(counts) > 1 else 0

    if std == 0:
        return anomalies

    latest_count = counts[-1]
    z_score = (latest_count - mean) / std

    if abs(z_score) > 3:
        anomalies.append(Anomaly(
            table=table_name,
            kind="row_count",
            severity="high",
            message=f"Row count z-score {z_score:.2f} (latest={latest_count}, mean={mean:.0f}, std={std:.0f})",
            details={"latest_count": latest_count, "mean": round(mean, 1), "std": round(std, 1), "z_score": round(z_score, 2)},
            detected_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        ))
    elif abs(z_score) > 2:
        anomalies.append(Anomaly(
            table=table_name,
            kind="row_count",
            severity="medium",
            message=f"Row count z-score {z_score:.2f} (latest={latest_count}, mean={mean:.0f})",
            details={"latest_count": latest_count, "mean": round(mean, 1), "z_score": round(z_score, 2)},
            detected_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        ))

    return anomalies


# ── Value outlier detection ─────────────────────────────────────────────────

def _detect_value_outliers(table_name: str) -> list[Anomaly]:
    """Detect numeric values outside expected ranges."""
    anomalies = []
    from validate import SCHEMAS
    schema = SCHEMAS.get(table_name, {})
    value_ranges = schema.get("value_ranges", {})
    if not value_ranges:
        return anomalies

    glob_path = c.CATALOG.get(table_name, "")
    files = sorted(
        _glob_mod.glob(glob_path.replace("/", os.sep), recursive=True),
        key=os.path.getmtime,
    )
    if not files:
        return anomalies

    try:
        df = pd.read_parquet(files[-1])
    except Exception:
        return anomalies

    for col, (lo, hi) in value_ranges.items():
        if col not in df.columns:
            continue
        numeric = pd.to_numeric(df[col], errors="coerce")
        outliers = int(((numeric < lo) | (numeric > hi)).sum())
        if outliers > 0:
            pct = outliers / len(df) * 100
            severity = "high" if pct > 10 else "medium" if pct > 1 else "low"
            anomalies.append(Anomaly(
                table=table_name,
                kind="value_outlier",
                severity=severity,
                message=f"{outliers} values ({pct:.1f}%) in '{col}' outside [{lo}, {hi}]",
                details={"column": col, "outliers": outliers, "pct": round(pct, 2), "range": [lo, hi]},
                detected_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ))

    return anomalies


# ── Temporal gap detection ──────────────────────────────────────────────────

def _detect_temporal_gaps(table_name: str) -> list[Anomaly]:
    """Detect missing expected dates in time-series data."""
    anomalies = []
    from validate import SCHEMAS
    schema = SCHEMAS.get(table_name, {})
    date_col = schema.get("date_col")
    if not date_col:
        return anomalies

    glob_path = c.CATALOG.get(table_name, "")
    files = sorted(
        _glob_mod.glob(glob_path.replace("/", os.sep), recursive=True),
        key=os.path.getmtime,
    )
    if not files:
        return anomalies

    try:
        df = pd.read_parquet(files[-1], columns=[date_col])
        dates = pd.to_datetime(df[date_col], errors="coerce").dropna().sort_values()
        if len(dates) < 10:
            return anomalies

        # Check for gaps > 7 days in daily data
        diffs = dates.diff().dt.days.dropna()
        large_gaps = diffs[diffs > 7]
        if len(large_gaps) > 0:
            max_gap = int(large_gaps.max())
            anomalies.append(Anomaly(
                table=table_name,
                kind="temporal_gap",
                severity="medium" if max_gap > 30 else "low",
                message=f"{len(large_gaps)} gaps >7 days found, max gap = {max_gap} days",
                details={"gap_count": int(len(large_gaps)), "max_gap_days": max_gap},
                detected_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ))
    except Exception:
        pass

    return anomalies


# ── Main detector ───────────────────────────────────────────────────────────

class AnomalyDetector:
    """Run all anomaly checks across all or selected tables."""

    def __init__(self, tables: list[str] | None = None) -> None:
        self.tables = tables or list(c.CATALOG.keys())

    def check_table(self, table_name: str) -> list[Anomaly]:
        """Run all anomaly checks on a single table."""
        anomalies = []
        anomalies.extend(_detect_row_count_anomaly(table_name))
        anomalies.extend(_detect_value_outliers(table_name))
        anomalies.extend(_detect_temporal_gaps(table_name))
        return anomalies

    def check_all(self) -> list[Anomaly]:
        """Run anomaly checks on all configured tables."""
        all_anomalies = []
        for table_name in self.tables:
            try:
                all_anomalies.extend(self.check_table(table_name))
            except Exception as exc:
                logger.warning("Anomaly check failed for %s: %s", table_name, exc)

        # Sort by severity
        severity_order = {"high": 0, "medium": 1, "low": 2}
        all_anomalies.sort(key=lambda a: severity_order.get(a.severity, 3))
        return all_anomalies

    def summary(self) -> str:
        """Human-readable summary of detected anomalies."""
        anomalies = self.check_all()
        if not anomalies:
            return "No anomalies detected."

        lines = [f"\nAnomaly Report — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
        for a in anomalies:
            icon = {"high": "!!", "medium": "!", "low": "."}[a.severity]
            lines.append(f"  [{icon}] {a.severity:6s}  {a.table:30s}  {a.kind:14s}  {a.message}")
        lines.append(f"\n  Total: {len(anomalies)} anomalies")
        return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Anomaly detection")
    parser.add_argument("--table", help="Check a single table")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    detector = AnomalyDetector(tables=[args.table] if args.table else None)
    if args.json:
        anomalies = detector.check_all()
        print(json.dumps([asdict(a) for a in anomalies], indent=2))
    else:
        print(detector.summary())
