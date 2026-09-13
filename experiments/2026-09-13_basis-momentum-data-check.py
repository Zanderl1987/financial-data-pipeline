#!/usr/bin/env python3
"""
Basis-momentum data-availability check (TASKS.md item: "Verify a buildable
proxy exists [for multi-maturity futures curves] before scoping; if no proxy,
document as data-blocked and move on").

CONCLUSION (documented, deterministic): data-blocked on this store. The
`futures` table is yfinance-style CONTINUOUS FRONT-MONTH series only
(44 symbols, `CL=F`-style, no contract code / expiry). Hands on evidence:
raw schema carries date/symbol/open..close/volume and two Hive partition
columns (`year`,`month` = FETCH time); the only `month`-named column in the
whole table is that fetch partition, NOT contract maturity. No other curated
table exposes a term structure (EIA petroleum products are front-month series,
2 stale-2024 products; COT is positioning; omkar_commodity has NO data;
market_history indices are cash). The ONE curve-capable free data source is
the Cboe VIX-futures daily settlement CSV (2004+, keyless, verified live
2026-09-12 per docs/OPTIONS_DATA_SOURCES.md) -- a genuine multi-maturity term
structure of VOL futures (VX/VXM), which supports a VIX roll-yield signal, NOT
cross-commodity basis momentum.

Run: C:\\ProgramData\\anaconda3\\python.exe experiments/2026-09-13_basis-momentum-data-check.py
"""
import glob
import json
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = "storage/reports/eval"
FUTURES_GLOB = "storage/raw/futures/**/*.parquet"
CURATED_GLOBS = {
    "futures": "storage/curated/futures/*.parquet",
    "omkar_commodity": "storage/curated/omkar_commodity/*.parquet",
    "eia_petroleum_futures": "storage/curated/eia_petroleum_futures/*.parquet",
    "cot": "storage/curated/cot/*.parquet",
}


def read(label, pattern, limit=None):
    if not glob.glob(pattern):
        return {"label": label, "ok": False, "reason": "no files"}
    try:
        cols = [c[0] for c in duckdb.sql(
            f"describe select * from read_parquet('{pattern}')").fetchall()]
        n = duckdb.sql(f"select count(*) from read_parquet('{pattern}')").fetchone()[0]
        return {"label": label, "ok": True, "rows": int(n), "columns": cols}
    except Exception as e:
        return {"label": label, "ok": False, "reason": str(e)}


def main() -> None:
    findings = {}

    raw_cols = [c[0] for c in duckdb.sql(
        f"describe select * from read_parquet('{FUTURES_GLOB}')").fetchall()]
    syms = duckdb.sql(
        f"select distinct symbol from read_parquet('{FUTURES_GLOB}')").fetchall()
    maturity_like = [c for c in raw_cols if any(
        k in c.lower() for k in
        ("contract", "expiry", "expiration", "maturity", "code"))]
    findings["futures_raw"] = {
        "n_files": len(glob.glob(FUTURES_GLOB, recursive=True)),
        "columns": raw_cols,
        "n_symbols": int(len(syms)),
        "symbol_sample": sorted(s for (s,) in syms)[:8],
        "maturity_like_columns": maturity_like,
        "all_symbols_end_in_=F": all(s.upper().endswith("=F") for (s,) in syms),
    }

    for label, pattern in CURATED_GLOBS.items():
        findings[label] = read(label, pattern)

    print(json.dumps(findings, indent=2))

    verdict = (
        "DATA-BLOCKED: the futures store is continuous front-month only "
        "(`*_=F` symbols, no contract/expiry identifiers anywhere). Basis "
        "momentum requires multi-maturity curves (near vs deferred rolls); "
        "no such table exists. Only curve-capable free source: Cboe VX/VXM "
        "daily settlement CSVs (2004+) -> a VIX-futures roll-yield signal, "
        "NOT cross-commodity basis momentum. Document as data-blocked; "
        "optional future reopen = same paid gate as the equity delisting "
        "panel (curve-grade futures data / Barchart-CME-class)."
    )
    print("\nVERDICT:", verdict)
    findings["verdict"] = verdict

    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, "basis_momentum_data_check_20260913.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(findings, fh, indent=2)
    print(f"\nwrote {p}")


if __name__ == "__main__":
    main()