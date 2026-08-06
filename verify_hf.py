"""
verify_hf.py — post-push health check for the HF financial-fundamentals dataset.

Re-pulls every snapshot file straight from Hugging Face (not the local cache)
and sanity-checks the assembled dataset produced by
build_fundamentals_dataset.py: row counts, fetched_at recency, symbol coverage,
duplicate rates, cross-file coherence against snapshot.json.

The dataset is the Option-D snapshot (approved 2026-08-05): long atomic
`facts.parquet` + masters (`companies`, `filings`) + wide latest-filing-wins
tables under the legacy `financials_*_latest.parquet` filenames + a `metrics`
reference. All files in one revision are built by one run of the build script,
so their counts must match snapshot.json (one-coherent-revision rule).

Run after `build_fundamentals_dataset.py` pushes, then follow with a full
`validate.py` sweep. Exits non-zero if the dataset looks wrong.

Usage:
  C:\\ProgramData\\anaconda3\\python.exe verify_hf.py [--repo owner/financial-fundamentals]
"""

import argparse
import datetime
import json
import os
import sys
import tempfile

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

FILES = [
    ("facts",               "facts.parquet",                       "long"),
    ("companies",           "companies.parquet",                   "master"),
    ("filings",             "filings.parquet",                     "master"),
    ("annual latest",       "financials_annual_latest.parquet",    "wide"),
    ("quarterly latest",    "financials_quarterly_latest.parquet", "wide"),
    ("metrics",             "metrics.parquet",                     "reference"),
]

# Baselines set from the 2026-08-04 full-market curated state (pre-IFRS; they
# grow once foreign issuers land). Flag only if a revision dropped well below.
EXPECTED_MIN_ROWS = {
    "facts": 4_000_000,
    "companies": 10_000,
    "filings": 1_000,
    "annual latest": 50_000,
    "quarterly latest": 100_000,
    "metrics": 10,
}

# Stale if the newest fetched_at is older than this many days.
STALE_DAYS = 7

# Fail if collapsed duplicates exceed this share of the union (facts table).
MAX_DUP_RATE = 0.10


def _check_long(df: pd.DataFrame, label: str, args) -> bool:
    ok = True
    n = len(df)
    print(f"  rows:       {n:,}")
    print(f"  symbols:    {df['symbol'].nunique():,}")

    if "fetched_at" in df.columns:
        fa = pd.to_datetime(df["fetched_at"], errors="coerce")
        newest = fa.max()
        oldest = fa.min()
        print(f"  fetched_at: {oldest:%Y-%m-%d} .. {newest:%Y-%m-%d}  (UTC)")
        if pd.isna(newest) or (datetime.datetime.utcnow() - newest.to_pydatetime()) > datetime.timedelta(days=STALE_DAYS):
            print(f"  ! Stale: newest fetched_at older than {STALE_DAYS} days.")
            ok = False
        dup_cols = [c for c in df.columns if c != "fetched_at"]
        if dup_cols:
            collapsed = df.drop_duplicates(subset=dup_cols).shape[0]
            dup_rate = 1.0 - collapsed / n if n else 0.0
            print(f"  dup rate:   {dup_rate:.1%}  (all-but-fetched_at key)")
            if dup_rate > args.max_dup_rate:
                print(f"  ! Duplicate rate {dup_rate:.1%} exceeds {args.max_dup_rate:.0%} - unexpected.")
                ok = False
    else:
        print("  ! No fetched_at column.")
        ok = False

    if "taxonomy" in df.columns and not df["taxonomy"].dropna().empty:
        print(f"  taxonomy:   {', '.join(f'{t}:{int(c):,}' for t, c in df['taxonomy'].value_counts().items())}")
    return ok


def _check_wide(df: pd.DataFrame, label: str) -> bool:
    ok = True
    n = len(df)
    print(f"  rows:       {n:,}")
    print(f"  symbols:    {df['symbol'].nunique():,}")
    if {"symbol", "period_end"}.issubset(df.columns):
        dups = df.duplicated(subset=["symbol", "period_end"]).sum()
        print(f"  dup keys:   {dups:,}  ((symbol, period_end))")
        if dups:
            print("  ! Duplicate (symbol, period_end) rows - latest-filing-wins violated.")
            ok = False
    metric_cols = [c for c in df.columns if c in (
        "revenue", "net_income", "eps_diluted", "eps_basic", "gross_profit",
        "operating_income", "total_assets", "total_liabilities",
        "operating_cash_flow", "shares_outstanding")]
    if metric_cols:
        with_any = df[metric_cols].notna().any(axis=1).sum()
        print(f"  metrics:    {len(metric_cols)} columns, {with_any:,} rows with >=1 value")
    return ok


