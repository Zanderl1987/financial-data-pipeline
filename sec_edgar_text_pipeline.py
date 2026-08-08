#!/usr/bin/env python3
"""
SEC EDGAR Raw Filing Text Pipeline.

Downloads 10-K and 10-Q raw filing text from the TeraflopAI/SEC-EDGAR dataset
on Hugging Face Hub. Provides full-text filing content for NLP analysis
(MDA, risk factors, business descriptions) — complements the structured
fundamentals from fundamentals_pipeline.py.

API: Hugging Face datasets (TeraflopAI/SEC-EDGAR), no API key required for
     public datasets.

CLI:
  python sec_edgar_text_pipeline.py              # incremental (last 3 yrs)
  python sec_edgar_text_pipeline.py --backfill   # all 8M filings (slow)
  python sec_edgar_text_pipeline.py --forms 10-K,10-Q,8-K   # custom forms

Requires:
  pip install datasets

Output:
  storage/raw/sec_edgar_text/year=YYYY/month=MM/sec_edgar_text_{mode}_{date}.parquet
"""

import argparse
import datetime
import json
import os
import sys

import pandas as pd
from storage_utils import write_partitioned

BASE_DIR = "storage/raw/sec_edgar_text"
HF_DATASET = "TeraflopAI/SEC-EDGAR"

DEFAULT_FORMS = ["10-K", "10-Q"]
BATCH_SIZE = 5000


def _safe_json(val: str) -> dict | list | None:
    if not val:
        return None
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


def _extract_form_type(raw_documents: str) -> str:
    docs = _safe_json(raw_documents)
    if isinstance(docs, list) and docs:
        return (docs[0].get("type") or "").strip()
    return ""


def _extract_cik(raw_filer: str) -> str:
    filer = _safe_json(raw_filer)
    if isinstance(filer, dict):
        cd = filer.get("company-data") or {}
        return (cd.get("cik") or "").strip()
    return ""


def _extract_company_name(raw_filer: str) -> str:
    filer = _safe_json(raw_filer)
    if isinstance(filer, dict):
        cd = filer.get("company-data") or {}
        return (cd.get("conformed-name") or "").strip()
    return ""


def _extract_sic(raw_filer: str) -> str:
    filer = _safe_json(raw_filer)
    if isinstance(filer, dict):
        cd = filer.get("company-data") or {}
        return (cd.get("assigned-sic") or "").strip()
    return ""


def stream_filings(target_forms: list[str], cutoff_date: str | None):
    """Stream filings from HF dataset, yield batches as DataFrames."""
    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: Install datasets: pip install datasets")
        sys.exit(1)

    print(f"  Loading {HF_DATASET} (streaming mode, {len(target_forms)} form types)...")
    ds = load_dataset(HF_DATASET, split="train", streaming=True)
    rows = []
    total = 0

    for i, ex in enumerate(ds):
        form_type = _extract_form_type(ex.get("metadata_documents"))
        if form_type not in target_forms:
            continue

        filing_date = (ex.get("metadata_filing-date") or "")[:10]
        if cutoff_date and filing_date < cutoff_date:
            continue

        rows.append({
            "accession_number": ex.get("metadata_accession-number", ""),
            "filing_date":      filing_date,
            "period":           (ex.get("metadata_period") or "")[:10],
            "form_type":        form_type,
            "cik":              _extract_cik(ex.get("metadata_filer")),
            "company_name":     _extract_company_name(ex.get("metadata_filer")),
            "sic":              _extract_sic(ex.get("metadata_filer")),
            "text_content":     ex.get("text", ""),
        })
        total += 1

        if len(rows) >= BATCH_SIZE:
            yield pd.DataFrame(rows), total
            rows.clear()

    if rows:
        yield pd.DataFrame(rows), total


def main() -> None:
    parser = argparse.ArgumentParser(description="SEC EDGAR raw filing text (TeraflopAI)")
    parser.add_argument("--backfill", action="store_true", help="Download all filings")
    parser.add_argument("--forms", default=",".join(DEFAULT_FORMS),
                        help="Comma-separated form types (default: 10-K,10-Q)")
    args = parser.parse_args()

    target_forms = [f.strip() for f in args.forms.split(",") if f.strip()]

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode = "backfill" if args.backfill else "incremental"
    cutoff = None if args.backfill else str(now.year - 3)

    os.makedirs(BASE_DIR, exist_ok=True)

    print(f"SEC EDGAR Text Pipeline  mode={mode}  forms={target_forms}  cutoff={cutoff}\n")
    print("[sec_edgar_text]")

    batch_num = 0
    total_rows = 0

    for df_batch, running_total in stream_filings(target_forms, cutoff):
        if df_batch.empty:
            continue
        df_batch["fetched_at"] = fetched_at
        batch_num += 1
        path = write_partitioned(
            df_batch, BASE_DIR,
            f"sec_edgar_text_{mode}_{today_str}_batch{batch_num:04d}.parquet"
        )
        total_rows = running_total
        print(f"  Batch {batch_num}: {len(df_batch):,} rows  -> {path}")

    if total_rows == 0:
        print("  No filings found.")
        return

    print(f"\n  Total: {total_rows:,} filings written")
    print("\n--- SEC EDGAR TEXT PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
