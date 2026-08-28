#!/usr/bin/env python3
"""
CFPB Consumer Complaint Database Pipeline.

No API key required. CFPB publishes the entire complaint database as a single
always-current bulk CSV (regenerated daily), which is the intended way to get
full history -- not the paginated search API (which caps deep pagination and
would need ~17M rows / 100-per-page = far too many requests for a full pull).

Source: https://files.consumerfinance.gov/ccdb/complaints.csv.zip (~1.4 GB
compressed as of 2026-08, ~17.3M complaints back to 2011-12-01).

Rebuilt 2026-08-24: an earlier `cfpb_complaints` table existed on this
project's published HuggingFace dataset (6.7M rows) with no corresponding
pipeline file or git history anywhere in this repo -- the source code that
produced it was apparently never committed. This is a from-scratch rebuild,
not a restoration of the original code.

Because the source is one full snapshot file (not incremental), every run
re-downloads and re-writes the whole thing -- there is no meaningful
--backfill vs incremental distinction, matching the fao_pipeline.py bulk-ZIP
fallback pattern. The file is streamed in chunks (pandas chunksize) rather
than loaded whole, since 17M+ rows as one DataFrame risks exhausting memory;
each chunk is written as its own partitioned parquet file so the existing
glob-based catalog/dedup layer handles reassembly.

Outputs:
  storage/raw/cfpb/complaints/cfpb_complaints_{mode}_{YYYYMMDD}_part{NNNN}.parquet

CLI:
  python cfpb_complaints_pipeline.py             # full snapshot (only mode that exists)
  python cfpb_complaints_pipeline.py --backfill  # same as above; flag kept for CLI consistency
"""

import argparse
import datetime
import os
import re
import tempfile
import zipfile

import pandas as pd
import requests
from storage_utils import write_partitioned

BULK_URL = "https://files.consumerfinance.gov/ccdb/complaints.csv.zip"
BASE_DIR = os.path.join("storage", "raw", "cfpb")
COMPLAINTS_DIR = os.path.join(BASE_DIR, "complaints")
CHUNK_ROWS = 250_000
DOWNLOAD_TIMEOUT = 900


def _snake(col: str) -> str:
    col = col.strip().lower().replace("-", "_").replace(" ", "_")
    return re.sub(r"[^a-z0-9_]", "", col)


def download_bulk_zip() -> "str | None":
    print(f"  Downloading {BULK_URL} ...")
    try:
        r = requests.get(BULK_URL, stream=True, timeout=DOWNLOAD_TIMEOUT)
    except requests.RequestException as e:
        print(f"  Request error: {e}")
        return None
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}")
        return None
    total = int(r.headers.get("content-length", 0))
    print(f"  {total/1e6:.1f} MB to download...")
    fd, path = tempfile.mkstemp(suffix=".zip")
    written = 0
    with os.fdopen(fd, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
            written += len(chunk)
    print(f"  Downloaded {written/1e6:.1f} MB -> {path}")
    return path


def main():
    parser = argparse.ArgumentParser(description="CFPB consumer complaints pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="No-op (source is always the full current snapshot) -- kept for CLI consistency")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"

    print(f"CFPB Complaints Pipeline  mode={mode}\n")
    os.makedirs(COMPLAINTS_DIR, exist_ok=True)

    zip_path = download_bulk_zip()
    if zip_path is None:
        print("  Download failed.")
        return

    try:
        with zipfile.ZipFile(zip_path) as zf:
            csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
            if not csv_names:
                print("  No CSV found inside the ZIP.")
                return
            csv_name = max(csv_names, key=lambda n: zf.getinfo(n).file_size)
            print(f"  Reading {csv_name} in {CHUNK_ROWS:,}-row chunks...")

            total_rows = 0
            part = 0
            with zf.open(csv_name) as f:
                for chunk in pd.read_csv(f, chunksize=CHUNK_ROWS, low_memory=False, dtype=str):
                    chunk.columns = [_snake(c) for c in chunk.columns]
                    chunk["fetched_at"] = now.isoformat()
                    path = write_partitioned(
                        chunk, COMPLAINTS_DIR,
                        f"cfpb_complaints_{mode}_{today_str}_part{part:04d}.parquet",
                    )
                    total_rows += len(chunk)
                    part += 1
                    if part % 10 == 0:
                        print(f"    ...{total_rows:,} rows written so far ({part} parts)")

            print(f"  -> {part} parquet parts, {total_rows:,} total rows")
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass

    print("\n--- CFPB COMPLAINTS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
