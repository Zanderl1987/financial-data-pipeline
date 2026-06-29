#!/usr/bin/env python3
"""
OpenFDA Pipeline — drug approvals and enforcement actions (recalls/warnings).

Uses the FDA Open Data API (free, no API key required up to 1,000 req/day;
an optional key raises the limit to 120,000/day).

Two data feeds:
  1. Drug approvals (drugsatfda) — NDA/BLA/ANDA approvals with applicant,
     drug name, active ingredient, dosage form, approval date, and route.
     Signal: PDUFA date outcomes; new molecular entity counts by sponsor.

  2. Drug enforcement actions (recalls) — Class I/II/III recalls with
     recalling firm, product description, reason, and recall initiation date.
     Signal: Safety-driven revenue disruption for affected companies.

Outputs:
  storage/raw/openfda/approvals/year=YYYY/month=MM/openfda_approvals_{mode}_{date}.parquet
  storage/raw/openfda/recalls/year=YYYY/month=MM/openfda_recalls_{mode}_{date}.parquet
  CATALOG tables: openfda_approvals, openfda_recalls

Usage:
  python openfda_pipeline.py             # incremental (last 2 years)
  python openfda_pipeline.py --backfill  # full history from 2010
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests

from storage_utils import write_partitioned

APPROVALS_URL   = "https://api.fda.gov/drug/drugsfda.json"
RECALLS_URL     = "https://api.fda.gov/drug/enforcement.json"
APPROVALS_DIR   = os.path.join("storage", "raw", "openfda", "approvals")
RECALLS_DIR     = os.path.join("storage", "raw", "openfda", "recalls")

BACKFILL_START_YEAR = 2010
INCREMENTAL_YEARS   = 2
PAGE_LIMIT          = 100   # max records per request
REQUEST_INTERVAL    = 0.5
MAX_RETRIES         = 3


def _get_with_retry(url: str, params: dict) -> dict | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None   # no results for query
            if r.status_code == 429:
                wait = 60 * attempt
                print(f"    429 rate limit — waiting {wait}s")
                time.sleep(wait)
            else:
                print(f"    HTTP {r.status_code}: {r.text[:200]}")
                return None
        except requests.RequestException as exc:
            print(f"    Request error (attempt {attempt}): {exc}")
            time.sleep(20 * attempt)
    return None


def _paginate(url: str, search: str, limit: int = PAGE_LIMIT) -> list[dict]:
    """Paginate through all results for a given search query."""
    records = []
    skip = 0
    while True:
        params = {
            "search": search,
            "limit":  limit,
            "skip":   skip,
        }
        data = _get_with_retry(url, params)
        if data is None:
            break
        results = data.get("results", [])
        if not results:
            break
        records.extend(results)
        total = data.get("meta", {}).get("results", {}).get("total", 0)
        skip += len(results)
        if skip >= total or skip >= 5000:  # FDA caps at 5000 results per query
            break
        time.sleep(REQUEST_INTERVAL)
    return records


# ── Approvals ─────────────────────────────────────────────────────────────────

def _parse_approval(rec: dict) -> list[dict]:
    """Flatten one drugsatfda record into per-submission rows."""
    rows = []
    submissions = rec.get("submissions", [])
    products    = rec.get("products", [])
    sponsor     = rec.get("sponsor_name", "")
    app_num     = rec.get("application_number", "")

    # Build a product index for the application
    product_names = [
        p.get("brand_name", "") or p.get("generic_name", "")
        for p in products
    ]
    active_ingredients = [
        ", ".join(
            [ai.get("name", "") for ai in p.get("active_ingredients", [])]
        )
        for p in products
    ]
    dosage_forms = [p.get("dosage_form", "") for p in products]

    product_label = "; ".join(filter(None, product_names[:3]))
    ingredient_label = "; ".join(filter(None, active_ingredients[:3]))
    dosage_label = dosage_forms[0] if dosage_forms else ""

    for sub in submissions:
        action_date = sub.get("submission_status_date", "")
        rows.append({
            "application_number":   app_num,
            "sponsor_name":         sponsor,
            "brand_name":           product_label,
            "active_ingredients":   ingredient_label,
            "dosage_form":          dosage_label,
            "submission_type":      sub.get("submission_type", ""),
            "submission_number":    sub.get("submission_number", ""),
            "submission_status":    sub.get("submission_status", ""),
            "action_date":          action_date,
            "review_priority":      sub.get("review_priority", ""),
        })
    return rows


def fetch_approvals(start_year: int) -> pd.DataFrame:
    print(f"  [approvals] Fetching from {start_year}...")
    search = f"submissions.submission_status_date:[{start_year}0101 TO 29991231]"
    records = _paginate(APPROVALS_URL, search)
    print(f"    {len(records)} applications returned")

    rows = []
    for rec in records:
        rows.extend(_parse_approval(rec))

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["action_date"] = pd.to_datetime(df["action_date"], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["action_date"])
    df = df[df["action_date"].dt.year >= start_year]
    return df.sort_values("action_date").reset_index(drop=True)


# ── Recalls ────────────────────────────────────────────────────────────────────

def _parse_recall(rec: dict) -> dict:
    return {
        "recall_number":         rec.get("recall_number", ""),
        "recalling_firm":        rec.get("recalling_firm", ""),
        "product_description":   rec.get("product_description", "")[:500],
        "reason_for_recall":     rec.get("reason_for_recall", "")[:500],
        "recall_initiation_date": rec.get("recall_initiation_date", ""),
        "report_date":           rec.get("report_date", ""),
        "classification":        rec.get("classification", ""),   # Class I/II/III
        "status":                rec.get("status", ""),
        "product_type":          rec.get("product_type", ""),
        "voluntary_mandated":    rec.get("voluntary_mandated", ""),
        "country":               rec.get("country", ""),
        "state":                 rec.get("state", ""),
        "distribution_pattern":  rec.get("distribution_pattern", "")[:300],
    }


def fetch_recalls(start_year: int) -> pd.DataFrame:
    print(f"  [recalls]   Fetching from {start_year}...")
    search = f"recall_initiation_date:[{start_year}0101 TO 29991231]"
    records = _paginate(RECALLS_URL, search)
    print(f"    {len(records)} recalls returned")

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame([_parse_recall(r) for r in records])
    for col in ("recall_initiation_date", "report_date"):
        df[col] = pd.to_datetime(df[col], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["recall_initiation_date"])
    df = df[df["recall_initiation_date"].dt.year >= start_year]
    return df.sort_values("recall_initiation_date").reset_index(drop=True)


# ── Main ───────────────────────────────────────────────────────────────────────

def main(backfill: bool = False) -> None:
    os.makedirs(APPROVALS_DIR, exist_ok=True)
    os.makedirs(RECALLS_DIR, exist_ok=True)

    now        = datetime.datetime.utcnow()
    today      = now.strftime("%Y%m%d")
    mode       = "backfill" if backfill else "incremental"
    start_year = BACKFILL_START_YEAR if backfill else now.year - INCREMENTAL_YEARS

    print(f"OpenFDA Pipeline  mode={mode}  start_year={start_year}\n")

    # Approvals
    df_approvals = fetch_approvals(start_year)
    if not df_approvals.empty:
        df_approvals["fetched_at"] = now.isoformat()
        path = write_partitioned(df_approvals, APPROVALS_DIR, f"openfda_approvals_{mode}_{today}.parquet")
        print(f"[+] {path}  ({len(df_approvals):,} rows)")
    else:
        print("  No approvals data.")

    print()

    # Recalls
    df_recalls = fetch_recalls(start_year)
    if not df_recalls.empty:
        df_recalls["fetched_at"] = now.isoformat()
        path = write_partitioned(df_recalls, RECALLS_DIR, f"openfda_recalls_{mode}_{today}.parquet")
        print(f"[+] {path}  ({len(df_recalls):,} rows)")
    else:
        print("  No recalls data.")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="OpenFDA pipeline — drug approvals and recalls (keyless)"
    )
    parser.add_argument("--backfill", action="store_true",
                        help=f"Full history from {BACKFILL_START_YEAR}. Default: last {INCREMENTAL_YEARS} years.")
    args = parser.parse_args()
    main(backfill=args.backfill)
