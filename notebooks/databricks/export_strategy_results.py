"""Export the TradingView strategy catalog's development results for the Databricks notebook.

The evaluation registry (storage/eval_registry/results.parquet) is local state: gitignored and not
published to Hugging Face. Section 13 of fdp_visual_tour.py reads this small CSV instead, from the
Git folder next to the notebook (or from GitHub raw when run elsewhere).

Outputs:
    notebooks/databricks/data/tv_catalog_v2.csv   one row per strategy with a Stage 3 v2 result
    notebooks/databricks/data/tv_catalog_meta.json   admitted count, split, export time

Usage (from the repo root):
    C:\\ProgramData\\anaconda3\\python.exe notebooks/databricks/export_strategy_results.py

Only development-split numbers are exported. Holdout status is a flag (spent or not), never a result
that could be read before the campaign closes.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

REGISTRY = os.path.join(ROOT, "storage", "eval_registry", "results.parquet")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STAGE3_V2 = "tv_strategy_catalog_stage3_v2"
HOLDOUT_EVALS = ("tv_strategy_catalog_stage5", "tv_strategy_catalog_stage5_v2")
STATS = ["n_trades", "win_rate", "profit_factor", "sharpe", "median_hold", "total_pnl_net",
         "pnl_p", "pnl_p_5bps", "pnl_p_20bps", "null_exposure_ratio", "exposure_mismatch"]


def display_name(slug: str) -> str:
    """'8iayxxss_hyperliquid_ready_webhook_template' -> 'hyperliquid ready webhook template'.
    The 8-character TradingView id prefix is dropped when present."""
    parts = slug.split("_", 1)
    if len(parts) == 2 and len(parts[0]) == 8 and re.search(r"\d", parts[0]):
        slug = parts[1]
    return slug.replace("_", " ")


def main() -> None:
    reg = pd.read_parquet(REGISTRY)
    v2 = reg[reg["evaluation"] == STAGE3_V2]
    if v2.empty:
        raise SystemExit(f"no {STAGE3_V2} rows in {REGISTRY}")
    # Rows are append-only; the newest row per (strategy, statistic) is the current result.
    v2 = v2.sort_values("created_at")
    wide = v2.pivot_table(index="input_name", columns="statistic", values="value", aggfunc="last")
    missing = [s for s in STATS if s not in wide.columns]
    if missing:
        raise SystemExit(f"registry lacks statistics {missing}")
    out = wide[STATS].reset_index()
    out["slug"] = out["input_name"].str.removeprefix("pine_")
    out["strategy"] = out["slug"].map(display_name)
    spent = set(reg.loc[reg["evaluation"].isin(HOLDOUT_EVALS), "input_name"])
    out["holdout_spent"] = out["input_name"].isin(spent)
    out = out.drop(columns="input_name")[["strategy", "slug", *STATS, "holdout_spent"]]
    out = out.sort_values("pnl_p").reset_index(drop=True)

    from strategies.stage3 import admitted_slugs
    meta = {
        "admitted": len(admitted_slugs()),
        "tested": int(len(out)),
        "dev_split": str(v2["date_range"].iloc[-1]),
        "universe_hash": str(v2["universe_hash"].iloc[-1]),
        "last_result_at": str(v2["created_at"].max()),
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "primary_endpoint": "pnl_p: circular-shift null on net P&L, sqrt-law cost model; "
                            "pnl_p_5bps / pnl_p_20bps are flat-cost sensitivity checks",
        "n_perm": 200,
        "bh_q": 0.10,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    out.to_csv(os.path.join(OUT_DIR, "tv_catalog_v2.csv"), index=False)
    with open(os.path.join(OUT_DIR, "tv_catalog_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"wrote {len(out)} strategies; admitted {meta['admitted']}; holdouts spent: "
          f"{int(out['holdout_spent'].sum())}")


if __name__ == "__main__":
    main()
