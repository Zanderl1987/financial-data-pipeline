"""
strategies/stage5.py -- Stage 5 (one-shot holdout test) for the TV strategy
catalog campaign. See experiments/2026-08-11_tv-strategy-catalog-preregistration.md
sections 2 and 5: only Stage 4 survivors (fdr_pass=True) are tested, once,
on the holdout split -- symbols with sha256(symbol) % 4 == 0, restricted to
2018-01-01 onward -- using the identical rule and cost model as Stage 3.
Pre-declared success: pnl_p < 0.05 on holdout.

Amendment 2026-10-28: the null is evaluation.stats.circular_shift_trades, rows go
under tv_strategy_catalog_stage5_v2, success also needs holdout net P&L > 0, and
only Stage 4 survivors with dev net P&L > 0 and no null-exposure mismatch are
eligible. A strategy whose holdout was opened under the voided endpoint (any row
under the old tv_strategy_catalog_stage5 name) stays spent: already_run() checks
both names, per section 2's "a second holdout run ... invalidates it".

"Once" is enforced here, not just documented: before testing a strategy,
this module checks the registry for an existing tv_strategy_catalog_stage5
row for that strategy_id and refuses to run again if one exists (per section
2, "a second holdout run on the same strategy invalidates it for this
campaign" -- there is deliberately no --force override).

Reuses strategies.stage3's rule loading, price-floor gate, and cost-model
monkeypatch rather than duplicating them, so the holdout test is mechanically
identical to the development test in every way except the data split.

Survivorship amendment: a strategy must also have passed the re-score in
strategies/survivorship.py, and it is scored on every holdout-split CRSP company,
alive or dead, under that module's rules. Success then needs p < 0.05 and net
P&L > 0 in both the primary and the wipeout scenario. holdout_cache() stays on
the Yahoo panel because strategies/portfolio.py reads it; it no longer feeds this
test.
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import query as q
from analytics.technical import _load_ohlcv
from evaluation import execution as ev_execution
from evaluation import registry as ev_registry
from evaluation.universe import clean_symbols
from strategies import survivorship as sv
from strategies.catalog import build_catalog_rows, write_catalog_table
from strategies.stage3 import (
    PRICE_TABLE,
    SEED,
    dev_holdout_symbols,
    load_rule_for,
)

N_PERM = sv.N_PERM
HOLDOUT_START = sv.HOLDOUT_START
EVALUATION_NAME = "tv_strategy_catalog_stage5_v2"
#: Every evaluation name under which a holdout counts as opened. The voided
#: pre-amendment name stays here forever -- those holdouts were seen.
HOLDOUT_EVALUATION_NAMES = ("tv_strategy_catalog_stage5", EVALUATION_NAME)
SUCCESS_P = 0.05


# --------------------------------------------------------------- holdout cache

def build_holdout_cache(symbols: "list[str]", price_table: str = PRICE_TABLE) -> dict:
    """The 25% symbol-holdout, restricted to 2018+ -- data no Stage 3 run has
    ever seen (Stage 3's dev_cache() truncates these same symbols at
    DEV_END=2017-12-31, see stage3.py)."""
    _dev_symbols, holdout_symbols = dev_holdout_symbols(symbols)
    cache = {}
    for sym in holdout_symbols:
        df = _load_ohlcv(sym, price_table, start=HOLDOUT_START, end=None)
        if df is not None and not df.empty:
            cache[sym] = df
    return cache


_CACHE_SINGLETON: dict = {}


def holdout_cache(price_table: str = PRICE_TABLE) -> dict:
    if price_table not in _CACHE_SINGLETON:
        symbols = q.symbols(price_table)
        symbols = clean_symbols(symbols, price_table=price_table)
        _CACHE_SINGLETON[price_table] = build_holdout_cache(symbols, price_table)
    return _CACHE_SINGLETON[price_table]


def stage3_ranges() -> dict:
    return sv.stage3_ranges(q, PRICE_TABLE)


def crsp_holdout_panel() -> "sv.CrspPanel":
    """Every holdout-split CRSP company. Its prices are requested only when Stage 5 runs."""
    return sv.load_holdout_panel(stage3_ranges())


# --------------------------------------------------------------- one-shot guard

def already_run(strategy_id: str) -> bool:
    reg = ev_registry.load()
    sub = reg[reg["evaluation"].isin(HOLDOUT_EVALUATION_NAMES)
              & (reg["input_name"] == f"pine_{strategy_id}")]
    return not sub.empty


def rescore_passed(strategy_id: str) -> bool:
    """Passed the dead-company re-score, with no failing row anywhere: failure is final,
    so a later row cannot rescue a strategy."""
    reg = ev_registry.load()
    sub = reg[(reg["evaluation"] == sv.EVALUATION_NAME) & (reg["input_name"] == f"pine_{strategy_id}")
              & (reg["statistic"] == "rescore_pass")]
    return bool(not sub.empty and (pd.to_numeric(sub["value"]) == 1.0).all())


# --------------------------------------------------------------- stage 4 survivors

def stage4_survivors() -> "list[str]":
    """Strategies eligible for the holdout: fdr_pass=True in the current
    catalog snapshot (only valid once strategies.stage4.run_close() has
    actually closed the campaign), development net P&L > 0, and no
    null-exposure mismatch (amendment 2026-10-28). An empty/partial catalog
    just yields an empty list, not an error, since that's a legitimate
    ("nothing to test yet") state."""
    catalog = build_catalog_rows()
    needed = {"fdr_pass", "total_pnl_net", "exposure_mismatch"}
    if not needed.issubset(catalog.columns):
        return []
    pnl = pd.to_numeric(catalog["total_pnl_net"], errors="coerce")
    survivors = catalog[(catalog["fdr_pass"] == True)  # noqa: E712
                        & (pnl > 0)
                        & (catalog["exposure_mismatch"] == False)]  # noqa: E712
    return sorted(survivors["strategy_id"].tolist())


# --------------------------------------------------------------- per-strategy run

def run_holdout_for(slug: str, cache, n_perm: int = N_PERM, seed: int = SEED, workers: int = 2) -> dict:
    """`cache` is the crsp_holdout_panel(). The rule is left unfloored because the panel
    scoring applies the floor to the real traded price."""
    if already_run(slug):
        raise RuntimeError(
            f"{slug} already has a Stage 5 holdout result -- a second run "
            f"would invalidate it for this campaign (section 2). Refusing.")
    if not rescore_passed(slug):
        raise RuntimeError(f"{slug} has not passed the dead-company re-score. Refusing.")

    rule, translation_verified = load_rule_for(slug)
    cfg = sv.crsp_config()
    with ProcessPoolExecutor(max_workers=workers) as executor:
        result = sv.score_panel({slug: rule}, cache, n_perm=n_perm, seed=seed, config=cfg,
                                executor=executor, n_workers=workers, rule_loader=sv.catalog_rule)[slug]

    primary, wipeout = result[sv.PRIMARY_SCENARIO], result[sv.WIPEOUT_SCENARIO]
    run_ts = datetime.now(timezone.utc).isoformat()
    return {
        "strategy_id": slug,
        "translation_verified": translation_verified,
        "holdout_n_trades": primary.get("n_trades", 0),
        "holdout_total_pnl_net": primary.get("obs_pnl_dollars"),
        "holdout_pnl_p": primary.get("pnl_p"),
        "holdout_null_exposure_ratio": primary.get("null_exposure_ratio"),
        "holdout_wipeout_pnl_p": wipeout.get("pnl_p"),
        "holdout_wipeout_total_pnl_net": wipeout.get("obs_pnl_dollars"),
        "holdout_run_ts": run_ts,
        "holdout_success": sv.holdout_success(result),
    }


def _registry_rows(row: dict, run_id: str, universe_hash: str) -> pd.DataFrame:
    stats = {"holdout_pnl_p": row["holdout_pnl_p"],
             "holdout_n_trades": row["holdout_n_trades"],
             "holdout_total_pnl_net": row.get("holdout_total_pnl_net"),
             "holdout_null_exposure_ratio": row.get("holdout_null_exposure_ratio"),
             "holdout_wipeout_pnl_p": row.get("holdout_wipeout_pnl_p"),
             "holdout_wipeout_total_pnl_net": row.get("holdout_wipeout_total_pnl_net"),
             "holdout_success": row["holdout_success"]}
    recs = []
    for statistic, value in stats.items():
        if value is None:
            continue
        recs.append({
            "run_id": run_id, "input_name": f"pine_{row['strategy_id']}",
            "input_type": "trade_rule", "evaluation": EVALUATION_NAME,
            "horizon": -1, "statistic": statistic, "value": value,
            "n": row["holdout_n_trades"], "universe_hash": universe_hash,
            "date_range": f"{HOLDOUT_START}..", "created_at": row["holdout_run_ts"],
            "execution_hash": ev_execution.config_hash(sv.crsp_config()),
        })
    return pd.DataFrame(recs, columns=ev_registry.COLUMNS)


# --------------------------------------------------------------- main

def run_all(confirm: bool = False, n_perm: int = N_PERM, seed: int = SEED) -> pd.DataFrame:
    survivors = stage4_survivors()
    passed = [s for s in survivors if rescore_passed(s)]
    to_run = [s for s in passed if not already_run(s)]

    if not confirm:
        print(f"{len(survivors)} Stage 4 survivor(s): {survivors}")
        print(f"{len(passed)} also passed the dead-company re-score: {passed}")
        print(f"{len(to_run)} not yet holdout-tested: {to_run}")
        print("Pass confirm=True (CLI: --confirm-run) to actually run the "
              "one-shot holdout test on these.")
        return pd.DataFrame(columns=["strategy_id", "holdout_pnl_p", "holdout_success"])

    if not to_run:
        print("No untested Stage 4 survivors to run.")
        return pd.DataFrame(columns=["strategy_id", "holdout_pnl_p", "holdout_success"])

    cache = crsp_holdout_panel()
    uhash = ev_registry.universe_hash(cache.keys())
    run_id = ev_registry.new_run_id()

    rows = []
    for slug in to_run:
        print(f"{slug} -- running holdout test", flush=True)
        row = run_holdout_for(slug, cache, n_perm=n_perm, seed=seed)
        rows.append(row)
        reg_rows = _registry_rows(row, run_id, uhash)
        if not reg_rows.empty:
            ev_registry.append(reg_rows)
        print(f"{slug} -- holdout pnl_p={row['holdout_pnl_p']} "
              f"success={row['holdout_success']}", flush=True)

    result_df = pd.DataFrame(rows)

    catalog = build_catalog_rows()
    p_map = result_df.set_index("strategy_id")["holdout_pnl_p"]
    ts_map = result_df.set_index("strategy_id")["holdout_run_ts"]
    mask = catalog["strategy_id"].isin(result_df["strategy_id"])
    catalog.loc[mask, "holdout_pnl_p"] = catalog.loc[mask, "strategy_id"].map(p_map)
    catalog.loc[mask, "holdout_run_ts"] = catalog.loc[mask, "strategy_id"].map(ts_map)
    catalog.loc[mask, "stage"] = "stage5"
    write_catalog_table(catalog)

    return result_df


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--confirm-run", action="store_true",
                    help="Actually run the one-shot holdout test on untested "
                         "Stage 4 survivors. Without this flag, only previews "
                         "who would be tested.")
    ap.add_argument("--n-perm", type=int, default=N_PERM)
    args = ap.parse_args()
    out = run_all(confirm=args.confirm_run, n_perm=args.n_perm)
    if not out.empty:
        pd.set_option("display.max_columns", None)
        pd.set_option("display.width", 200)
        print(out[["strategy_id", "holdout_pnl_p", "holdout_success"]])
