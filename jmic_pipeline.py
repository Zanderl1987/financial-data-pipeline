#!/usr/bin/env python3
"""
JMIC (Joint Maritime Information Center) Pipeline -- maritime security
incidents and advisories for the Red Sea / Gulf of Aden / Arabian Gulf /
Indian Ocean, relevant to shipping risk, routing, and freight rates.

JMIC is a Combined Maritime Forces capability stood up Feb 2024 to track
Houthi attacks on merchant shipping. Its public site (ukmto.org) is a
Next.js/Sitecore SPA -- the HTML gives nothing (see CLAUDE.md's
"UKMTO incident reports" dead end for /recent-incidents), but the frontend's
own document-index REST API is reachable directly and unauthenticated:

  GET {BASE_URL}/api/ukmto/products-count/{productTypeId}/{year}
      -> [{id: <month folder id>, name: "January", productItemCount: N}, ...]
  GET {BASE_URL}/api/ukmto/products/{monthFolderId}
      -> [{id, reference, issueDate, name, location, pdfUrl}, ...]

Found by reading the site's webpack bundle (chunk 401's module 455) --
NOT the Sitecore GraphQL edge endpoint also embedded there, which is a red
herring for CMS page content, not this document index. Cloudflare 403s a
bare request; a real User-Agent (+ Origin/Referer) is enough -- confirmed
no rate-limiting across 15+ rapid sequential requests, unlike the WAF'd
Invesco/Global X ETF sources.

Two tables:
  jmic_documents  -- one row per PDF across all 4 product types, with the
                     full extracted PDF text as a search corpus.
  jmic_incidents  -- structured rows (date, vessel, vessel type, event type,
                     location, narrative) parsed via PyMuPDF's find_tables()
                     from the "Confirmed Maritime Security Incidents List"
                     table that (recent) JMIC Advisories embed. Older
                     advisories (2024) are prose-only guidance with no such
                     table -- extract_incident_rows() just returns nothing
                     for those, which is expected, not a bug. The Weekly
                     Dashboard PDFs have several inconsistent per-region
                     table layouts (different column sets, plus chart images
                     mis-detected as tables) -- deliberately NOT parsed into
                     structured rows here; their full text is still captured
                     in jmic_documents for keyword search.

PDF downloads are the expensive/impolite part of this pipeline against a
government server, so -- unlike most pipelines in this repo, where raw
storage is allowed to accumulate duplicates across daily runs (see
CLAUDE.md) -- this one explicitly skips re-downloading any doc_id already
present in prior raw output. The lightweight index calls (count + products
list) are cheap and always re-walked so newly published documents are found.

Outputs:
  storage/raw/jmic/documents/year=YYYY/month=MM/jmic_documents_*.parquet
      -> CATALOG table jmic_documents
  storage/raw/jmic/incidents/year=YYYY/month=MM/jmic_incidents_*.parquet
      -> CATALOG table jmic_incidents

Usage:
  python jmic_pipeline.py             # incremental: only fetches new doc_ids
  python jmic_pipeline.py --backfill  # re-fetch and re-parse everything

CLI output is ASCII-only (cp1252 terminal).
"""

import argparse
import datetime
import re
import time
from pathlib import Path

import pandas as pd
from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from storage_utils import write_partitioned, find_parquet_files

BASE_DIR = Path(__file__).parent
DOCS_OUT_DIR = BASE_DIR / "storage" / "raw" / "jmic" / "documents"
INCIDENTS_OUT_DIR = BASE_DIR / "storage" / "raw" / "jmic" / "incidents"

BASE_URL = "https://sccd.royalnavy.mod.uk"
COUNT_URL = BASE_URL + "/api/ukmto/products-count/{product_type_id}/{year}"
PRODUCTS_URL = BASE_URL + "/api/ukmto/products/{folder_id}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Origin": "https://www.ukmto.org",
    "Referer": "https://www.ukmto.org/",
    "Accept": "application/json",
}

PRODUCT_TYPES = {
    "advisories":         "84a787ea-9bf6-4f47-9689-e2c0546dbdad",
    "weekly_dashboard":   "426cd2e1-3c60-4a02-b1a8-fa113cd99da2",
    "infonotes":          "9cfb6549-32b0-40cc-8444-fb97262788ed",
    "monthly_statistics": "ba3c97f7-7f9a-4662-8b74-72c2cf7db231",
}

