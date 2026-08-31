#!/usr/bin/env python3
"""
California Legislature Financial Disclosures Pipeline.

California FPPC Form 700 (Statement of Economic Interests), Schedule A-1
(Investments -- Stocks, Bonds, and Other Interests, ownership < 10%), for
State Senate and State Assembly filers. No API key required.

Source: form700search.fppc.ca.gov ("DisclosureDocs eRetrieval", a Granicus
product used by the California FPPC). Two real, keyless endpoints:
  - POST /Home/SearchDocuments        filing index (JSON, double-encoded --
                                       the response body is itself a JSON
                                       string containing the real payload)
  - POST /Home/GetRedactedFormPdf     {indexID} -> a session-keyed PDF URL.
    The ASP.NET_SessionId cookie set on THIS response must be forwarded to
    the DownloadPdf GET or it silently 200s with an HTML error page instead
    of the PDF -- there is no error status to catch, only a wrong
    content-type.

Notes on the data:
  - First electronic filing year is 2018 (0 filings 2016-2017, confirmed
    live). State Senate = 774 filings, State Assembly = 1,933 filings
    total as of 2026-08-31, well under any per-request cap.
  - The PDF is NOT an AcroForm (page.widgets() is empty) -- values are
    flattened text, positioned near but not identical to their template
    labels. Checkbox state (Fair Market Value bracket, Nature of
    Investment) is invisible to text extraction entirely; it is recovered
    from page.get_drawings() -- the selected option has a small
    blue-filled rect drawn inside its black-stroke checkbox outline.
    Verified against a live filing (Sen. Choi, 2024/2025: Coinbase Global
    Inc marked Stock / $10,001-$100,000).
  - Each Schedule A-1 page holds exactly 5 investment slots (2 rows of 2
    columns, plus one more slot sharing page space with the signature
    block). Slots are found by their "(NAME OF BUSINESS ENTITY" anchor
    bullet; every other field is a slot-relative offset from that anchor
    or from the slot's own "FAIR MARKET VALUE"/"NATURE OF INVESTMENT"
    labels, so the map holds across rows/columns/pages.
  - Only Schedule A-1 is parsed. Schedules A-2/B/C/D/E are real, separate
    field layouts and out of scope for this pipeline -- see
    work-notes/financial-data-pipeline/TASKS.md.

CLI:
  python california_disclosures_pipeline.py                 # current year
  python california_disclosures_pipeline.py --backfill       # full history
  python california_disclosures_pipeline.py --years 2024 2025
  python california_disclosures_pipeline.py --agency senate  # one chamber

Outputs:
  storage/raw/california_disclosures/california_disclosures_{mode}_{YYYY}.parquet
"""

import argparse
import datetime
import json
import os
import time

import pandas as pd
import requests

from storage_utils import write_partitioned

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - environment guard
    fitz = None

# -- Endpoints ---------------------------------------------------------------
BASE = "https://form700search.fppc.ca.gov"
SEARCH_URL = BASE + "/Home/SearchDocuments"
PDF_INFO_URL = BASE + "/Home/GetRedactedFormPdf"

AGENCIES = {"senate": "State Senate", "assembly": "State Assembly"}
FIRST_YEAR = 2018

OUTPUT_DIR = os.path.join("storage", "raw", "california_disclosures")
CACHE_DIR = os.path.join("storage", "cache", "california_disclosures")

USER_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "financial-data-pipeline (contact via github.com/Zanderl1987)")

MAX_RETRIES = 3
REQUEST_PAUSE = 0.35
CHECKPOINT_EVERY = 100

_last_request = [0.0]


def _throttle():
    elapsed = time.time() - _last_request[0]
    if elapsed < REQUEST_PAUSE:
        time.sleep(REQUEST_PAUSE - elapsed)
    _last_request[0] = time.time()