def _check_master(df: pd.DataFrame, label: str, key, allow_null_key: bool = False) -> bool:
    """key may be a column name or a list of columns (composite natural key)."""
    ok = True
    n = len(df)
    print(f"  rows:       {n:,}")
    keys = [key] if isinstance(key, str) else list(key)
    if keys[0] in df.columns:
        null_rate = df[keys[0]].isna().mean()
        print(f"  null {keys[0]}:  {null_rate:.1%}")
        if not allow_null_key and null_rate > 0.5:
            print(f"  ! {keys[0]} mostly null - assembly looks wrong.")
            ok = False
        if df[keys[0]].notna().any() and all(k in df.columns for k in keys):
            dups = int(df.duplicated(subset=keys, keep=False).sum())
            if dups:
                print(f"  ! Duplicate {keys}: {dups:,} rows.")
                ok = False
    return ok


def main():
    parser = argparse.ArgumentParser(description="Verify the HF financial-fundamentals dataset after a push.")
    parser.add_argument("--repo", type=str, default=os.environ.get("HF_DATASET_REPO", ""))
    parser.add_argument("--max-dup-rate", type=float, default=MAX_DUP_RATE,
                        help="Fail if dedup collapse rate exceeds this (default: 0.10).")
    parser.add_argument("--no-min-rows", action="store_true",
                        help="Skip the row-count baseline checks (for smoke tests on scratch repos).")
    args = parser.parse_args()

    if not args.repo:
        print("No --repo and no HF_DATASET_REPO in .env. Nothing to verify.")
        return 2
    if not os.environ.get("HF_TOKEN"):
        print("HF_TOKEN not set in .env. Skipping verify.")
        return 2

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("huggingface_hub not installed. Run: pip install huggingface_hub")
        return 2

    tmpdir = tempfile.mkdtemp(prefix="hf_verify_")
    ok = True
    counts = {}

    for label, filename, kind in FILES:
        print(f"= {label} ({filename})")
        try:
            path = hf_hub_download(
                repo_id=args.repo,
                filename=filename,
                repo_type="dataset",
                token=os.environ.get("HF_TOKEN"),
                local_dir=tmpdir,
                force_download=True,
            )
        except Exception as e:
            print(f"  ! Download FAILED: {e}")
            ok = False
            continue

        df = pd.read_parquet(path)
        counts[label] = len(df)

        min_rows = EXPECTED_MIN_ROWS.get(label)
        if min_rows and not args.no_min_rows and len(df) < min_rows:
            print(f"  ! Row count below expected baseline ({min_rows:,}) - possible bad push.")
            ok = False

        if kind == "long":
            ok = _check_long(df, label, args) and ok
        elif kind == "wide":
            ok = _check_wide(df, label) and ok
        elif kind == "master":
            key = ["accession_number", "period", "fiscal_year", "fiscal_period"] if label == "filings" else "cik"
            ok = _check_master(df, label, key) and ok
        elif kind == "reference":
            print(f"  rows:       {len(df):,}")
            print(f"  metrics:    {', '.join(df['metric'].astype(str)) if 'metric' in df.columns else 'n/a'}")

    # Cross-file coherence: snapshot.json must match the actual file row counts.
    print("\n= coherence (snapshot.json)")
    try:
        snap_path = hf_hub_download(
            repo_id=args.repo,
            filename="snapshot.json",
            repo_type="dataset",
            token=os.environ.get("HF_TOKEN"),
            local_dir=tmpdir,
            force_download=True,
        )
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
        for label, fname, _ in FILES:
            if label == "annual latest":
                key = "annual_latest_rows"
            elif label == "quarterly latest":
                key = "quarterly_latest_rows"
            else:
                key = f"{label}_rows"
            reported = snap.get(key)
            if reported is not None and label in counts:
                match = "OK" if int(reported) == counts[label] else "MISMATCH"
                print(f"  {label}: snapshot.json={int(reported):,} actual={counts[label]:,}  {match}")
                if match != "OK":
                    ok = False
            elif reported is None:
                print(f"  {label}: no count recorded in snapshot.json")
    except Exception as e:
        print(f"  ! Could not load snapshot.json for coherence check: {e}")

    print(f"\nTOTAL rows on HF: {sum(counts.values()):,}")
    if ok:
        print("VERIFY PASS")
        return 0
    print("VERIFY FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
