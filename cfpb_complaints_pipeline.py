#!/usr/bin/env python3
"""
CFPB Consumer Finance Complaints Pipeline.

Downloads consumer complaint data directly from the CFPB's public bulk-export
CSV (complaints.csv.zip). Provides narrative complaint text + metadata for
consumer finance sentiment analysis, company-level complaint tracking, and
alternative risk signals.

Source: https://www.consumerfinance.gov/data-research/consumer-complaints/
Bulk CSV: https://files.consumerfinance.gov/ccdb/complaints.csv.zip
License: CC0-1.0 (public domain).

CLI:
  python cfpb_complaints_pipeline.py              # incremental (last 12 months)
  python cfpb_complaints_pipeline.py --backfill   # all complaints (1M+ rows)

Output:
  storage/raw/cfpb_complaints/year=YYYY/month=MM/cfpb_complaints_{mode}_{date}.parquet
"""

import argparse
import datetime
import io
import os
import zipfile

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR = "storage/raw/cfpb_complaints"
DOWNLOAD_URL = "https://files.consumerfinance.gov/ccdb/complaints.csv.zip"
CHUNK_SIZE = 100_000

COLUMN_MAP = {
    "Complaint ID":                "complaint_id",
    "Date received":               "date_received",
    "Product":                     "product",
    "Sub-product":                 "sub_product",
    "Issue":                       "issue",
    "Sub-issue":                   "sub_issue",
    "Consumer complaint narrative": "complaint_text",
    "Company public response":     "company_public_response",
    "Company":                     "company",
    "State":                       "state",
    "ZIP code":                    "zip_code",
    "Tags":                        "tags",
    "Consumer consent provided?":  "consumer_consent_provided",
    "Submitted via":               "submitted_via",
    "Date sent to company":        "date_sent_to_company",
    "Company response to consumer": "company_response",
    "Timely response?":            "timely_response",
    "Consumer disputed?":          "consumer_disputed",
}


def _parse_date(val) -> str:
    if pd.isna(val) or not val:
        return ""
    val = str(val).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(val, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return val[:10]


def stream_complaints(cutoff_date: str | None):
    """Stream complaints CSV from CFPB ZIP, yielding DataFrames in chunks."""
    print(f"  Downloading {DOWNLOAD_URL} ...")
    resp = requests.get(DOWNLOAD_URL, stream=True)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        csv_name = [n for n in z.namelist() if n.endswith(".csv")][0]
        print(f"  Reading {csv_name} from archive ...")

        with z.open(csv_name) as f:
            for chunk in pd.read_csv(f, chunksize=CHUNK_SIZE, dtype=str, keep_default_na=False, low_memory=False):
                chunk.rename(columns=COLUMN_MAP, inplace=True)

                if cutoff_date:
                    chunk["_date_parsed"] = chunk["date_received"].apply(_parse_date)
                    chunk = chunk[chunk["_date_parsed"] >= cutoff_date].copy()
                    chunk.drop(columns=["_date_parsed"], inplace=True)
                    if chunk.empty:
                        continue
                else:
                    chunk["date_received"] = chunk["date_received"].apply(_parse_date)

                chunk["date_sent_to_company"] = chunk["date_sent_to_company"].apply(_parse_date)

                yield chunk


def main() -> None:
    parser = argparse.ArgumentParser(description="CFPB consumer finance complaints")
    parser.add_argument("--backfill", action="store_true", help="Download all complaints")
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone.utc)
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode = "backfill" if args.backfill else "incremental"

    cutoff = None if args.backfill else (now - datetime.timedelta(days=365)).strftime("%Y-%m-%d")

    os.makedirs(BASE_DIR, exist_ok=True)

    print(f"CFPB Complaints Pipeline  mode={mode}  cutoff={cutoff}\n")
    print("[cfpb_complaints]")

    batch_num = 0
    total_rows = 0

    for df_chunk in stream_complaints(cutoff):
        if df_chunk.empty:
            continue
        df_chunk["fetched_at"] = fetched_at
        batch_num += 1
        total_rows += len(df_chunk)
        path = write_partitioned(
            df_chunk, BASE_DIR,
            f"cfpb_complaints_{mode}_{today_str}_batch{batch_num:04d}.parquet"
        )
        print(f"  Batch {batch_num}: {len(df_chunk):,} rows  -> {path}")

    if total_rows == 0:
        print("  No complaints found.")
        return

    print(f"\n  Total: {total_rows:,} complaints written")
    print("\n--- CFPB COMPLAINTS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
