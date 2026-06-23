#!/usr/bin/env python3
"""
FDIC BankFind Suite Pipeline.

Fetches US commercial bank data from the FDIC public REST API (no key required):
  - institutions : all active FDIC-insured institutions (assets, deposits, charter class, state)
  - financials   : quarterly call report financials (paginated, back to 1992)
  - failures     : all bank failures since 1934

API: https://banks.data.fdic.gov/api/

CLI:
  python fdic_pipeline.py             # incremental (last 5 years of financials)
  python fdic_pipeline.py --backfill  # full financials history (1992+)

Outputs:
  storage/raw/fdic/institutions/year=YYYY/month=MM/fdic_institutions_{date}.parquet
  storage/raw/fdic/financials/year=YYYY/month=MM/fdic_financials_{mode}_{date}.parquet
  storage/raw/fdic/failures/year=YYYY/month=MM/fdic_failures_{date}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_URL    = "https://banks.data.fdic.gov/api"
BASE_DIR    = "storage/raw/fdic"
PAGE_SIZE   = 10_000
REQUEST_GAP = 0.5


def _get(endpoint: str, params: dict) -> list[dict]:
    """Paginate through FDIC API and return all records."""
    params = {**params, "limit": PAGE_SIZE, "offset": 0, "output": "json"}
    records = []
    while True:
        resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=60)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", [])
        if not data:
            break
        for item in data:
            records.append(item.get("data", item))
        total = body.get("meta", {}).get("total", 0)
        params["offset"] += PAGE_SIZE
        print(f"    fetched {len(records):,} / {total:,}")
        if len(records) >= total:
            break
        time.sleep(REQUEST_GAP)
    return records


def fetch_institutions(fetched_at: str) -> pd.DataFrame:
    fields = "CERT,INSTNAME,CITY,STNAME,STALP,ASSET,DEP,NETINC,REPDTE,CHRTAGNT,INSTCAT,ACTIVE"
    records = _get("institutions", {"fields": fields, "filters": "ACTIVE:1"})
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df.columns = [c.lower() for c in df.columns]
    df["fetched_at"] = fetched_at
    return df


def fetch_financials(start_date: str, end_date: str, fetched_at: str) -> pd.DataFrame:
    fields = (
        "CERT,REPDTE,ASSET,DEP,LNLSNET,INTINC,NONII,NETINC,"
        "INTEXP,EQTOT,LNATRES,RBCRWAJ,NIM,ROA,ROE"
    )
    # REPDTE format: YYYYMMDD
    start = start_date.replace("-", "")
    end   = end_date.replace("-", "")
    records = _get("financials", {
        "fields":  fields,
        "filters": f"REPDTE:[{start}+TO+{end}]",
        "sort_by": "REPDTE",
        "sort_order": "ASC",
    })
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df.columns = [c.lower() for c in df.columns]
    if "repdte" in df.columns:
        df["report_date"] = pd.to_datetime(df["repdte"].astype(str), format="%Y%m%d", errors="coerce")
        df["report_date"] = df["report_date"].dt.strftime("%Y-%m-%d")
    df["fetched_at"] = fetched_at
    return df


def fetch_failures(fetched_at: str) -> pd.DataFrame:
    fields = "CERT,NAME,CITY,STALP,SAVR,RESTYPE,RESTYPE1,QBFDESC,COST,FAILDATE,SAVR,CHARTER"
    records = _get("failures", {"fields": fields})
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df.columns = [c.lower() for c in df.columns]
    if "faildate" in df.columns:
        df["faildate"] = pd.to_datetime(df["faildate"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["fetched_at"] = fetched_at
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="FDIC bank institutions, financials, and failures")
    parser.add_argument("--backfill", action="store_true", help="Full financial history (1992+)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"
    start_date = "1992-03-31" if args.backfill else f"{now.year - 5}-01-01"
    end_date   = now.strftime("%Y-%m-%d")

    for sub in ("institutions", "financials", "failures"):
        os.makedirs(os.path.join(BASE_DIR, sub), exist_ok=True)

    print(f"FDIC Pipeline  mode={mode}  financials={start_date} -> {end_date}\n")

    print("[fdic_institutions]")
    try:
        df = fetch_institutions(fetched_at)
        if not df.empty:
            path = write_partitioned(df, f"{BASE_DIR}/institutions",
                                     f"fdic_institutions_{today_str}.parquet")
            print(f"  -> {path}  ({len(df):,} rows)\n")
        else:
            print("  No data\n")
    except Exception as exc:
        print(f"  ERROR: {exc}\n")

    print("[fdic_financials]")
    try:
        df = fetch_financials(start_date, end_date, fetched_at)
        if not df.empty:
            path = write_partitioned(df, f"{BASE_DIR}/financials",
                                     f"fdic_financials_{mode}_{today_str}.parquet")
            print(f"  -> {path}  ({len(df):,} rows)\n")
        else:
            print("  No data\n")
    except Exception as exc:
        print(f"  ERROR: {exc}\n")

    print("[fdic_failures]")
    try:
        df = fetch_failures(fetched_at)
        if not df.empty:
            path = write_partitioned(df, f"{BASE_DIR}/failures",
                                     f"fdic_failures_{today_str}.parquet")
            print(f"  -> {path}  ({len(df):,} rows)\n")
        else:
            print("  No data\n")
    except Exception as exc:
        print(f"  ERROR: {exc}\n")

    print("--- FDIC PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