START_YEAR = 2024  # JMIC established Feb 2024; first advisory 07 Aug 2024
SOURCE = "jmic"
REQUEST_PAUSE = 0.2   # between index API calls
PDF_PAUSE = 0.4        # between PDF downloads (courtesy to a gov't server)
MAX_RETRIES = 3
BACKOFF_SECONDS = 20

# Header keywords that identify the structured incident table within a page,
# regardless of exact wording/whitespace across advisory revisions.
INCIDENT_TABLE_KEYWORDS = ("date", "vessel", "narrative")

_DATE_REF_RE = re.compile(
    r"(\d{1,2}\s+[A-Za-z]+\s+\d{4}),?\s*UKMTO\s*([\w-]+)", re.IGNORECASE
)


def _get_with_retry(url: str, timeout: int = 60):
    # Cloudflare's bot management fingerprints the TLS/JA3 handshake, not just
    # headers -- plain `requests` (urllib3's OpenSSL stack) gets a 403 that a
    # manual curl probe with identical headers does not. curl_cffi impersonates
    # a real Chrome TLS fingerprint, which is enough to pass; confirmed live
    # 2026-09-01 (plain requests 403, curl_cffi 200, same URL/headers).
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, impersonate="chrome124")
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from server. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code} for {url}")
                return None
        except RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(BACKOFF_SECONDS * attempt)
    return None


# ---------------------------------------------------------------------------
# Document index (cheap JSON calls, always walked)
# ---------------------------------------------------------------------------

def fetch_month_folders(product_type_id: str, year: int) -> list[dict]:
    r = _get_with_retry(COUNT_URL.format(product_type_id=product_type_id, year=year))
    if r is None:
        return []
    try:
        return r.json()
    except ValueError:
        return []


def fetch_products(folder_id: str) -> list[dict]:
    r = _get_with_retry(PRODUCTS_URL.format(folder_id=folder_id))
    if r is None:
        return []
    try:
        return r.json()
    except ValueError:
        return []


def list_all_documents(end_year: int) -> pd.DataFrame:
    """Walk every product type x year x month folder. Cheap (JSON only)."""
    records = []
    for product_type, type_id in PRODUCT_TYPES.items():
        for year in range(START_YEAR, end_year + 1):
            months = fetch_month_folders(type_id, year) or []
            time.sleep(REQUEST_PAUSE)
            print(f"  {product_type} {year}: {len(months)} month(s), "
                  f"{sum(m.get('productItemCount', 0) for m in months)} document(s)")
            for month in months:
                products = fetch_products(month["id"]) or []
                time.sleep(REQUEST_PAUSE)
                for p in products:
                    records.append({
                        "doc_id": p.get("id"),
                        "product_type": product_type,
                        "reference": p.get("reference"),
                        "name": p.get("name"),
                        "issue_date": p.get("issueDate"),
                        "location": p.get("location"),
                        "pdf_url": p.get("pdfUrl"),
                    })
    df = pd.DataFrame(records)
    print(f"  Document index: {len(df)} documents across {len(PRODUCT_TYPES)} product types, "
          f"{START_YEAR}-{end_year}.")
    return df


# ---------------------------------------------------------------------------
# PDF fetch + parse (expensive, skipped for already-known doc_ids)
# ---------------------------------------------------------------------------

def _parse_date_ref(cell: str) -> tuple[str | None, str | None]:
    if not cell:
        return None, None
    m = _DATE_REF_RE.search(cell.replace("\n", " "))
    if not m:
        return None, None
    date_str, ref = m.groups()
    parsed = pd.to_datetime(date_str, errors="coerce")
    return (parsed.date().isoformat() if pd.notnull(parsed) else None), f"UKMTO {ref}"


def extract_full_text(pdf_bytes: bytes) -> tuple[str | None, int | None]:
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
        pages = doc.page_count
        doc.close()
        return text, pages
    except Exception as exc:
        print(f"    Text extraction failed: {exc}")
        return None, None


