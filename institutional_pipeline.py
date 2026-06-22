#!/usr/bin/env python3
"""
Institutional Holdings Pipeline — SEC 13F Filings.

Parses 13F-HR quarterly filings from SEC EDGAR for a curated list of
major institutional investors. Shows what each institution holds, at what
value, and how their positions change quarter over quarter.

Uses the SEC EDGAR EFTS (Electronic Full-Text Search) API directly.
No API key required. Please include a user-agent in requests per SEC policy
(set EDGAR_USER_AGENT in .env, e.g. "YourName your@email.com").

Institutions tracked:
  Berkshire Hathaway, Vanguard, BlackRock, Fidelity, State Street,
  ARK Investment, Bridgewater, Citadel, Renaissance, Third Point,
  Pershing Square, Tiger Global, D.E. Shaw, Two Sigma, Coatue

CLI:
  python institutional_pipeline.py             # last 4 quarters
  python institutional_pipeline.py --backfill  # last 12 quarters (~3 years)
  python institutional_pipeline.py --quarters 8

Output:
  storage/raw/institutional/institutional_holdings_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import io
import os
import re
import time
import xml.etree.ElementTree as ET

import pandas as pd
import requests
from dotenv import load_dotenv
from storage_utils import write_partitioned

load_dotenv()

EDGAR_USER_AGENT = os.environ.get("EDGAR_USER_AGENT", "financial-data-pipeline contact@example.com")
BASE_URL = "https://data.sec.gov"
EFTS_URL = "https://efts.sec.gov"
BASE_DIR = os.path.join("storage", "raw", "institutional")
REQUEST_INTERVAL = 0.15  # SEC asks for max ~10 req/sec
MAX_RETRIES = 3

# CIK numbers for major institutional 13F filers
INSTITUTIONS = {
    "Berkshire Hathaway":      "0001067983",
    "Vanguard Group":          "0000102909",
    "BlackRock":               "0001364742",
    "FMR LLC (Fidelity)":      "0000315066",
    "State Street":            "0000093751",
    "ARK Investment Mgmt":     "0001579982",
    "Bridgewater Associates":  "0001350694",
    "Citadel Advisors":        "0001423298",
    "D.E. Shaw":               "0001009207",
    "Two Sigma Investments":   "0001536411",
    "Third Point LLC":         "0001040570",
    "Pershing Square Capital": "0001336528",
    "Coatue Management":       "0001336752",
    "Tiger Global Mgmt":       "0001167483",
    "Viking Global Investors": "0001103804",
    "Greenlight Capital":      "0001079114",
    "Baupost Group":           "0001061219",
    "Point72 Asset Mgmt":      "0001603466",
}


def make_headers():
    return {
        "User-Agent": EDGAR_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }


def get_with_backoff(url, headers=None, stream=False):
    h = headers or make_headers()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=h, timeout=30, stream=stream)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 429:
                wait = 30 * attempt
                print(f"    429 rate limit. Waiting {wait}s.")
                time.sleep(wait)
            elif resp.status_code == 404:
                return None
            else:
                print(f"    HTTP {resp.status_code}: {url}")
                return None
        except requests.RequestException as exc:
            print(f"    Request error (attempt {attempt}): {exc}")
            time.sleep(10 * attempt)
    return None


def get_13f_filings(cik, max_filings):
    """Return list of recent 13F-HR filing metadata for a CIK."""
    url = f"{BASE_URL}/submissions/CIK{cik}.json"
    resp = get_with_backoff(url)
    if not resp:
        return []

    data = resp.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accession_nums = recent.get("accessionNumber", [])
    filed_dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    filings = []
    for form, accession, filed, primary_doc in zip(forms, accession_nums, filed_dates, primary_docs):
        if form != "13F-HR":
            continue
        filings.append({
            "accession": accession,
            "filed_date": filed,
            "primary_doc": primary_doc,
        })
        if len(filings) >= max_filings:
            break

    return filings


def parse_13f_xml(xml_content, institution_name, cik, filed_date):
    """Parse 13F-HR XML and return list of holding dicts."""
    # Handle both old and new namespace formats
    xml_content = xml_content.replace('xmlns=', 'xmlignore=')  # strip default namespace
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        print(f"    XML parse error: {exc}")
        return []

    rows = []
    for info in root.iter("infoTable"):
        def txt(tag):
            el = info.find(tag)
            return el.text.strip() if el is not None and el.text else None

        value_raw = txt("value")
        shares_raw = txt("sshPrnamt")
        try:
            value = float(value_raw) * 1000 if value_raw else None  # 13F values in thousands
        except (ValueError, TypeError):
            value = None
        try:
            shares = float(shares_raw) if shares_raw else None
        except (ValueError, TypeError):
            shares = None

        rows.append({
            "institution":   institution_name,
            "cik":           cik,
            "filed_date":    filed_date,
            "company_name":  txt("nameOfIssuer"),
            "cusip":         txt("cusip"),
            "value_usd":     value,
            "shares":        shares,
            "share_type":    txt("sshPrnamtType"),
            "put_call":      txt("putCall"),
            "investment_discretion": txt("investmentDiscretion"),
            "voting_authority_sole": txt("Sole"),
            "voting_authority_shared": txt("Shared"),
            "voting_authority_none": txt("None"),
        })
    return rows


def fetch_filing_document(cik, accession, primary_doc):
    """Fetch the primary 13F XML document from EDGAR Archives."""
    # Accession number formatted as path: 0001067983-24-000001 -> 0001067983/24000001
    acc_no_dash = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/{acc_no_dash[:10]}/{acc_no_dash}/{primary_doc}"
    resp = get_with_backoff(url)
    if not resp:
        return None
    return resp.text


def main():
    parser = argparse.ArgumentParser(description="SEC 13F institutional holdings pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch last 12 quarters (~3 years)")
    parser.add_argument("--quarters", type=int, default=4,
                        help="Number of most recent quarters to fetch (default: 4)")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if args.backfill else "incremental"
    max_filings = 12 if args.backfill else args.quarters

    print(f"Institutional Holdings Pipeline  mode={mode}  quarters_per_institution={max_filings}")
    print(f"  User-Agent: {EDGAR_USER_AGENT}")
    print(f"  Tracking {len(INSTITUTIONS)} institutions\n")

    os.makedirs(BASE_DIR, exist_ok=True)

    all_rows = []
    total = len(INSTITUTIONS)

    for i, (name, cik) in enumerate(INSTITUTIONS.items(), 1):
        print(f"[{i}/{total}] {name} (CIK {cik})...")
        filings = get_13f_filings(cik, max_filings)
        print(f"  Found {len(filings)} 13F-HR filings.")
        time.sleep(REQUEST_INTERVAL)

        for filing in filings:
            acc = filing["accession"]
            filed = filing["filed_date"]
            primary_doc = filing["primary_doc"]

            # Fetch the actual XML document
            xml_content = fetch_filing_document(cik, acc, primary_doc)
            if not xml_content:
                # Try common alternate document name patterns
                xml_content = fetch_filing_document(cik, acc, "primary_doc.xml")
            if not xml_content:
                print(f"    Skipping {acc} — could not fetch document")
                time.sleep(REQUEST_INTERVAL)
                continue

            rows = parse_13f_xml(xml_content, name, cik, filed)
            if rows:
                all_rows.extend(rows)
                print(f"    {filed}: {len(rows):,} holdings")
            else:
                print(f"    {filed}: no holdings parsed")
            time.sleep(REQUEST_INTERVAL)

    if not all_rows:
        print("No institutional holding data fetched.")
        return

    df = pd.DataFrame(all_rows)
    df["value_usd"] = pd.to_numeric(df["value_usd"], errors="coerce")
    df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
    df["filed_date"] = pd.to_datetime(df["filed_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["fetched_at"] = now.isoformat()

    path = write_partitioned(
        df, BASE_DIR,
        f"institutional_holdings_{mode}_{today_str}.parquet",
    )
    print(f"\n-> {path}")
    print(f"   {len(df):,} holding records  |  {df['institution'].nunique()} institutions  "
          f"|  {df['filed_date'].nunique()} filing dates")
    print("\n--- INSTITUTIONAL HOLDINGS PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
