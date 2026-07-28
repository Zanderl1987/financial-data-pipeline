"""
freshness_dashboard.py — Data freshness monitoring dashboard.

Scans all CATALOG tables, finds the latest Parquet file for each,
extracts the newest date, and produces an HTML/JSON report showing
staleness per table.

Usage (CLI):
    python freshness_dashboard.py                  # print freshness report
    python freshness_dashboard.py --json           # JSON output
    python freshness_dashboard.py --html           # generate dashboard.html
    python freshness_dashboard.py --warn-hours 48  # custom staleness threshold

Usage (API):
    from freshness_dashboard import get_freshness_report, generate_html

    report = get_freshness_report()
    generate_html(report, "dashboard.html")
"""

import datetime
import glob as _glob_mod
import json
import os
import sys
from dataclasses import dataclass, asdict

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)
import query as c


# ── Data structures ─────────────────────────────────────────────────────────

@dataclass
class TableFreshness:
    table: str
    latest_date: str | None     # ISO date of newest row
    latest_file: str            # basename of newest parquet
    file_count: int             # number of parquet files
    total_rows: int             # approximate row count (from newest file)
    staleness_hours: float | None   # hours since latest_date
    status: str                 # "FRESH" | "STALE" | "STALE_CRITICAL" | "NO DATA" | "NO DATE COL"
    threshold_hours: float      # used threshold


# ── Core functions ──────────────────────────────────────────────────────────

def _find_date_column(table: str) -> str | None:
    """Find the date column for freshness checks using SCHEMAS."""
    from validate import SCHEMAS
    schema = SCHEMAS.get(table, {})
    if schema.get("date_col"):
        return schema["date_col"]
    return None


