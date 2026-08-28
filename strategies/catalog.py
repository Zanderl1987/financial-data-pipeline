"""
strategies/catalog.py -- assembles and persists the tv_strategy_catalog table
(preregistration section 7 schema) for the TV strategy catalog campaign.

Deliberately does NOT go through query.py's CATALOG dict or the Iceberg pilot
mirror (iceberg_pilot.py), even though the preregistration names those as the
storage path. That machinery is scoped to raw pipeline data sources (see
CLAUDE.md's "Adding a new pipeline" wiring checklist and iceberg_pilot.py's own
docstring: 4 core financial tables mirrored for real iceberg_scan reads); this
is a derived research artifact, one level removed from raw data, exactly like
evaluation/eval_registry -- which deliberately has its own small accessor
module (evaluation/registry.py: load()/append()/population()) instead of being
wired into query.py. This module follows that same precedent rather than
touching shared pilot infrastructure other pipelines depend on for a storage
detail outside the campaign's locked statistical protocol.

Catalog rows are rebuilt from evaluation/eval_registry/results.parquet's
"tv_strategy_catalog_stage3" rows (already written incrementally by
strategies/stage3.py) joined with cheap, non-recomputed metadata (.meta.json,
strategies.screen.screen_source(), the ports registry) -- so building/refreshing
the catalog never re-runs the expensive permutation test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage_utils
from evaluation import registry as ev_registry
from strategies import ports as strategy_ports
from strategies.screen import screen_source
from strategies.stage3 import TV_SCRIPTS_DIR, admitted_slugs

CATALOG_DIR = os.path.join("storage", "tv_strategy_catalog")
CATALOG_FILENAME = "tv_strategy_catalog.parquet"
EVALUATION_NAME = "tv_strategy_catalog_stage3"

SCHEMA_COLUMNS = [
    "strategy_id", "tv_url", "tv_author", "tv_script_name",
    "tv_boosts", "tv_views", "collected_at", "license",
    "mechanism_family", "param_count",
    "screen_status", "excluded_reason", "translation_verified",
    "n_trades", "win_rate", "profit_factor", "sharpe", "max_dd",
    "turnover", "median_hold",
    "total_pnl_net", "pnl_p", "pnl_p_5bps", "pnl_p_20bps", "cost_fragile",
    "bh_q", "fdr_pass",
    "holdout_pnl_p", "holdout_run_ts",
    "provisional", "stage", "run_id", "git_commit", "fetched_at",
]


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _stage3_wide() -> pd.DataFrame:
    """Pivot eval_registry's long (statistic, value) rows for this campaign
    back to one row per strategy_id."""
    reg = ev_registry.load()
    sub = reg[reg["evaluation"] == EVALUATION_NAME].copy()
    if sub.empty:
        return pd.DataFrame(columns=["strategy_id", "run_id"])
    sub["strategy_id"] = sub["input_name"].str.replace("^pine_", "", regex=True)
    # keep the latest run per (strategy_id, statistic) in case of a re-run
    sub = sub.sort_values("created_at").drop_duplicates(
        subset=["strategy_id", "statistic"], keep="last")
    wide = sub.pivot(index="strategy_id", columns="statistic", values="value")
    run_ids = sub.groupby("strategy_id")["run_id"].last()
    wide["run_id"] = run_ids
    return wide.reset_index()


def build_catalog_rows() -> pd.DataFrame:
    """One row per admitted strategy: Stage 3 numeric results (if run yet)
    joined with provenance/screen metadata. Strategies not yet Stage-3-tested
    still appear, with stage="stage2" and the numeric columns null."""
    stage3 = _stage3_wide()
    git_commit = _git_commit()
    fetched_at = pd.Timestamp.now("UTC").isoformat()

    ported = {info.slug for info in strategy_ports.all_ports()}
    rows = []
    for slug, note in sorted(admitted_slugs().items()):
        pine_path = os.path.join(TV_SCRIPTS_DIR, f"{slug}.pine")
        with open(pine_path, "r", encoding="utf-8") as f:
            screen = screen_source(f.read(), script_name=slug)
        meta_path = os.path.join(TV_SCRIPTS_DIR, f"{slug}.meta.json")
        meta = {}
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

        s3 = stage3[stage3["strategy_id"] == slug] if not stage3.empty else pd.DataFrame()
        has_stage3 = not s3.empty

        def get(col):
            if not has_stage3 or col not in s3.columns:
                return None
            v = s3.iloc[0][col]
            return None if pd.isna(v) else v

        pnl_p_5, pnl_p_20 = get("pnl_p_5bps"), get("pnl_p_20bps")
        cost_fragile = (None if None in (pnl_p_5, pnl_p_20)
                        else bool((pnl_p_5 < 0.05) != (pnl_p_20 < 0.05)))

        rows.append({
            "strategy_id": slug,
            "tv_url": meta.get("tv_url"),
            "tv_author": meta.get("tv_author"),
            "tv_script_name": meta.get("tv_script_name"),
            "tv_boosts": meta.get("tv_boosts"),
            "tv_views": meta.get("tv_views"),
            "collected_at": meta.get("collected_at"),
            "license": meta.get("license"),
            "mechanism_family": screen.mechanism_family,
            "param_count": screen.param_count,
            "screen_status": "admitted",
            "excluded_reason": None,
            "translation_verified": "unit_tested" if slug in ported else "unverified",
            "n_trades": get("n_trades"),
            "win_rate": get("win_rate"),
            "profit_factor": get("profit_factor"),
            "sharpe": get("sharpe"),
            "max_dd": get("max_dd"),
            "turnover": None,   # no portfolio-level capital tracking in evaluation/trades.py
            "median_hold": get("median_hold"),
            "total_pnl_net": get("total_pnl_net"),
            "pnl_p": get("pnl_p"),
            "pnl_p_5bps": pnl_p_5,
            "pnl_p_20bps": pnl_p_20,
            "cost_fragile": cost_fragile,
            "bh_q": None,          # Stage 4: computed campaign-wide at close
            "fdr_pass": None,
            "holdout_pnl_p": None,  # Stage 5: one-shot, not touched here
            "holdout_run_ts": None,
            "provisional": True,
            "stage": "stage3" if has_stage3 else "stage2",
            "run_id": get("run_id"),
            "git_commit": git_commit,
            "fetched_at": fetched_at,
        })
    return pd.DataFrame(rows, columns=SCHEMA_COLUMNS)


def write_catalog_table(df: "pd.DataFrame | None" = None) -> str:
    """Full-rebuild write of the catalog table (matches eval_registry's
    every-strategy-carries-provisional=True convention -- this isn't an
    append-only log, it's a rebuildable snapshot)."""
    if df is None:
        df = build_catalog_rows()
    return storage_utils.write_partitioned(df, CATALOG_DIR, CATALOG_FILENAME)


def load_catalog_table() -> pd.DataFrame:
    """Read back the most recently written snapshot (all partitions)."""
    import glob
    files = glob.glob(os.path.join(CATALOG_DIR, "**", CATALOG_FILENAME), recursive=True)
    if not files:
        return pd.DataFrame(columns=SCHEMA_COLUMNS)
    latest = max(files, key=os.path.getmtime)
    return pd.read_parquet(latest)


if __name__ == "__main__":
    out = write_catalog_table()
    rows = load_catalog_table()
    print(f"wrote {len(rows)} rows -> {out}")
    tested = rows[rows["stage"] == "stage3"]
    print(f"{len(tested)}/{len(rows)} strategies have Stage 3 results so far")
    if not tested.empty:
        print(tested[["strategy_id", "n_trades", "pnl_p", "cost_fragile"]].to_string(index=False))