def _to_date(raw):
    if not raw:
        return None
    ts = pd.to_datetime(str(raw).strip(), errors="coerce")
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def _mmddyy_to_iso(mm, dd, yy):
    """Unambiguous MM/DD/YY (2-digit year) -> ISO date, or None if invalid."""
    year = f"20{yy}" if len(yy) == 2 else yy
    ts = pd.to_datetime(f"{year}-{mm}-{dd}", format="%Y-%m-%d", errors="coerce")
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


# -- Filing index --------------------------------------------------------

def fetch_filing_index(session, agency_label, year):
    """
    Return every filing for one agency/year as a list of dicts, from the
    SearchDocuments JSON API. The response body is a JSON-encoded string
    containing the real payload -- must be parsed twice.
    """
    body = {"searchFieldQueryInfos": [
        {"queryField": "FilerAgency", "filterValue": agency_label,
         "queryType": "Match"},
        {"queryField": "FilingYear", "filterValue": str(year),
         "queryType": "Match"},
    ]}
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            resp = session.post(SEARCH_URL, json=body, timeout=60)
            outer = resp.json()
            data = json.loads(outer) if isinstance(outer, str) else outer
        except (requests.RequestException, ValueError) as exc:
            print(f"    search failed (attempt {attempt}): {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
            continue
        filings = []
        for doc in data.get("documents", []):
            positions = doc.get("filingPositions") or [{}]
            pos = next((p for p in positions if p.get("agency") == agency_label),
                       positions[0])
            filer = doc.get("filer", {})
            info = doc.get("filingInfo", {})
            filings.append({
                "index_id": doc.get("indexID"),
                "filer_last_name": filer.get("lastName"),
                "filer_first_name": filer.get("firstName"),
                "filer_middle_name": filer.get("middleName"),
                "agency": pos.get("agency"),
                "position": pos.get("position"),
                "filing_type": pos.get("filingType"),
                "filing_year": pos.get("filingYear"),
                "filed_date": _to_date(info.get("filedDate")),
                "is_amendment": bool(info.get("isAmendment")),
                "no_reportable_interests": bool(info.get("noReportableInterests")),
            })
        return filings
    return []


def _fetch_pdf(session, index_id):
    """
    2-step fetch: GetRedactedFormPdf returns a session-keyed download URL,
    and the ASP.NET_SessionId cookie set on THAT response must be sent on
    the DownloadPdf request or it returns an HTML error page (HTTP 200,
    wrong content-type) instead of the PDF.
    """
    _throttle()
    try:
        resp = session.post(PDF_INFO_URL, json={"indexID": index_id},
                             timeout=60)
        info = resp.json()
    except (requests.RequestException, ValueError):
        return None
    url = info.get("PDFDownloadUrl")
    if not url:
        return None
    _throttle()
    try:
        dl = session.get(url, timeout=60)
    except requests.RequestException:
        return None
    if dl.status_code != 200 or "pdf" not in dl.headers.get("content-type", ""):
        return None
    return dl.content


# -- Schedule A-1 parsing -----------------------------------------------
# Offsets below are relative to the slot's own anchor words, derived from a
# live filing and confirmed template-constant across rows/columns/pages
# (California FPPC Form 700, rev. 2024/2025). A future form redesign would
# need this table re-derived, not just re-tuned.

_SLOT_ANCHOR = ("NAME", "OF", "BUSINESS", "ENTITY")
_FMV_LABEL = ("FAIR", "MARKET", "VALUE")
_NATURE_LABEL = ("NATURE", "OF", "INVESTMENT")
_DATE_LABEL = ("IF", "APPLICABLE,")

# (dx, dy) of each checkbox's rect top-left, relative to the FMV label's
# (x0, y0), and the bracket it represents.
_FMV_BOXES = [
    (-0.3, 11.0, "$2,000 - $10,000", 2000, 10000),
    (107.6, 11.0, "$10,001 - $100,000", 10001, 100000),
    (-0.3, 21.6, "$100,001 - $1,000,000", 100001, 1000000),
    (107.6, 21.6, "Over $1,000,000", 1000000, None),
]
# Same, relative to the NATURE OF INVESTMENT label.
_NATURE_BOXES = [
    (-0.4, 10.1, "Stock"),
    (54.5, 9.8, "Other"),
    (-0.4, 26.7, "Partnership"),
    (54.6, 27.8, "Income Received of $0 - $499"),
    (54.5, 36.2, "Income Received of $500 or More"),
]
_BOX_TOL = 4.0  # points of slop when matching a drawing to a checkbox slot


def _words_by_row(words):
    rows = {}
    for x0, y0, x1, y1, text, *_ in words:
        rows.setdefault(round(y0, 1), []).append((x0, text))
    return rows


def _find_anchors(rows, phrase):
    """
    (x, y) of every occurrence of `phrase` (consecutive words) in the page --
    a row can hold two occurrences (one per column), so every match on a row
    is kept, not just the first.
    """
    hits = []
    for y, cells in rows.items():
        cells = sorted(cells)
        texts = [t for _, t in cells]
        for i in range(len(texts) - len(phrase) + 1):
            if tuple(texts[i:i + len(phrase)]) == phrase:
                hits.append((cells[i][0], y))
    return sorted(hits, key=lambda p: p[1])


def _text_near(rows, x0, y0, dx_range, dy_range, exclude=()):
    """Words within an (x, y) offset window of an anchor, joined in order."""
    found = []
    for y, cells in rows.items():
        dy = y - y0
        if not (dy_range[0] <= dy <= dy_range[1]):
            continue
        for x, text in cells:
            dx = x - x0
            if dx_range[0] <= dx <= dx_range[1] and text not in exclude:
                found.append((y, x, text))
    found.sort()
    return " ".join(t for _, _, t in found).strip() or None


def _checkbox_selected(drawings, x0, y0, dx, dy):
    """True if a blue-filled mark sits inside the checkbox at (x0+dx, y0+dy)."""
    cx, cy = x0 + dx, y0 + dy
    for d in drawings:
        if d.get("color") != (0.0, 0.0, 1.0):
            continue
        r = d["rect"]
        if (abs(r.x0 - cx) < _BOX_TOL and abs(r.y0 - cy) < _BOX_TOL):
            return True
    return False


def parse_schedule_a1(pdf_bytes, filing):
    """Extract Schedule A-1 investment rows from one Form 700 PDF."""
    rows_out = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            header = page.get_text()
            if "SCHEDULE A-1" not in header.upper():
                continue
            words = page.get_text("words")
            row_map = _words_by_row(words)
            drawings = page.get_drawings()

            anchors = _find_anchors(row_map, _SLOT_ANCHOR)
            fmv_anchors = _find_anchors(row_map, _FMV_LABEL)
            nature_anchors = _find_anchors(row_map, _NATURE_LABEL)

            for ax, ay in anchors:
                # Business entity name: just below the anchor, same column.
                entity = _text_near(row_map, ax, ay, (0, 260), (5, 13),
                                     exclude=("NAME", "OF", "BUSINESS",
                                              "ENTITY", "►"))
                description = _text_near(row_map, ax, ay, (0, 260), (40, 50),
                                          exclude=("GENERAL", "DESCRIPTION",
                                                    "OF", "THIS", "BUSINESS"))

                # FMV / Nature labels for THIS slot: nearest one below the
                # anchor, in the same column.
                fmv_pick = min(
                    (p for p in fmv_anchors
                     if abs(p[0] - ax) < 20 and 55 <= p[1] - ay <= 65),
                    key=lambda p: p[1], default=None)
                nature_pick = min(
                    (p for p in nature_anchors
                     if abs(p[0] - ax) < 20 and 95 <= p[1] - ay <= 105),
                    key=lambda p: p[1], default=None)

                fmv_range = fmv_min = fmv_max = None
                if fmv_pick:
                    fx, fy = fmv_pick
                    for dx, dy, label, lo, hi in _FMV_BOXES:
                        if _checkbox_selected(drawings, fx, fy, dx, dy):
                            fmv_range, fmv_min, fmv_max = label, lo, hi
                            break

                nature = None
                if nature_pick:
                    nx, ny = nature_pick
                    for dx, dy, label in _NATURE_BOXES:
                        if _checkbox_selected(drawings, nx, ny, dx, dy):
                            nature = label
                            break
                    # "Other"/describe freetext sits right after the label.
                    describe = _text_near(row_map, nx, ny, (0, 260), (0.5, 3.5),
                                           exclude=("NATURE", "OF", "INVESTMENT"))
                    if nature == "Other" and describe:
                        nature = f"Other: {describe}"

                # Acquired/Disposed dates: two MM/DD/YY triples below the
                # "IF APPLICABLE, LIST DATE:" line for this slot.
                date_anchors = _find_anchors(row_map, _DATE_LABEL)
                date_pick = min(
                    (p for p in date_anchors
                     if abs(p[0] - ax) < 20 and 150 <= p[1] - ay <= 165),
                    key=lambda p: p[1], default=None)
                acquired = disposed = None
                if date_pick:
                    dxx, dyy = date_pick
                    raw_digits = [
                        (x, y, t) for y, cells in row_map.items()
                        for x, t in cells
                        if 10 <= y - dyy <= 20 and 0 <= x - dxx <= 160
                        and t.strip("/").isdigit()]
                    # The renderer sometimes draws a digit run twice at a
                    # slightly different y (a visual-weight artifact) --
                    # collapse near-duplicate x positions to one entry.
                    by_x_bucket = {}
                    for x, y, t in raw_digits:
                        bucket = round(x / 8)
                        if bucket not in by_x_bucket or y < by_x_bucket[bucket][1]:
                            by_x_bucket[bucket] = (x, y, t)
                    digits = sorted(by_x_bucket.values())
                    parts = [t for _, _, t in digits]
                    if len(parts) >= 3:
                        acquired = _mmddyy_to_iso(*parts[0:3])
                    if len(parts) >= 6:
                        disposed = _mmddyy_to_iso(*parts[3:6])

                if not entity:
                    continue  # unfilled template slot
                rows_out.append({
                    "index_id": filing["index_id"],
                    "filer_last_name": filing["filer_last_name"],
                    "filer_first_name": filing["filer_first_name"],
                    "filer_middle_name": filing["filer_middle_name"],
                    "agency": filing["agency"],
                    "position": filing["position"],
                    "filing_type": filing["filing_type"],
                    "filing_year": filing["filing_year"],
                    "filed_date": filing["filed_date"],
                    "is_amendment": filing["is_amendment"],
                    "business_entity": entity,
                    "description": description,
                    "nature_of_investment": nature,
                    "fmv_range": fmv_range,
                    "fmv_min": fmv_min,
                    "fmv_max": fmv_max,
                    "acquired_date": acquired,
                    "disposed_date": disposed,
                })
    finally:
        doc.close()
    return rows_out


def _finalize(rows, fetched_at):
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["fetched_at"] = fetched_at
    df = df.reset_index(drop=True)
    df["row_index"] = df.groupby("index_id").cumcount()
    df["date"] = df["acquired_date"].fillna(df["disposed_date"])
    return df


# -- Resumable checkpoints (same reasoning as congressional_trades_pipeline) -

def _done_path(path):
    return path + ".done"


def _load_checkpoint(path):
    done = set()
    done_file = _done_path(path)
    if os.path.exists(done_file):
        with open(done_file, encoding="utf-8") as fh:
            done = {line.strip() for line in fh if line.strip()}
    if not os.path.exists(path):
        return [], done
    try:
        df = pd.read_parquet(path)
    except Exception:
        return [], done
    if not done and "index_id" in df.columns:
        done = set(df["index_id"].dropna().astype(str).unique())
    return df.to_dict("records"), done


def _save_checkpoint(path, rows, done):
    try:
        with open(_done_path(path), "w", encoding="utf-8") as fh:
            for item in sorted(done):
                print(item, file=fh)
        if rows:
            pd.DataFrame(rows).to_parquet(path, index=False,
                                           compression="snappy")
    except Exception as exc:
        print(f"    checkpoint write failed: {exc}")


def _clear_checkpoint(path):
    for target in (path, _done_path(path)):
        if os.path.exists(target):
            try:
                os.remove(target)
            except OSError:
                pass


# -- Collection ------------------------------------------------------------

def collect_year(session, agency_key, agency_label, year, mode, fetched_at):
    print(f"\n[{agency_key} {year}] searching filings...")
    filings = fetch_filing_index(session, agency_label, year)
    if not filings:
        print(f"  no filings for {year}")
        return
    print(f"  {len(filings)} filings")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    checkpoint = os.path.join(CACHE_DIR, f"{agency_key}_{year}_partial.parquet")
    rows, done = _load_checkpoint(checkpoint)
    if done:
        print(f"  resuming: {len(done)} filings already parsed")

    no_data = 0
    for i, filing in enumerate(filings, 1):
        index_id = filing["index_id"]
        if index_id in done:
            continue
        pdf_bytes = _fetch_pdf(session, index_id)
        if pdf_bytes is None:
            no_data += 1
        else:
            try:
                parsed = parse_schedule_a1(pdf_bytes, filing)
            except Exception as exc:
                print(f"    parse failed for {index_id}: {exc}")
                parsed = []
            if parsed:
                rows.extend(parsed)
            else:
                no_data += 1
        done.add(index_id)

        if i % CHECKPOINT_EVERY == 0:
            _save_checkpoint(checkpoint, rows, done)
            print(f"    {i}/{len(filings)} filings, {len(rows)} rows")

    df = _finalize(rows, fetched_at)
    if df.empty:
        print(f"  {year}: no Schedule A-1 rows parsed")
        _clear_checkpoint(checkpoint)
        return
    path = write_partitioned(
        df, OUTPUT_DIR,
        f"california_disclosures_{agency_key}_{mode}_{year}.parquet")
    print(f"  -> {path}  ({len(df):,} rows, "
          f"{df['filer_last_name'].nunique()} filers, "
          f"{no_data} filings with no Schedule A-1 / unfetchable)")
    _clear_checkpoint(checkpoint)


def main():
    parser = argparse.ArgumentParser(
        description="California legislature Form 700 Schedule A-1 "
                    "(keyless, FPPC official source)")
    parser.add_argument("--backfill", action="store_true",
                        help=f"Full history ({FIRST_YEAR}+)")
    parser.add_argument("--years", nargs="+", type=int,
                        help="Explicit years to fetch (overrides --backfill)")
    parser.add_argument("--agency", choices=["senate", "assembly", "both"],
                        default="both", help="Limit to one chamber")
    args = parser.parse_args()

    if fitz is None:
        print("PyMuPDF (fitz) not installed -- cannot parse Form 700 PDFs.")
        return

    now = datetime.datetime.utcnow()
    fetched_at = now.isoformat()
    mode = "backfill" if (args.backfill or args.years) else "incremental"

    if args.years:
        years = sorted(args.years)
    elif args.backfill:
        years = list(range(FIRST_YEAR, now.year + 1))
    else:
        years = [now.year]

    agencies = (AGENCIES.items() if args.agency == "both"
                else [(args.agency, AGENCIES[args.agency])])

    print("California Legislature Disclosures Pipeline  "
          "(keyless, FPPC Form 700 Schedule A-1)")
    print(f"  mode={mode}  years={years[0]}-{years[-1]}  "
          f"agencies={[a for a, _ in agencies]}")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    for agency_key, agency_label in agencies:
        for year in years:
            collect_year(session, agency_key, agency_label, year, mode,
                         fetched_at)

    print("\n--- CALIFORNIA DISCLOSURES PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