def get_freshness_report(
    warn_hours: float = 48.0,
    critical_hours: float = 168.0,
    storage_root: str | None = None,
) -> list[TableFreshness]:
    """Compute freshness for all CATALOG tables.

    Args:
        warn_hours: Staleness threshold for WARNING status.
        critical_hours: Staleness threshold for STALE_CRITICAL status.
        storage_root: Override default storage path.

    Returns:
        List of TableFreshness, sorted by staleness (worst first).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    results: list[TableFreshness] = []

    for table_name, glob_path in sorted(c.CATALOG.items()):
        if storage_root:
            rel = glob_path.split("storage/raw/")[1] if "storage/raw/" in glob_path else glob_path
            glob_path = os.path.join(storage_root, rel).replace("\\", "/")

        date_col = _find_date_column(table_name)

        files = sorted(
            _glob_mod.glob(glob_path.replace("/", os.sep), recursive=True),
            key=os.path.getmtime,
        )

        if not files:
            results.append(TableFreshness(
                table=table_name, latest_date=None, latest_file="",
                file_count=0, total_rows=0, staleness_hours=None,
                status="NO DATA", threshold_hours=warn_hours,
            ))
            continue

        latest_file = files[-1]
        filename = os.path.basename(latest_file)

        max_date: datetime.date | None = None
        row_count = 0
        try:
            if date_col:
                df = pd.read_parquet(latest_file, columns=[date_col])
                dates = pd.to_datetime(df[date_col], errors="coerce")
                max_dt = dates.max()
                if not pd.isna(max_dt):
                    max_date = max_dt.date()
                row_count = len(df)
            else:
                df = pd.read_parquet(latest_file)
                row_count = len(df)
        except Exception:
            row_count = 0

        staleness_h: float | None = None
        status = "NO DATE COL"
        if max_date:
            delta = now.replace(tzinfo=None) - datetime.datetime.combine(max_date, datetime.time())
            staleness_h = delta.total_seconds() / 3600
            if staleness_h <= warn_hours:
                status = "FRESH"
            elif staleness_h <= critical_hours:
                status = "STALE"
            else:
                status = "STALE_CRITICAL"

        results.append(TableFreshness(
            table=table_name,
            latest_date=max_date.isoformat() if max_date else None,
            latest_file=filename,
            file_count=len(files),
            total_rows=row_count,
            staleness_hours=round(staleness_h, 1) if staleness_h is not None else None,
            status=status,
            threshold_hours=warn_hours,
        ))

    severity_order = {"STALE_CRITICAL": 0, "STALE": 1, "NO DATA": 2, "NO DATE COL": 3, "FRESH": 4}
    results.sort(key=lambda x: (severity_order.get(x.status, 5), -(x.staleness_hours or 0)))
    return results


# ── Output formatters ───────────────────────────────────────────────────────

def print_freshness_report(report: list[TableFreshness]) -> None:
    """Print a human-readable freshness report."""
    icons = {"FRESH": "+", "STALE": "!", "STALE_CRITICAL": "X", "NO DATA": "-", "NO DATE COL": "?"}
    print(f"\n{'=' * 72}")
    print(f"  DATA FRESHNESS REPORT — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 72}\n")
    for item in report:
        icon = icons.get(item.status, "?")
        age = f"{item.staleness_hours:.0f}h" if item.staleness_hours is not None else "n/a"
        date = item.latest_date or "n/a"
        print(f"  [{icon}] {item.status:14s}  {item.table:30s}  date={date}  age={age:>6s}  files={item.file_count}")
    fresh = sum(1 for r in report if r.status == "FRESH")
    stale = sum(1 for r in report if r.status in ("STALE", "STALE_CRITICAL"))
    no_data = sum(1 for r in report if r.status == "NO DATA")
    print(f"\n  {fresh} FRESH  |  {stale} STALE  |  {no_data} NO DATA")


def generate_html(report: list[TableFreshness], path: str = "dashboard.html") -> str:
    """Generate an HTML freshness dashboard. Returns absolute path."""
    rows_html = []
    for item in report:
        age_str = f"{item.staleness_hours:.0f}h" if item.staleness_hours is not None else "n/a"
        date_str = item.latest_date or "n/a"
        color = {
            "FRESH": "#22c55e", "STALE": "#f59e0b", "STALE_CRITICAL": "#ef4444",
            "NO DATA": "#6b7280", "NO DATE COL": "#8b5cf6",
        }.get(item.status, "#6b7280")
        rows_html.append(
            f'<tr><td><span class="badge" style="background:{color}">{item.status}</span></td>'
            f'<td>{item.table}</td><td>{date_str}</td><td>{age_str}</td>'
            f'<td>{item.file_count}</td><td>{item.total_rows:,}</td></tr>'
        )

    fresh_n = sum(1 for r in report if r.status == "FRESH")
    stale_n = sum(1 for r in report if r.status in ("STALE", "STALE_CRITICAL"))
    nodata_n = sum(1 for r in report if r.status == "NO DATA")

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>Data Freshness Dashboard</title>
<style>
body {{ font-family:system-ui,sans-serif; margin:2rem; background:#0f172a; color:#e2e8f0; }}
h1 {{ color:#f8fafc; }}
table {{ border-collapse:collapse; width:100%; }}
th,td {{ padding:.75rem 1rem; text-align:left; border-bottom:1px solid #1e293b; }}
th {{ color:#94a3b8; font-size:.85rem; text-transform:uppercase; }}
.badge {{ padding:.25rem .5rem; border-radius:4px; font-size:.75rem; font-weight:600; color:white; }}
.summary {{ display:flex; gap:2rem; margin-bottom:2rem; }}
.card {{ background:#1e293b; padding:1rem 1.5rem; border-radius:8px; }}
.card .label {{ color:#94a3b8; font-size:.8rem; }}
.card .value {{ font-size:1.5rem; font-weight:700; }}
</style></head><body>
<h1>Data Freshness Dashboard</h1>
<p style="color:#94a3b8">Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<div class="summary">
  <div class="card"><div class="label">FRESH</div><div class="value" style="color:#22c55e">{fresh_n}</div></div>
  <div class="card"><div class="label">STALE</div><div class="value" style="color:#f59e0b">{stale_n}</div></div>
  <div class="card"><div class="label">NO DATA</div><div class="value" style="color:#6b7280">{nodata_n}</div></div>
  <div class="card"><div class="label">TOTAL</div><div class="value">{len(report)}</div></div>
</div>
<table><thead><tr><th>Status</th><th>Table</th><th>Latest Date</th><th>Age</th><th>Files</th><th>Rows</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table></body></html>"""

    dest = os.path.join(REPO_ROOT, path) if not os.path.isabs(path) else path
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(html)
    return dest


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Data freshness dashboard")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--html", action="store_true", help="Generate HTML dashboard")
    parser.add_argument("--warn-hours", type=float, default=48.0, help="Staleness threshold (hours)")
    args = parser.parse_args()
    report = get_freshness_report(warn_hours=args.warn_hours)
    if args.json:
        print(json.dumps([asdict(r) for r in report], indent=2))
    elif args.html:
        path = generate_html(report)
        print(f"Dashboard written to {path}")
    else:
        print_freshness_report(report)
