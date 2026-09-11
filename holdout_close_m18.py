#!/usr/bin/env python3
"""Stage 5 holdout -- one-shot run for the closed m=18 family.

Mirrors strategies.stage5.run_all() but with the survivor list hardcoded,
because the family was closed under a provisional (document-only) Stage 4
recompute: stage5.stage4_survivors() reads the catalog fdr_pass column, which
never exists without a real run_close (impossible: 781 admitted lack Stage 3).

Closed family decision recorded in work-notes SESSION_NOTES_2026-09-07.md;
user approved close + holdout run for:
    bollinger_bands_simple, optimized_doji_breakout_short, rsi_bb_inside_strategy

Per preregistration section 5 this is the one-shot test on the 25% symbol
holdout, 2018+, identical rule + cost model. Registry rows are written so a
second run raises (already_run guard in stage5.run_holdout_for). n_perm=100
mirrors the executed Stage 3 full runs (prereg stated 200; the campaign ran
at 100 for runtime under the Windows permutation constraints -- recorded).
"""
from __future__ import annotations

import sys

sys.path.insert(0, r"C:\Users\zande\PycharmProjects\financial-data-pipeline")

import pandas as pd

from strategies.stage3 import N_PERM, SEED
from strategies.stage5 import (
    EVALUATION_NAME,
    build_holdout_cache,
    holdout_cache,
    run_holdout_for,
    _registry_rows,
)
from evaluation import registry as ev_registry, universe as ev_universe
import query as q

SURVIVORS = [
    "bollinger_bands_simple",
    "optimized_doji_breakout_short",
    "rsi_bb_inside_strategy",
]

N_PERM_USE = 100


def main() -> None:
    symbols = q.symbols("yfinance_universe_prices")
    symbols = ev_universe.clean_symbols(symbols, price_table="yfinance_universe_prices")
    from strategies.stage3 import dev_holdout_symbols
    _dev, holdout = dev_holdout_symbols(symbols)
    print(f"Holdout symbols (expected): {len(holdout)}", flush=True)

    cache = holdout_cache()
    print(f"Holdout cache symbols with 2018+ data: {len(cache)}", flush=True)

    for slug in SURVIVORS:
        if _already_run(slug):
            raise RuntimeError(
                f"{slug} already has a tv_strategy_catalog_stage5 row -- "
                f"a second holdout run would invalidate it for this campaign. "
                f"Refusing (no --force).")

    uhash = ev_registry.universe_hash(cache.keys())
    run_id = ev_registry.new_run_id()
    rows = []
    for slug in SURVIVORS:
        print(f"--- {slug}: running one-shot holdout (n_perm={N_PERM_USE}) ---", flush=True)
        row = run_holdout_for(slug, cache, n_perm=N_PERM_USE, seed=SEED)
        rows.append(row)
        reg_rows = _registry_rows(row, run_id, uhash)
        if not reg_rows.empty:
            n_written = ev_registry.append(reg_rows)
            print(f"wrote {n_written} registry row(s) for {slug}", flush=True)
        print(f"    holdout pnl_p={row['holdout_pnl_p']} "
              f"n_trades={row['holdout_n_trades']} "
              f"success={row['holdout_success']}", flush=True)

    result_df = pd.DataFrame(rows)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    print("\nRESULTS", flush=True)
    print(result_df[["strategy_id", "holdout_pnl_p", "holdout_n_trades",
                     "holdout_total_pnl_net", "holdout_success"]], flush=True)


def _already_run(slug: str) -> bool:
    reg = ev_registry.load()
    sub = reg[(reg["evaluation"] == EVALUATION_NAME)
              & (reg["input_name"] == f"pine_{slug}")]
    return not sub.empty


if __name__ == "__main__":
    main()