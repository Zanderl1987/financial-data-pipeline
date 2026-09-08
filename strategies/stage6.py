"""
strategies/stage6.py -- Stage 6 (catalog verdict labeling) for the TV strategy
catalog campaign. See experiments/2026-08-11_tv-strategy-catalog-preregistration.md
section 5, the final promotion rule: "Only Stage 5 survivors may be described
as promising. Everything else is a recorded null result."

Stage 6 is deliberately cheap and non-destructive: it reads the current
catalog (rebuilt from the eval registry, so it always carries each strategy's
stage + holdout_pnl_p) and attaches the derived `verdict` column -- "promising"
for Stage 5 survivors (holdout_pnl_p < 0.05, the pre-declared success bar),
"undetermined" for everything else (see strategies.catalog.label_verdict's
docstring for why "undetermined" rather than "null result").

The verdict is DERIVED from evidence already locked in the registry -- it never
re-runs any test, so there is nothing one-shot about it and no confirm gate is
needed for re-runs. By default this module only PREVIEWS what the verdict table
would be; pass --apply to rebuild and persist the catalog snapshot with the
verdict column. This stage is safe to re-run: applying again just re-derives
the same labels from the same registry rows.
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.catalog import build_catalog_rows, write_catalog_table


def preview() -> pd.DataFrame:
    """Rebuild the catalog and return the verdict summary rows (no write)."""
    catalog = build_catalog_rows()
    cols = ["strategy_id", "stage", "verdict", "pnl_p",
            "holdout_pnl_p", "provisional"]
    return catalog[cols].sort_values(
        ["verdict", "strategy_id"], kind="stable", ascending=[False, True])


def apply() -> pd.DataFrame:
    """Rebuild the catalog with the verdict column and persist it.
    Re-runnable: same registry rows -> same verdicts."""
    catalog = build_catalog_rows()
    write_catalog_table(catalog)
    return preview()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Rebuild and persist the catalog with the verdict "
                         "column. Without this flag, only previews the "
                         "verdict table.")
    args = ap.parse_args()
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    out = apply() if args.apply else preview()
    if not out.empty:
        print(out.to_string(index=False))
        print()
        print(out["verdict"].value_counts().to_string())