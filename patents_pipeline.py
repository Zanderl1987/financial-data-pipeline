#!/usr/bin/env python3
"""
PatentsView Pipeline.

Fetches US patent grant data from the USPTO PatentsView API as a proxy for
corporate R&D activity. No API key required (rate limit: 45 req/min keyless).

Covers patents in key technology sectors (by CPC group):
  G06N   AI / Machine Learning
  H01L   Semiconductors / Microelectronics
  A61K   Pharmaceutical Preparations (Biotech/Pharma)
  H04W   Wireless / Mobile Communications
  H02J   Power Storage / EV Charging / Grid
  G06F   General-Purpose Computing Hardware/Software

For each sector, fetches: patent_id, grant_date, assignee organization,
title, CPC group, number of claims, number of inventors.

CLI:
  python patents_pipeline.py             # last 2 years
  python patents_pipeline.py --backfill  # last 10 years

Outputs:
  storage/raw/patents/patents_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_URL        = "https://search.patentsview.org/api/v1/patent/"
PATENTS_DIR     = os.path.join("storage", "raw", "patents")
REQUEST_INTERVAL = 1.5
MAX_RETRIES     = 3
PAGE_SIZE       = 1000

# (cpc_group_prefix, sector_label)
CPC_SECTORS = [
    ("G06N", "AI and Machine Learning"),
    ("H01L", "Semiconductors"),
    ("A61K", "Pharmaceuticals and Biotech"),
    ("H04W", "Wireless Communications"),
    ("H02J", "Power Storage and EV"),
    ("G06F", "Computing Hardware and Software"),
]

FIELDS = [
    "patent_id",
    "patent_date",
    "patent_title",
    "patent_num_claims",
    "assignees.assignee_organization",
    "assignees.assignee_type",
    "cpcs.cpc_group",
    "inventors.inventor_first_name",
    "inventors.inventor_last_name",
]


def _get(params):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=60)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  429 rate limit. Backing off {wait}s.")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:200]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(20 * attempt)
    return None


def fetch_sector(cpc_prefix, sector, date_start, date_end):
    """Fetch all patents for a CPC group prefix in a date range (paginated)."""
    import json

    query = {
        "_and": [
            {"_gte": {"patent_date": date_start}},
            {"_lte": {"patent_date": date_end}},
            {"_text_phrase": {"cpcs.cpc_group": cpc_prefix}},
        ]
    }

    all_patents = []
    page = 1
    while True:
        options = {"per_page": PAGE_SIZE, "page": page}
        params  = {
            "q": json.dumps(query),
            "f": json.dumps(FIELDS),
            "o": json.dumps(options),
        }
        data = _get(params)
        time.sleep(REQUEST_INTERVAL)
        if not data:
            break

        patents = data.get("patents", [])
        all_patents.extend(patents)

        total = data.get("total_patent_count", 0)
        if len(all_patents) >= total or not patents:
            break
        page += 1

    return all_patents


def patents_to_rows(patents, sector, fetched_at):
    rows = []
    for p in patents:
        patent_id    = p.get("patent_id")
        patent_date  = p.get("patent_date")
        title        = p.get("patent_title")
        num_claims   = p.get("patent_num_claims")

        assignees = p.get("assignees") or []
        # Take first US corporate assignee, fall back to first assignee
        corp = next((a for a in assignees if (a.get("assignee_type") or "").startswith("2")), None)
        assignee_org = (corp or (assignees[0] if assignees else {})).get("assignee_organization")

        cpcs = p.get("cpcs") or []
        cpc_groups = "|".join(c.get("cpc_group", "") for c in cpcs if c.get("cpc_group"))

        inventors = p.get("inventors") or []
        num_inventors = len(inventors)

        rows.append({
            "patent_id":      patent_id,
            "patent_date":    patent_date,
            "date":           patent_date,
            "sector":         sector,
            "assignee_org":   assignee_org,
            "title":          title,
            "cpc_groups":     cpc_groups,
            "num_claims":     num_claims,
            "num_inventors":  num_inventors,
            "fetched_at":     fetched_at,
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description="PatentsView USPTO patent grants pipeline (keyless)")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch last 10 years (default: 2 years)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"
    years_back = 10 if args.backfill else 2

    date_start = (now - datetime.timedelta(days=365 * years_back)).strftime("%Y-%m-%d")
    date_end   = now.strftime("%Y-%m-%d")

    print(f"PatentsView Pipeline  mode={mode}  range={date_start} to {date_end}")
    os.makedirs(PATENTS_DIR, exist_ok=True)

    all_rows = []
    for cpc_prefix, sector in CPC_SECTORS:
        print(f"\n  [{cpc_prefix}] {sector}...")
        patents = fetch_sector(cpc_prefix, sector, date_start, date_end)
        if patents:
            rows = patents_to_rows(patents, sector, fetched_at)
            all_rows.extend(rows)
            print(f"    {len(patents):,} patents")
        else:
            print(f"    No patents returned")

    if not all_rows:
        print("\nNo data returned.")
        return

    df = (
        pd.DataFrame(all_rows)
        .drop_duplicates(subset=["patent_id"])
        .sort_values("patent_date")
        .reset_index(drop=True)
    )

    path = write_partitioned(df, PATENTS_DIR, f"patents_{mode}_{today_str}.parquet")
    print(f"\n-> {path}  ({len(df):,} patents, {df['sector'].nunique()} sectors)")
    print("\n--- PATENTS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
