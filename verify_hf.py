"""
verify_hf.py — post-append health check for the HF financial-fundamentals dataset.

Re-pulls both latest parquet files straight from Hugging Face (not the local
cache) and sanity-checks the append result: row counts, fetched_at recency,
per-symbol coverage, and the duplicate rate under the pipeline's dedup key
(all columns except fetched_at, keep newest).

Run after any `fundamentals_pipeline.py` run that pushes to HF, then follow
with a full `validate.py` sweep. Exits non-zero if the dataset looks wrong.

Usage:
  C:\\ProgramData\\anaconda3\\python.exe verify_hf.py [--repo owner/financial-fundamentals]
"""

import argparse
import datetime
import os
import sys
import tempfile

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

FILES = [
    ("annual", "fundamentals_annual_latest.parquet"),
    ("quarterly", "fundamentals_quarterly_latest.parquet"),
]

# Post-dedup baselines (2026-08-04 cleanup: re-fetch dups collapsed). These grow
# on future appends, so flag only if a run dropped well below the current size.
EXPECTED_MIN_ROWS = {
    "annual": 2_500_000,
    "quarterly": 5_500_000,
}

# Stale if the newest fetched_at is older than this many days.
STALE_DAYS = 7

# Fail if collapsed duplicates exceed this share of the union.
MAX_DUP_RATE = 0.10


def main():
    parser = argparse.ArgumentParser(description="Verify the HF financial-fundamentals dataset after an append.")
    parser.add_argument("--repo", type=str, default=os.environ.get("HF_DATASET_REPO", ""))
    parser.add_argument("--max-dup-rate", type=float, default=MAX_DUP_RATE,
                        help="Fail if dedup collapse rate exceeds this (default: 0.10).")
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
    total_rows = 0

    for label, filename in FILES:
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
        n = len(df)
        total_rows += n
        print(f"  rows:       {n:,}")

        min_rows = EXPECTED_MIN_ROWS.get(label)
        if min_rows and n < min_rows:
            print(f"  ! Row count below expected baseline ({min_rows:,}) - possible bad append.")
            ok = False

        if "fetched_at" in df.columns:
            fa = pd.to_datetime(df["fetched_at"], errors="coerce")
            newest = fa.max()
            oldest = fa.min()
            print(f"  fetched_at: {oldest:%Y-%m-%d} .. {newest:%Y-%m-%d}  (UTC)")
            if pd.isna(newest) or (datetime.datetime.utcnow() - newest.to_pydatetime()) > datetime.timedelta(days=STALE_DAYS):
                print(f"  ! Stale: newest fetched_at older than {STALE_DAYS} days.")
                ok = False
        else:
            print("  ! No fetched_at column.")
            ok = False

        if "symbol" in df.columns:
            print(f"  symbols:    {df['symbol'].nunique():,}")

        if "fetched_at" in df.columns:
            dup_cols = [c for c in df.columns if c != "fetched_at"]
            if dup_cols:
                before = len(df)
                collapsed = df.drop_duplicates(subset=dup_cols).shape[0]
                dup_rate = 1.0 - collapsed / before if before else 0.0
                print(f"  dup rate:   {dup_rate:.1%}  (all-but-fetched_at key)")
                if dup_rate > args.max_dup_rate:
                    print(f"  ! Duplicate rate {dup_rate:.1%} exceeds {args.max_dup_rate:.0%} - unexpected.")
                    ok = False

    print(f"\nTOTAL rows on HF: {total_rows:,}")
    if ok:
        print("VERIFY PASS")
        return 0
    print("VERIFY FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