def extract_incident_rows(pdf_bytes: bytes, doc_id: str, doc_reference: str) -> list[dict]:
    """Best-effort structured extraction of the incident table, when present.
    Returns [] for documents without a matching table -- expected for most
    non-advisory product types and pre-2025 prose-only advisories."""
    rows = []
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        print(f"    Table extraction open failed: {exc}")
        return rows

    for page in doc:
        try:
            tabs = page.find_tables()
        except Exception:
            continue
        for t in tabs.tables:
            header_joined = " ".join((h or "").lower().replace("\n", " ") for h in t.header.names)
            if not all(kw in header_joined for kw in INCIDENT_TABLE_KEYWORDS):
                continue
            for row in t.extract():
                if not row or (row[0] or "").strip().lower() == "date":
                    continue  # header row repeated inside extract()
                if len(row) < 6:
                    continue
                date_cell, vessel, vessel_type, event_type, location, narrative = row[:6]
                incident_date, ukmto_ref = _parse_date_ref(date_cell or "")
                rows.append({
                    "incident_date": incident_date,
                    "ukmto_reference": ukmto_ref,
                    "vessel_name": (vessel or "").replace("\n", " ").strip() or None,
                    "vessel_type": (vessel_type or "").replace("\n", " ").strip() or None,
                    "event_type": (event_type or "").replace("\n", " ").strip() or None,
                    "location": (location or "").replace("\n", " ").strip() or None,
                    "narrative": (narrative or "").replace("\n", " ").strip() or None,
                    "doc_id": doc_id,
                    "doc_reference": doc_reference,
                })
    doc.close()
    return rows


def fetch_and_parse_new_documents(index_df: pd.DataFrame, known_doc_ids: set) -> tuple[pd.DataFrame, pd.DataFrame]:
    new_docs = index_df[~index_df["doc_id"].isin(known_doc_ids)].copy()
    if new_docs.empty:
        print("  No new documents to download.")
        return pd.DataFrame(), pd.DataFrame()

    print(f"  Downloading {len(new_docs)} new documents...")
    doc_records, incident_records = [], []
    t_start = time.time()
    for i, (_, row) in enumerate(new_docs.iterrows(), 1):
        pdf_url = row["pdf_url"]
        if not pdf_url:
            continue
        r = _get_with_retry(pdf_url, timeout=60)
        time.sleep(PDF_PAUSE)
        if r is None:
            print(f"    [{i}/{len(new_docs)}] FAILED to download {row['reference']!r}")
            continue
        if i % 10 == 0 or i == len(new_docs):
            elapsed = time.time() - t_start
            print(f"    [{i}/{len(new_docs)}] {elapsed:.0f}s elapsed, {elapsed / i:.1f}s/doc avg")
        full_text, page_count = extract_full_text(r.content)
        doc_records.append({
            **row.to_dict(),
            "full_text": full_text,
            "page_count": page_count,
        })
        incident_records.extend(
            extract_incident_rows(r.content, row["doc_id"], row["reference"])
        )

    docs_df = pd.DataFrame(doc_records)
    incidents_df = pd.DataFrame(incident_records)
    print(f"  Fetched {len(docs_df)} documents ({docs_df['full_text'].notna().sum() if not docs_df.empty else 0} with text), "
          f"{len(incidents_df)} structured incident rows.")
    return docs_df, incidents_df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _existing_doc_ids() -> set:
    ids = set()
    for f in find_parquet_files(str(DOCS_OUT_DIR)):
        try:
            ids.update(pd.read_parquet(f, columns=["doc_id"])["doc_id"].dropna().tolist())
        except Exception:
            continue
    return ids


def main():
    parser = argparse.ArgumentParser(description="JMIC maritime security pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Re-download and re-parse every document, ignoring the already-fetched cache.")
    args = parser.parse_args()
    mode = "backfill" if args.backfill else "incremental"
    now = datetime.datetime.utcnow()
    fetched_at = now.isoformat()

    print("[jmic] Walking document index...")
    index_df = list_all_documents(end_year=now.year)
    if index_df.empty:
        print("[jmic] Empty index -- aborting.")
        return

    known_doc_ids = set() if args.backfill else _existing_doc_ids()
    print(f"[jmic] {len(known_doc_ids)} documents already captured; "
          f"{(~index_df['doc_id'].isin(known_doc_ids)).sum()} new.")

    docs_df, incidents_df = fetch_and_parse_new_documents(index_df, known_doc_ids)

    stamp = now.strftime("%Y%m%d")
    if not docs_df.empty:
        docs_df["source"] = SOURCE
        docs_df["fetched_at"] = fetched_at
        path = write_partitioned(docs_df, str(DOCS_OUT_DIR), f"jmic_documents_{mode}_{stamp}.parquet")
        print(f"  Wrote {len(docs_df):,} rows -> {path}")

    if not incidents_df.empty:
        incidents_df["source"] = SOURCE
        incidents_df["fetched_at"] = fetched_at
        path = write_partitioned(incidents_df, str(INCIDENTS_OUT_DIR), f"jmic_incidents_{mode}_{stamp}.parquet")
        print(f"  Wrote {len(incidents_df):,} rows -> {path}")

    print("\n--- JMIC PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
