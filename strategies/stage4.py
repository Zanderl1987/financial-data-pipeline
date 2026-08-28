"""
strategies/stage4.py -- Stage 4 (campaign-wide BH-FDR) for the TV strategy
catalog campaign. See experiments/2026-08-11_tv-strategy-catalog-preregistration.md
section 5: FDR is computed ONCE, across every strategy in the campaign, at
campaign close -- never per batch, and never before every admitted strategy
has a Stage 3 result.

The preregistered stopping rule (section 5) closes the campaign at 50
strategies, or when the sampling frame is exhausted under the 2-per-author
cap, whichever comes first -- and that determination is a call this script
does not make automatically. By default this module only PREVIEWS what
Stage 4 would compute against Stage 3 results collected so far, without
writing anything. The real, once-per-campaign close (run_close(), or the CLI
--confirm-close flag) still hard-fails if any admitted strategy is missing a
Stage 3 result, since a partial campaign closed early would understate m and
invalidate the correction for every other strategy.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation import registry as ev_registry
from evaluation import stats as ev_stats
from strategies.catalog import build_catalog_rows, write_catalog_table

ALPHA = 0.10
EVALUATION_NAME = "tv_strategy_catalog_stage4"
MIN_CAMPAIGN_SIZE = 30  # preregistration section 5 stopping-rule floor


def compute_fdr(catalog: "pd.DataFrame | None" = None) -> pd.DataFrame:
    """BH-FDR at q=ALPHA over every admitted strategy's primary endpoint
    (pnl_p). Strategies without a pnl_p yet are simply excluded from m
    (evaluation.stats.bh_fdr's contract), so this is safe to call as a live
    preview at any point -- but its m (and therefore every p_adj) is only the
    campaign's true, final value once every admitted strategy has run."""
    if catalog is None:
        catalog = build_catalog_rows()
    records = catalog[["strategy_id", "pnl_p"]].rename(columns={"pnl_p": "p"}).to_dict("records")
    return ev_stats.bh_fdr(records, alpha=ALPHA, p_key="p")


def preview(catalog: "pd.DataFrame | None" = None) -> pd.DataFrame:
    if catalog is None:
        catalog = build_catalog_rows()
    n_total = len(catalog)
    n_tested = int(catalog["pnl_p"].notna().sum())
    result = compute_fdr(catalog)
    print(f"{n_tested}/{n_total} admitted strategies have a Stage 3 pnl_p so far.")
    if n_total < MIN_CAMPAIGN_SIZE:
        print(f"NOTE: preregistered stopping rule targets 30-50 strategies "
              f"(section 5); only {n_total} admitted. Closing now needs an "
              f"explicit decision that the sampling frame is exhausted under "
              f"the 2-per-author cap -- not made automatically by this script.")
    cols = ["strategy_id", "p", "p_adj", "reject"]
    return result[cols].sort_values("p", na_position="last")


def _registry_rows(result: pd.DataFrame, run_id: str, created_at: str) -> pd.DataFrame:
    recs = []
    for _, row in result.iterrows():
        if pd.isna(row["p_adj"]):
            continue
        for statistic, value in (("bh_q", row["p_adj"]), ("fdr_pass", bool(row["reject"]))):
            recs.append({
                "run_id": run_id, "input_name": f"pine_{row['strategy_id']}",
                "input_type": "trade_rule", "evaluation": EVALUATION_NAME,
                "horizon": -1, "statistic": statistic, "value": value,
                "n": len(result), "universe_hash": None,
                "date_range": None, "created_at": created_at,
            })
    return pd.DataFrame(recs, columns=ev_registry.COLUMNS)


def run_close(confirm: bool = False) -> pd.DataFrame:
    """The real, once-per-campaign Stage 4 close: computes BH-FDR, logs
    bh_q/fdr_pass to the registry, and writes them into the catalog with
    provisional flipped to False. Refuses unless every admitted strategy
    already carries a Stage 3 pnl_p and confirm=True was passed explicitly."""
    if not confirm:
        raise SystemExit(
            "run_close() requires confirm=True (CLI: --confirm-close). Use "
            "preview() / `python -m strategies.stage4` with no flag to see "
            "what Stage 4 would compute without closing the campaign.")

    catalog = build_catalog_rows()
    missing = catalog[catalog["pnl_p"].isna()]
    if not missing.empty:
        names = ", ".join(missing["strategy_id"].head(5))
        more = "..." if len(missing) > 5 else ""
        raise SystemExit(
            f"{len(missing)} admitted strategies have no Stage 3 result yet "
            f"({names}{more}). Stage 4 may not run until Stage 3 covers every "
            f"admitted strategy.")

    result = compute_fdr(catalog)
    created_at = datetime.now(timezone.utc).isoformat()
    run_id = ev_registry.new_run_id()
    reg_rows = _registry_rows(result, run_id, created_at)
    if not reg_rows.empty:
        ev_registry.append(reg_rows)

    q_map = result.set_index("strategy_id")["p_adj"]
    reject_map = result.set_index("strategy_id")["reject"]
    catalog["bh_q"] = catalog["strategy_id"].map(q_map)
    catalog["fdr_pass"] = catalog["strategy_id"].map(reject_map)
    catalog["provisional"] = False
    catalog["stage"] = "stage4"
    write_catalog_table(catalog)
    return catalog


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--confirm-close", action="store_true",
                    help="Actually close the campaign and persist bh_q/fdr_pass. "
                         "Without this flag, only prints a preview.")
    args = ap.parse_args()
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    if args.confirm_close:
        out = run_close(confirm=True)
        print(out[["strategy_id", "pnl_p", "bh_q", "fdr_pass"]].sort_values("pnl_p"))
    else:
        print(preview(), end="\n\n")
