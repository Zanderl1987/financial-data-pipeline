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
  - Schedule A-1 (investments), Schedule B (interests in real property),
    Schedule D (gifts), and Schedule E (income -- gifts -- travel payments)
    are parsed, each to its own output table -- one PDF fetch per filing
    serves every enabled schedule via SCHEDULES / --schedules. Schedule D has
    5 source slots/page, up to 3 gift line items each, no checkboxes. Same
    digit-duplication render artifact as A-1's dates -- see _digits_near.
    Schedule E has 4 source slots/page (2 cols x 2 rows), a travel date RANGE
    per slot (not a single date), and three checkbox concepts (501(c)(3)
    toggle, Gift/Income, Speech-or-Panel/Other) -- its checkbox marks have
    more column-to-column positional slop than A-1's (~10pt vs ~4pt), so
    nearest-label distance matching is used instead of a fixed-offset table
    -- see _nearest_checkbox_label. Schedule B has 2 property slots/page
    (one per column); the same 2025/2026 page also stacks a loans subsection
    below the property slots, which the parser ignores (that belongs to
    Schedule C). Its checkbox groups (FMV / nature of interest / gross
    rental income) use _nearest_checkbox_label for the same reason.
  - Schedule A-2 (Investments, Income, and Assets of Business Entities/
    Trusts -- ownership >= 10%) and Schedule C (Income, Loans, & Business
    Positions) are also parsed -- see the module comments above
    parse_schedule_a2 / parse_schedule_c for their field maps. A-2 has a
    nested sub-section (investments/real property held BY the entity
    itself, not just the filer's interest in the entity) folded into the
    same output row via held_* columns rather than a separate table. C's
    two very different record shapes (income received vs. loans received)
    share one output table via a record_type column instead of two tables.

CLI:
  python california_disclosures_pipeline.py                 # current year, Schedule A-1
  python california_disclosures_pipeline.py --backfill       # full history
  python california_disclosures_pipeline.py --years 2024 2025
  python california_disclosures_pipeline.py --agency senate  # one chamber
  python california_disclosures_pipeline.py --schedules a1 a2 b c d e # multiple schedules

Outputs:
  storage/raw/california_disclosures/california_disclosures_{mode}_{YYYY}.parquet
  storage/raw/california_disclosures_business/california_disclosures_business_{mode}_{YYYY}.parquet
  storage/raw/california_disclosures_property/california_disclosures_property_{mode}_{YYYY}.parquet
  storage/raw/california_disclosures_income_loans/california_disclosures_income_loans_{mode}_{YYYY}.parquet
  storage/raw/california_disclosures_gifts/california_disclosures_gifts_{mode}_{YYYY}.parquet
  storage/raw/california_disclosures_travel_gifts/california_disclosures_travel_gifts_{mode}_{YYYY}.parquet
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


def _plausible_date(iso_date, filing_year, slack=2):
    """None out a date whose year is implausibly far from the filing year --
    a signal the extraction window landed on the wrong digits (e.g. a form
    revision with shifted row coordinates) rather than a real date."""
    if not iso_date or not filing_year:
        return iso_date
    if abs(int(iso_date[:4]) - int(filing_year)) > slack:
        return None
    return iso_date


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
        # The Schedule B template renders ASSESSOR'S with a curly apostrophe
        # (U+2019); labels are matched and excluded by ASCII token, so fold
        # curlies to ASCII here -- harmless for the other schedules' text.
        text = text.replace("\u2019", "'").replace("\u2018", "'")
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


def _digits_near(rows, x0, y0, dx_range, dy_range):
    """
    Digit tokens ("/"-stripped) within an offset window, sorted left-to-right
    by x -- NOT by (y, x) like _text_near. The renderer sometimes draws a
    digit run twice at a slightly different y (a visual-weight artifact);
    sorting by y first can interleave the duplicate with the next digit and
    scramble the date. Bucket by x, keep the topmost (min-y) duplicate.
    """
    by_x_bucket = {}
    for y, cells in rows.items():
        dy = y - y0
        if not (dy_range[0] <= dy <= dy_range[1]):
            continue
        for x, text in cells:
            dx = x - x0
            t = text.strip("/")
            if dx_range[0] <= dx <= dx_range[1] and t.isdigit():
                bucket = round(x / 8)
                if bucket not in by_x_bucket or y < by_x_bucket[bucket][1]:
                    by_x_bucket[bucket] = (x, y, t)
    return [t for _, _, t in sorted(by_x_bucket.values())]


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


def _checkbox_in_window(drawings, x0, x1, y0, y1):
    """True if any blue-filled mark's top-left corner falls in this window."""
    for d in drawings:
        if d.get("color") != (0.0, 0.0, 1.0):
            continue
        r = d["rect"]
        if x0 <= r.x0 <= x1 and y0 <= r.y0 <= y1:
            return True
    return False


def _nearest_checkbox_label(drawings, x0, y0, x_win, y_win, candidates,
                             max_dist=40.0):
    """
    Pick the label of the candidate (dx, dy, label) closest to a blue-filled
    mark found inside the (x0+x_win, y0+y_win) window. Schedule E's checkbox
    marks aren't positioned as consistently relative to their option text as
    Schedule A-1's are (~10pt of column-to-column slop vs ~4pt), so nearest-
    distance classification against the (template-fixed) option label
    positions is more robust here than a tight fixed-offset match.
    """
    best_label, best_dist = None, None
    for d in drawings:
        if d.get("color") != (0.0, 0.0, 1.0):
            continue
        r = d["rect"]
        if not (x0 + x_win[0] <= r.x0 <= x0 + x_win[1]
                and y0 + y_win[0] <= r.y0 <= y0 + y_win[1]):
            continue
        for dx, dy, label in candidates:
            dist = ((r.x0 - (x0 + dx)) ** 2 + (r.y0 - (y0 + dy)) ** 2) ** 0.5
            if best_dist is None or dist < best_dist:
                best_dist, best_label = dist, label
    return best_label if (best_dist is None or best_dist <= max_dist) else None


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


# -- Schedule A-2 parsing (Investments, Income, and Assets of Business
#    Entities/Trusts -- Ownership Interest is 10% or Greater) --------------
# 2 slots per page (one per column: left x~40, right x~310 on the 612-wide
# page), each with FOUR sub-sections: 1. the entity/trust itself (name,
# address, Trust-vs-Business-Entity checkbox, description, 5-bucket FMV +
# acquired/disposed dates, nature of investment + your business position),
# 2. gross income received from the entity (5-bucket checkbox), 3. reportable
# single income sources >= $10,000 (None / Names listed below + freetext),
# and 4. investments/real property HELD BY the entity itself (a nested
# Investment-vs-RealProperty slot with its own name/APN, description, 4-
# bucket FMV + dates, and 5-option nature-of-interest). All offsets below are
# relative to each field's own label anchor (found live on the page, same
# convention as Schedule B), derived from three live 2026 filings (Gallagher,
# Byors, Briseno) and cross-checked against each PDF's own checkbox-mark
# drawings before trusting a position -- see SESSION_NOTES for the
# reconciliation. A form redesign would need this re-derived, not tuned.
# Section 4's name/APN value is rendered TWICE in the PDF content stream (a
# few points apart, both with identical text) -- confirmed via a 4x-zoomed
# render, not a parsing bug -- so only the second (post-label) occurrence is
# read to avoid a duplicate.

_A2_SLOT_ANCHOR = ("1.", "BUSINESS", "ENTITY", "OR", "TRUST")
_A2_CHECKONE_LABEL = ("Check", "one")
_A2_DESC_LABEL = ("GENERAL", "DESCRIPTION", "OF", "THIS", "BUSINESS")
_A2_FMV_LABEL = ("FAIR", "MARKET", "VALUE")  # appears twice per slot
_A2_DATE_LABEL = ("IF", "APPLICABLE,", "LIST", "DATE:")  # appears twice per slot
_A2_NATURE_INV_LABEL = ("NATURE", "OF", "INVESTMENT")
_A2_POSITION_LABEL = ("YOUR", "BUSINESS", "POSITION")
_A2_INCOME_LABEL = ("IDENTIFY", "THE", "GROSS", "INCOME")
_A2_SOURCES_LABEL = ("3.", "LIST", "THE", "NAME", "OF", "EACH")
_A2_SECTION4_LABEL = ("4.", "INVESTMENTS", "AND", "INTERESTS")
_A2_CHECKONE2_LABEL = ("Check", "one", "box:")
_A2_NATURE_INT_LABEL = ("NATURE", "OF", "INTEREST")
_A2_COL_WIDTH = 300.0

_A2_CHECKONE_CANDIDATES = [(14.0, 11.7, "Trust"), (88.0, 11.7, "Business Entity")]
_A2_FMV_CANDIDATES = [
    (1.9, 12.1, "$0 - $1,999", 0, 1999),
    (1.9, 21.7, "$2,000 - $10,000", 2000, 10000),
    (1.9, 31.3, "$10,001 - $100,000", 10001, 100000),
    (1.9, 40.9, "$100,001 - $1,000,000", 100001, 1000000),
    (1.9, 50.5, "Over $1,000,000", 1000000, None),
]
_A2_FMV2_CANDIDATES = [
    (1.8, 12.3, "$2,000 - $10,000", 2000, 10000),
    (1.8, 21.9, "$10,001 - $100,000", 10001, 100000),
    (1.8, 31.5, "$100,001 - $1,000,000", 100001, 1000000),
    (1.8, 41.1, "Over $1,000,000", 1000000, None),
]
_A2_NATURE_INV_CANDIDATES = [
    (11.8, 10.0, "Partnership"), (68.8, 10.0, "Sole Proprietorship"),
    (138.7, 12.6, "Other"),
]
_A2_INCOME_CANDIDATES = [
    (-12.0, 27.0, "$0 - $499", 0, 499),
    (-12.0, 37.2, "$500 - $1,000", 500, 1000),
    (-12.0, 47.4, "$1,001 - $10,000", 1001, 10000),
    (83.0, 27.0, "$10,001 - $100,000", 10001, 100000),
    (83.0, 36.6, "OVER $100,000", 100001, None),
]
_A2_CHECKONE2_CANDIDATES = [(1.5, 14.9, "INVESTMENT"), (85.5, 14.9, "REAL PROPERTY")]
_A2_NATURE_INT_CANDIDATES = [
    (1.8, 12.2, "Property Ownership/Deed of Trust"), (145.8, 12.2, "Stock"),
    (196.8, 12.2, "Partnership"), (1.8, 32.6, "Leasehold"), (113.8, 32.6, "Other"),
]


def parse_schedule_a2(pdf_bytes, filing):
    """Extract Schedule A-2 rows (one per business-entity/trust slot) from
    one Form 700 PDF -- see the module comment above for the field map."""
    rows_out = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            text_upper = page.get_text().upper()
            if "SCHEDULE A-2" not in text_upper or "INVESTMENTS" not in text_upper:
                continue
            words = page.get_text("words")
            row_map = _words_by_row(words)
            drawings = page.get_drawings()

            for ax, ay in _find_anchors(row_map, _A2_SLOT_ANCHOR):
                entity = _text_near(row_map, ax, ay, (-5, 260), (10, 17),
                                     exclude=("1.", "BUSINESS", "ENTITY", "OR",
                                              "TRUST", "►"))
                if not entity:
                    continue  # unfilled template slot
                address = _text_near(row_map, ax, ay, (-5, 260), (32, 39))

                entity_type = None
                co_pick = _pick_same_column(
                    _find_anchors(row_map, _A2_CHECKONE_LABEL), ax, _A2_COL_WIDTH)
                if co_pick and 58 <= co_pick[1] - ay <= 68:
                    entity_type = _nearest_checkbox_label(
                        drawings, co_pick[0], co_pick[1], (-5, 100), (5, 20),
                        _A2_CHECKONE_CANDIDATES)

                description = _text_near(row_map, ax, ay, (-5, 260), (94, 101))

                fmv_range = fmv_min = fmv_max = None
                fmv_pick = min(
                    (p for p in _find_anchors(row_map, _A2_FMV_LABEL)
                     if abs(p[0] - ax) < 20 and 110 <= p[1] - ay <= 130),
                    key=lambda p: p[1], default=None)
                if fmv_pick:
                    label = _nearest_checkbox_label(
                        drawings, fmv_pick[0], fmv_pick[1], (-5, 110), (5, 55),
                        [(dx, dy, lbl) for dx, dy, lbl, _, _ in _A2_FMV_CANDIDATES])
                    if label:
                        lo, hi = next((lo, hi) for dx, dy, lbl, lo, hi
                                      in _A2_FMV_CANDIDATES if lbl == label)
                        fmv_range, fmv_min, fmv_max = label, lo, hi

                acquired = disposed = None
                date_pick = min(
                    (p for p in _find_anchors(row_map, _A2_DATE_LABEL)
                     if abs(p[0] - ax) < 130 and 110 <= p[1] - ay <= 130),
                    key=lambda p: p[1], default=None)
                if date_pick:
                    digits = _digits_near(row_map, date_pick[0], date_pick[1],
                                          (0, 160), (12, 22))
                    if len(digits) >= 3:
                        acquired = _plausible_date(
                            _mmddyy_to_iso(*digits[0:3]), filing["filing_year"])
                    if len(digits) >= 6:
                        disposed = _plausible_date(
                            _mmddyy_to_iso(*digits[3:6]), filing["filing_year"])

                nature_of_investment = None
                nature_pick = _pick_same_column(
                    _find_anchors(row_map, _A2_NATURE_INV_LABEL), ax, _A2_COL_WIDTH)
                if nature_pick and 183 <= nature_pick[1] - ay <= 194:
                    nature_of_investment = _nearest_checkbox_label(
                        drawings, nature_pick[0], nature_pick[1], (5, 145), (5, 18),
                        _A2_NATURE_INV_CANDIDATES)
                    if nature_of_investment == "Other":
                        describe = _text_near(
                            row_map, nature_pick[0], nature_pick[1], (145, 260),
                            (0, 5), exclude=("Other",))
                        if describe:
                            nature_of_investment = f"Other: {describe}"

                business_position = None
                pos_pick = _pick_same_column(
                    _find_anchors(row_map, _A2_POSITION_LABEL), ax, _A2_COL_WIDTH)
                if pos_pick and 216 <= pos_pick[1] - ay <= 228:
                    business_position = _text_near(
                        row_map, pos_pick[0], pos_pick[1], (0, 260), (-9, -3))

                income_range = income_min = income_max = None
                income_pick = _pick_same_column(
                    _find_anchors(row_map, _A2_INCOME_LABEL), ax, _A2_COL_WIDTH)
                if income_pick and 236 <= income_pick[1] - ay <= 248:
                    label = _nearest_checkbox_label(
                        drawings, income_pick[0], income_pick[1], (-15, 100),
                        (10, 55),
                        [(dx, dy, lbl) for dx, dy, lbl, _, _ in _A2_INCOME_CANDIDATES])
                    if label:
                        lo, hi = next((lo, hi) for dx, dy, lbl, lo, hi
                                      in _A2_INCOME_CANDIDATES if lbl == label)
                        income_range, income_min, income_max = label, lo, hi

                reportable_sources = None
                sources_pick = _pick_same_column(
                    _find_anchors(row_map, _A2_SOURCES_LABEL), ax, _A2_COL_WIDTH)
                if sources_pick and 295 <= sources_pick[1] - ay <= 305:
                    has_names = _checkbox_in_window(
                        drawings, sources_pick[0] + 45, sources_pick[0] + 70,
                        sources_pick[1] + 15, sources_pick[1] + 26)
                    if has_names:
                        reportable_sources = _text_near(
                            row_map, sources_pick[0], sources_pick[1], (-10, 260),
                            (25, 85), exclude=("None", "or", "Names", "listed",
                                                "below"))

                # -- Section 4: investments/real property held BY the entity --
                held_type = held_name = held_description = None
                held_fmv_range = held_fmv_min = held_fmv_max = None
                held_acquired = held_disposed = None
                held_nature = held_years_remaining = None
                sec4_pick = _pick_same_column(
                    _find_anchors(row_map, _A2_SECTION4_LABEL), ax, _A2_COL_WIDTH)
                if sec4_pick and 385 <= sec4_pick[1] - ay <= 395:
                    sx, sy = sec4_pick
                    co2_pick = _pick_same_column(
                        _find_anchors(row_map, _A2_CHECKONE2_LABEL), sx, _A2_COL_WIDTH)
                    if co2_pick and 15 <= co2_pick[1] - sy <= 22:
                        held_type = _nearest_checkbox_label(
                            drawings, co2_pick[0], co2_pick[1], (-5, 100), (10, 20),
                            _A2_CHECKONE2_CANDIDATES)

                    # The name/APN value renders once, right after the
                    # Investment/Real-Property checkbox row (dy~41 below the
                    # section-4 anchor), BEFORE its own "Name of Business
                    # Entity..." label (dy~56). A second value line at
                    # dy~72 -- after the label -- normally just repeats the
                    # same text (a render duplicate, confirmed via a 4x-zoom;
                    # see the module comment above parse_schedule_a2), but
                    # when the description field is also filled, THAT value
                    # appears at dy~72 instead (confirmed live: Gallagher's
                    # Letterkenny slot has the description "Farming Rio Oso"
                    # sitting in what would otherwise be the name's repeat
                    # slot). So: dy~41 is always the true name/APN; dy~72 is
                    # kept as the description only when it differs from the
                    # name (i.e. it wasn't just the duplicate).
                    held_name = _text_near(row_map, sx, sy, (-5, 260), (39, 43))
                    second_line = _text_near(row_map, sx, sy, (-5, 260), (70, 74))
                    if second_line and second_line != held_name:
                        held_description = second_line

                    fmv2_pick = min(
                        (p for p in _find_anchors(row_map, _A2_FMV_LABEL)
                         if abs(p[0] - sx) < 20 and 105 <= p[1] - sy <= 118),
                        key=lambda p: p[1], default=None)
                    if fmv2_pick:
                        label = _nearest_checkbox_label(
                            drawings, fmv2_pick[0], fmv2_pick[1], (-5, 105),
                            (5, 45),
                            [(dx, dy, lbl) for dx, dy, lbl, _, _ in _A2_FMV2_CANDIDATES])
                        if label:
                            lo, hi = next((lo, hi) for dx, dy, lbl, lo, hi
                                          in _A2_FMV2_CANDIDATES if lbl == label)
                            held_fmv_range, held_fmv_min, held_fmv_max = label, lo, hi

                    date2_pick = min(
                        (p for p in _find_anchors(row_map, _A2_DATE_LABEL)
                         if abs(p[0] - sx) < 130 and 105 <= p[1] - sy <= 118),
                        key=lambda p: p[1], default=None)
                    if date2_pick:
                        digits = _digits_near(row_map, date2_pick[0], date2_pick[1],
                                              (0, 160), (12, 22))
                        if len(digits) >= 3:
                            held_acquired = _plausible_date(
                                _mmddyy_to_iso(*digits[0:3]), filing["filing_year"])
                        if len(digits) >= 6:
                            held_disposed = _plausible_date(
                                _mmddyy_to_iso(*digits[3:6]), filing["filing_year"])

                    nature2_pick = _pick_same_column(
                        _find_anchors(row_map, _A2_NATURE_INT_LABEL), sx,
                        _A2_COL_WIDTH)
                    if nature2_pick and 160 <= nature2_pick[1] - sy <= 168:
                        held_nature = _nearest_checkbox_label(
                            drawings, nature2_pick[0], nature2_pick[1],
                            (-5, 200), (5, 40), _A2_NATURE_INT_CANDIDATES)
                        held_years_remaining = _text_near(
                            row_map, nature2_pick[0], nature2_pick[1], (60, 80),
                            (18, 25))
                        if held_nature == "Other":
                            describe = _text_near(
                                row_map, nature2_pick[0], nature2_pick[1],
                                (113, 260), (33, 40), exclude=("Other",))
                            if describe:
                                held_nature = f"Other: {describe}"

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
                    "address": address,
                    "entity_type": entity_type,
                    "description": description,
                    "fmv_range": fmv_range,
                    "fmv_min": fmv_min,
                    "fmv_max": fmv_max,
                    "acquired_date": acquired,
                    "disposed_date": disposed,
                    "nature_of_investment": nature_of_investment,
                    "business_position": business_position,
                    "gross_income_range": income_range,
                    "gross_income_min": income_min,
                    "gross_income_max": income_max,
                    "reportable_income_sources": reportable_sources,
                    "held_type": held_type,
                    "held_name": held_name,
                    "held_description": held_description,
                    "held_fmv_range": held_fmv_range,
                    "held_fmv_min": held_fmv_min,
                    "held_fmv_max": held_fmv_max,
                    "held_acquired_date": held_acquired,
                    "held_disposed_date": held_disposed,
                    "held_nature_of_interest": held_nature,
                    "held_years_remaining": held_years_remaining,
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


def _finalize_generic(rows, fetched_at):
    """Same as _finalize but for schedules with no acquired/disposed dates."""
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["fetched_at"] = fetched_at
    df = df.reset_index(drop=True)
    df["row_index"] = df.groupby("index_id").cumcount()
    return df


# -- Schedule D parsing (Income -- Gifts) --------------------------------
# 5 source slots per page (2 columns x 2 rows, plus a 5th sharing space with
# the signature block), each with up to 3 gift line items. No checkboxes.

_D_SLOT_ANCHOR = ("NAME", "OF", "SOURCE")
_D_LINE_DY = [(93, 112), (118, 138), (144, 163)]  # 3 gift-line dy bands


def parse_schedule_d(pdf_bytes, filing):
    """Extract Schedule D (Income -- Gifts) line items from one Form 700 PDF."""
    rows_out = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            if "SCHEDULE D" not in page.get_text().upper():
                continue
            words = page.get_text("words")
            row_map = _words_by_row(words)

            anchors = _find_anchors(row_map, _D_SLOT_ANCHOR)
            for ax, ay in anchors:
                source = _text_near(row_map, ax, ay, (0, 240), (8, 15),
                                     exclude=("NAME", "OF", "SOURCE",
                                              "(Not", "an", "Acronym)", "?"))
                if not source:
                    continue  # unfilled template slot
                address = _text_near(row_map, ax, ay, (0, 240), (35, 42),
                                      exclude=("ADDRESS", "(Business",
                                                "Address", "Acceptable)"))
                business = _text_near(row_map, ax, ay, (0, 240), (63, 70),
                                       exclude=("BUSINESS", "ACTIVITY,",
                                                 "IF", "ANY,", "OF", "SOURCE"))
                for lo, hi in _D_LINE_DY:
                    digits = _digits_near(row_map, ax, ay, (0, 46), (lo, hi))
                    value_txt = _text_near(row_map, ax, ay, (65, 90), (lo, hi))
                    desc = _text_near(row_map, ax, ay, (105, 240), (lo, hi))
                    date = _mmddyy_to_iso(*digits[0:3]) if len(digits) >= 3 else None
                    value = None
                    if value_txt:
                        try:
                            value = float(value_txt.replace("$", "").replace(",", ""))
                        except ValueError:
                            value = None
                    if not (date or value or desc):
                        continue  # blank template line
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
                        "source_name": source,
                        "source_address": address,
                        "source_business_activity": business,
                        "gift_date": date,
                        "gift_value": value,
                        "gift_description": desc,
                    })
    finally:
        doc.close()
    return rows_out


# -- Schedule E parsing (Income -- Gifts -- Travel Payments) ------------
# 4 source slots per page (2 columns x 2 rows). Each slot has a travel date
# RANGE (not a single date, unlike D), an amount, and three independent
# checkbox concepts: a 501(c)(3) toggle on the business-activity line, a
# Gift/Income choice, and a Speech-or-Panel/Other choice (with a free-text
# description when Other is picked). All offsets below are relative to the
# slot's own "NAME OF SOURCE" anchor, derived from a live filing (Sen.
# Cabaldon, 2024) and confirmed to reproduce identically across both columns
# for text; checkbox marks vary by ~10pt between columns, hence
# _nearest_checkbox_label instead of a fixed-offset table.

_E_SLOT_ANCHOR = ("NAME", "OF", "SOURCE")
_E_501C3_LABEL = ("501", "(c)(3)")

_E_GIFT_INCOME_CANDIDATES = [(91.2, 139.6, "Gift"), (148.5, 139.9, "Income")]
_E_SPEECH_OTHER_CANDIDATES = [(15.1, 157.5, "Speech"), (15.1, 175.6, "Other")]


def parse_schedule_e(pdf_bytes, filing):
    """Extract Schedule E (Income -- Gifts -- Travel Payments) line items."""
    rows_out = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            text_upper = page.get_text().upper()
            if "SCHEDULE E" not in text_upper:
                continue
            if "GIFT" not in text_upper and "TRAVEL" not in text_upper:
                continue  # cover-page mention of Schedule E, not the form page
            words = page.get_text("words")
            row_map = _words_by_row(words)
            drawings = page.get_drawings()

            anchors = _find_anchors(row_map, _E_SLOT_ANCHOR)
            for ax, ay in anchors:
                source = _text_near(row_map, ax, ay, (0, 260), (7, 14),
                                     exclude=("NAME", "OF", "SOURCE", "(Not",
                                               "an", "Acronym)"))
                if not source:
                    continue  # unfilled template slot
                address = _text_near(row_map, ax, ay, (0, 260), (24, 67),
                                      exclude=("ADDRESS", "(Business",
                                                "Address", "Acceptable)",
                                                "CITY", "AND", "STATE"))
                business = _text_near(row_map, ax, ay, (0, 260), (78, 88),
                                       exclude=("501", "(c)(3)", "or",
                                                 "DESCRIBE", "BUSINESS",
                                                 "ACTIVITY,", "IF", "ANY,",
                                                 "OF", "SOURCE"))
                source_501c3 = _checkbox_in_window(
                    drawings, ax - 8, ax + 18, ay + 78, ay + 92)

                digits = _digits_near(row_map, ax, ay, (20, 140), (105, 116))
                date_start = (_mmddyy_to_iso(*digits[0:3])
                              if len(digits) >= 3 else None)
                date_end = (_mmddyy_to_iso(*digits[3:6])
                            if len(digits) >= 6 else None)
                # Older form revisions (pre-2024/2025) shift this row's y
                # position enough that the digit window can grab unrelated
                # digits -- guard against a plausible-looking but wrong date
                # rather than let it silently corrupt the table.
                date_start = _plausible_date(date_start, filing["filing_year"])
                date_end = _plausible_date(date_end, filing["filing_year"])
                amt_txt = _text_near(row_map, ax, ay, (170, 235), (105, 116))
                amount = None
                if amt_txt:
                    try:
                        amount = float(amt_txt.replace("$", "").replace(",", ""))
                    except ValueError:
                        amount = None
                # A source PDF with a corrupted embedded font (rare, but seen
                # live -- MuPDF logs "unknown cid font type" for these) can
                # decode a $ token into garbage digits, e.g. a real filing
                # coming out as a $1.12 billion travel gift. Cap at a ceiling
                # well above any plausible legitimate travel reimbursement
                # (even large ones are 5-6 figures) rather than trust it.
                if amount is not None and amount > 5_000_000:
                    amount = None

                gift_or_income = _nearest_checkbox_label(
                    drawings, ax, ay, (60, 170), (135, 152),
                    _E_GIFT_INCOME_CANDIDATES)
                speech_or_other = _nearest_checkbox_label(
                    drawings, ax, ay, (-10, 40), (153, 190),
                    _E_SPEECH_OTHER_CANDIDATES)

                other_description = None
                if speech_or_other == "Other":
                    other_description = _text_near(
                        row_map, ax, ay, (0, 260), (179, 192))

                destination = _text_near(row_map, ax, ay, (-2, 260), (206, 213),
                                          exclude=("NAME", "OF", "SOURCE",
                                                    "(Not", "an", "Acronym)"))
                # Same class of issue as _plausible_date: on a layout the
                # coordinate table doesn't match, this window can land on
                # static boilerplate (a neighboring slot's header, the
                # page-level "Comments:" line) instead of real data.
                if destination and ("SOURCE" in destination.upper()
                                     or "COMMENTS" in destination.upper()):
                    destination = None

                if not (date_start or amount or gift_or_income or destination):
                    continue  # blank template slot with only a name printed

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
                    "source_name": source,
                    "source_address": address,
                    "source_business_activity": business,
                    "source_501c3": source_501c3,
                    "gift_or_income": gift_or_income,
                    "travel_date_start": date_start,
                    "travel_date_end": date_end,
                    "amount": amount,
                    "speech_or_other": speech_or_other,
                    "other_description": other_description,
                    "travel_destination": destination,
                })
    finally:
        doc.close()
    return rows_out


# -- Schedule B parsing (Interests in Real Property) ----------------------
# 2 property slots per page (one per column: left x~58, right x~320 on the
# 612-wide page). The 2025/2026 template also stacks a loans subsection
# (NAME OF LENDER, HIGHEST BALANCE...) below the property slots on the same
# page -- ignored here (that belongs to Schedule C). All offsets below are
# relative to the slot's own "ASSESSOR'S PARCEL NUMBER OR STREET ADDRESS"
# label (or each checkbox group's own label), derived from two live 2025
# filings (Petrie-Norris 932 Catalina St; Lowenthal) and identical across
# both columns. The checkbox marks reuse _nearest_checkbox_label -- same
# Schedule E lesson: ~10pt of column-to-column positional slop rules out a
# fixed-offset table. A form redesign would need this re-derived, not tuned.
# The acquired/disposed dates share the "IF APPLICABLE, LIST DATE:" row that
# A-1's dates use; on B pages the renderer pre-prints the 2-digit year into
# the blank fields, so dates come out None unless actually filled.

_B_SLOT_ANCHOR = ("ASSESSOR'S", "PARCEL", "NUMBER", "OR", "STREET", "ADDRESS")
_B_CITY_LABEL = ("CITY",)
_B_FMV_LABEL = ("FAIR", "MARKET", "VALUE")
_B_NATURE_LABEL = ("NATURE", "OF", "INTEREST")
_B_RENTAL_LABEL = ("IF", "RENTAL", "PROPERTY,", "GROSS", "INCOME", "RECEIVED")
_B_SOURCES_LABEL = ("SOURCES", "OF", "RENTAL", "INCOME:")
_B_DATE_LABEL = ("IF", "APPLICABLE,", "LIST", "DATE:")

# (dx, dy, label) of each checkbox, relative to its group's label anchor.
_B_FMV_CANDIDATES = [
    (-0.2, 10.3, "$2,000 - $10,000"),
    (-0.2, 20.9, "$10,001 - $100,000"),
    (-0.2, 31.6, "$100,001 - $1,000,000"),
    (-0.2, 42.1, "Over $1,000,000"),
]
_B_FMV_RANGES = {
    "$2,000 - $10,000": (2000, 10000),
    "$10,001 - $100,000": (10001, 100000),
    "$100,001 - $1,000,000": (100001, 1000000),
    "Over $1,000,000": (1000000, None),
}
_B_NATURE_CANDIDATES = [
    (-0.2, 13.4, "Ownership/Deed of Trust"),
    (125.8, 13.4, "Easement"),
    (-0.2, 33.8, "Leasehold"),
    (125.8, 33.7, "Other"),
]
_B_RENTAL_CANDIDATES = [
    (0.0, 15.2, "$0 - $499"),
    (63.8, 15.2, "$500 - $1,000"),
    (144.0, 15.2, "$1,001 - $10,000"),
    (0.0, 30.4, "$10,001 - $100,000"),
    (108.6, 30.4, "OVER $100,000"),
]
_B_COL_WIDTH = 300.0  # x dividing the page's left and right property columns


def _pick_same_column(anchors, ax, col_width):
    """Nearest anchor in the same column (left/right half) as x=ax."""
    left_col = ax < col_width
    matches = [p for p in anchors if (p[0] < col_width) == left_col]
    return min(matches, key=lambda p: p[1]) if matches else None


def parse_schedule_b(pdf_bytes, filing):
    """Extract Schedule B (Interests in Real Property) rows from one PDF."""
    rows_out = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            text_upper = page.get_text().upper()
            if "SCHEDULE B" not in text_upper:
                continue
            if "INTERESTS IN REAL PROPERTY" not in text_upper:
                continue  # cover-page mention of Schedule B, not the form page
            words = page.get_text("words")
            row_map = _words_by_row(words)
            drawings = page.get_drawings()

            anchors = _find_anchors(row_map, _B_SLOT_ANCHOR)
            city_anchors = _find_anchors(row_map, _B_CITY_LABEL)
            fmv_anchors = _find_anchors(row_map, _B_FMV_LABEL)
            nature_anchors = _find_anchors(row_map, _B_NATURE_LABEL)
            rental_anchors = _find_anchors(row_map, _B_RENTAL_LABEL)
            sources_anchors = _find_anchors(row_map, _B_SOURCES_LABEL)
            date_anchors = _find_anchors(row_map, _B_DATE_LABEL)

            for ax, ay in anchors:
                property_address = _text_near(
                    row_map, ax, ay, (0, 235), (11, 30),
                    exclude=("ASSESSOR'S", "PARCEL", "NUMBER", "OR", "STREET",
                             "ADDRESS", "CITY", "►"))
                if not property_address:
                    continue  # unfilled template slot

                city = None
                city_pick = _pick_same_column(city_anchors, ax, _B_COL_WIDTH)
                if city_pick and 25 <= city_pick[1] - ay <= 35:
                    city = _text_near(row_map, city_pick[0], city_pick[1],
                                      (0, 235), (11, 30), exclude=("CITY",))

                acquired = disposed = None
                date_pick = _pick_same_column(date_anchors, ax, _B_COL_WIDTH)
                if date_pick and 55 <= date_pick[1] - ay <= 70:
                    digits = _digits_near(row_map, date_pick[0], date_pick[1],
                                          (0, 160), (12, 22))
                    if len(digits) >= 3:
                        acquired = _plausible_date(
                            _mmddyy_to_iso(*digits[0:3]),
                            filing["filing_year"])
                    if len(digits) >= 6:
                        disposed = _plausible_date(
                            _mmddyy_to_iso(*digits[3:6]),
                            filing["filing_year"])

                fmv_range = fmv_min = fmv_max = None
                fmv_pick = _pick_same_column(fmv_anchors, ax, _B_COL_WIDTH)
                if fmv_pick and 55 <= fmv_pick[1] - ay <= 70:
                    label = _nearest_checkbox_label(
                        drawings, fmv_pick[0], fmv_pick[1], (-8, 125), (5, 50),
                        _B_FMV_CANDIDATES)
                    if label:
                        fmv_range, fmv_min, fmv_max = (label,
                                                       *_B_FMV_RANGES[label])

                nature = None
                nature_pick = _pick_same_column(nature_anchors, ax,
                                                _B_COL_WIDTH)
                if nature_pick and 115 <= nature_pick[1] - ay <= 130:
                    nature = _nearest_checkbox_label(
                        drawings, nature_pick[0], nature_pick[1],
                        (-8, 140), (8, 45), _B_NATURE_CANDIDATES)
                    if nature == "Other":
                        describe = _text_near(
                            row_map, nature_pick[0], nature_pick[1],
                            (165, 235), (41, 50), exclude=("Other",))
                        if describe:
                            nature = f"Other: {describe}"

                rental_income = None
                rental_pick = _pick_same_column(rental_anchors, ax,
                                                _B_COL_WIDTH)
                if rental_pick and 175 <= rental_pick[1] - ay <= 190:
                    rental_income = _nearest_checkbox_label(
                        drawings, rental_pick[0], rental_pick[1],
                        (-8, 165), (5, 40), _B_RENTAL_CANDIDATES)

                rental_sources = None
                sources_pick = _pick_same_column(sources_anchors, ax,
                                                 _B_COL_WIDTH)
                if sources_pick and 225 <= sources_pick[1] - ay <= 240:
                    rental_sources = _text_near(
                        row_map, sources_pick[0], sources_pick[1],
                        (0, 235), (35, 80), exclude=("None", "OF", "$10,000",
                                                      "OR", "MORE."))

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
                    "property_address": property_address,
                    "city": city,
                    "nature_of_interest": nature,
                    "fmv_range": fmv_range,
                    "fmv_min": fmv_min,
                    "fmv_max": fmv_max,
                    "rental_income_range": rental_income,
                    "rental_sources": rental_sources,
                    "acquired_date": acquired,
                    "disposed_date": disposed,
                })
    finally:
        doc.close()
    return rows_out


# -- Schedule C parsing (Income, Loans, & Business Positions -- other than
#    gifts and travel payments) --------------------------------------------
# Two record types share one page, both handled by parse_schedule_c and
# tagged with a "record_type" column rather than split into two output
# tables (matches how BOTH sub-sections are just "Schedule C" on the real
# form, unlike D/E which are separately-lettered schedules):
#   1. INCOME RECEIVED -- 2 slots per page (one per column), source name/
#      address/business activity/your position, 5-bucket gross-income
#      checkbox, and an 8-option "consideration for which income was
#      received" checkbox. Describe-freetext is only captured for the
#      "Other" option (the common case) -- Sale-of/Rental-Income's describe
#      lines sit in different relative positions and are left unparsed,
#      same scoping choice as Schedule B's ignored loans subsection.
#   2. LOANS RECEIVED OR OUTSTANDING -- ONE record per page (full width, not
#      per-column): lender name/address/business activity, 4-bucket highest-
#      balance checkbox, interest rate (%  or a "None" checkbox), term, and
#      a 5-option security-for-loan checkbox (None/Personal residence/Real
#      Property+address/Guarantor+name/Other+describe, captured together as
#      one loose `security_detail` freetext field rather than per-option
#      sub-fields). Field-column disambiguation matters here even though
#      it's "one record, not two slots": name/address/activity/balance
#      (left) and rate/term/security (right) render on the SAME text rows,
#      so an uncapped dx window bled the right column into the left one's
#      values (caught live: rate "7.625"/term "0" leaking into lender_name,
#      "SECURITY FOR LOAN" leaking into address) -- fixed by capping dx to
#      260, the same column width used everywhere else on this form family.
#      Verified against two live filled loans (David Couch/Tri Counties
#      Bank, 7.625%/Personal residence; a Singh filing/Wells Fargo, 24%/
#      None) -- both reproduce every field exactly.

_C_INCOME_SLOT_ANCHOR = ("1.", "INCOME", "RECEIVED")
_C_SOURCE_LABEL = ("NAME", "OF", "SOURCE", "OF", "INCOME")
_C_ADDRESS_LABEL = ("ADDRESS",)
_C_ACTIVITY_LABEL = ("BUSINESS", "ACTIVITY,", "IF", "ANY,", "OF", "SOURCE")
_C_POSITION_LABEL = ("YOUR", "BUSINESS", "POSITION")
_C_GROSS_LABEL = ("GROSS", "INCOME", "RECEIVED")
_C_CONSIDERATION_LABEL = ("CONSIDERATION", "FOR", "WHICH", "INCOME", "WAS",
                          "RECEIVED")
_C_LOAN_ANCHOR = ("2.", "LOANS", "RECEIVED", "OR", "OUTSTANDING")
_C_LENDER_LABEL = ("NAME", "OF", "LENDER*")
_C_LENDER_ACTIVITY_LABEL = ("BUSINESS", "ACTIVITY,", "IF", "ANY,", "OF", "LENDER")
_C_BALANCE_LABEL = ("HIGHEST", "BALANCE", "DURING", "REPORTING", "PERIOD")
_C_RATE_LABEL = ("INTEREST", "RATE")
_C_TERM_LABEL = ("TERM", "(Months/Years)")
_C_SECURITY_LABEL = ("SECURITY", "FOR", "LOAN")
_C_COL_WIDTH = 300.0

_C_GROSS_CANDIDATES = [
    (113.3, 2.6, "No Income - Business Position Only", None, None),
    (1.9, 15.3, "$500 - $1,000", 500, 1000),
    (114.1, 15.5, "$1,001 - $10,000", 1001, 10000),
    (1.9, 27.5, "$10,001 - $100,000", 10001, 100000),
    (114.1, 27.5, "OVER $100,000", 100001, None),
]
_C_CONSIDERATION_CANDIDATES = [
    (1.9, 14.2, "Salary"),
    (49.9, 14.2, "Spouse's or registered domestic partner's income"),
    (1.9, 38.8, "Partnership (< 10% ownership)"),
    (1.9, 66.4, "Sale of"),
    (2.6, 85.4, "Loan repayment"),
    (2.6, 102.4, "Commission"),
    (76.3, 102.4, "Rental Income"),
    (1.9, 142.7, "Other"),
]
_C_BALANCE_CANDIDATES = [
    (1.9, 16.9, "$500 - $1,000", 500, 1000),
    (1.9, 32.5, "$1,001 - $10,000", 1001, 10000),
    (1.9, 48.1, "$10,001 - $100,000", 10001, 100000),
    (1.9, 63.7, "OVER $100,000", 100001, None),
]
_C_SECURITY_CANDIDATES = [
    (1.9, 15.2, "None"), (73.9, 15.2, "Personal residence"),
    (1.9, 38.0, "Real Property"), (1.9, 88.8, "Guarantor"), (1.9, 116.4, "Other"),
]


def _parse_c_income_slot(row_map, drawings, ax, ay, filing):
    source_name = _text_near(row_map, ax, ay, (-5, 260), (20, 27),
                              exclude=("1.", "INCOME", "RECEIVED", "►"))
    if not source_name:
        return None
    address = _text_near(row_map, ax, ay, (-5, 260), (50, 56))
    business_activity = _text_near(row_map, ax, ay, (-5, 260), (79, 85))
    business_position = _text_near(row_map, ax, ay, (-5, 260), (108, 113))

    gross_range = gross_min = gross_max = None
    gross_pick = _pick_same_column(
        _find_anchors(row_map, _C_GROSS_LABEL), ax, _C_COL_WIDTH)
    if gross_pick and 125 <= gross_pick[1] - ay <= 135:
        label = _nearest_checkbox_label(
            drawings, gross_pick[0], gross_pick[1], (-15, 130), (0, 32),
            [(dx, dy, lbl) for dx, dy, lbl, _, _ in _C_GROSS_CANDIDATES])
        if label:
            lo, hi = next((lo, hi) for dx, dy, lbl, lo, hi
                          in _C_GROSS_CANDIDATES if lbl == label)
            gross_range, gross_min, gross_max = label, lo, hi

    consideration = consideration_describe = None
    cons_pick = _pick_same_column(
        _find_anchors(row_map, _C_CONSIDERATION_LABEL), ax, _C_COL_WIDTH)
    if cons_pick and 166 <= cons_pick[1] - ay <= 176:
        consideration = _nearest_checkbox_label(
            drawings, cons_pick[0], cons_pick[1], (-5, 100), (10, 150),
            _C_CONSIDERATION_CANDIDATES)
        if consideration == "Other":
            candidate_dx, candidate_dy = next(
                (dx, dy) for dx, dy, lbl in _C_CONSIDERATION_CANDIDATES
                if lbl == "Other")
            describe = _text_near(
                row_map, cons_pick[0] + candidate_dx, cons_pick[1] + candidate_dy,
                (0, 260), (-13, -7), exclude=("Other",))
            if describe:
                consideration_describe = describe

    return {
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
        "record_type": "income",
        "source_name": source_name,
        "source_address": address,
        "business_activity": business_activity,
        "business_position": business_position,
        "gross_income_range": gross_range,
        "gross_income_min": gross_min,
        "gross_income_max": gross_max,
        "consideration": consideration,
        "consideration_describe": consideration_describe,
        "lender_name": None, "lender_address": None,
        "lender_business_activity": None,
        "highest_balance_range": None, "highest_balance_min": None,
        "highest_balance_max": None, "interest_rate_pct": None,
        "interest_rate_none": None, "term": None, "security_type": None,
        "security_detail": None,
    }


def _parse_c_loan(row_map, drawings, filing):
    loan_anchors = _find_anchors(row_map, _C_LOAN_ANCHOR)
    if not loan_anchors:
        return None
    ax, ay = loan_anchors[0]

    # dx capped at 260 (not the full page width) -- the loan record spans
    # the page but its fields are still two columns (lender name/address/
    # activity/balance on the left, rate/term/security on the right); an
    # uncapped window on a shared text row bled the right column's values
    # into these left-column fields (confirmed live: rate "7.625" and term
    # "0" leaking into lender_name, "SECURITY FOR LOAN" leaking into
    # address) before this was caught and fixed.
    lender_name = _text_near(row_map, ax, ay, (-10, 260), (82, 90),
                              exclude=("2.", "LOANS", "RECEIVED", "OR",
                                       "OUTSTANDING", "DURING", "THE",
                                       "REPORTING", "PERIOD", "►"))
    if not lender_name:
        return None
    address = _text_near(row_map, ax, ay, (-10, 260), (111, 119))
    business_activity = _text_near(row_map, ax, ay, (-10, 260), (140, 148))

    balance_range = balance_min = balance_max = None
    balance_pick = next(iter(_find_anchors(row_map, _C_BALANCE_LABEL)), None)
    if balance_pick and 160 <= balance_pick[1] - ay <= 170:
        label = _nearest_checkbox_label(
            drawings, balance_pick[0], balance_pick[1], (-5, 15), (10, 70),
            [(dx, dy, lbl) for dx, dy, lbl, _, _ in _C_BALANCE_CANDIDATES])
        if label:
            lo, hi = next((lo, hi) for dx, dy, lbl, lo, hi
                          in _C_BALANCE_CANDIDATES if lbl == label)
            balance_range, balance_min, balance_max = label, lo, hi

    rate_pct = rate_none = None
    rate_pick = next(iter(_find_anchors(row_map, _C_RATE_LABEL)), None)
    if rate_pick and 70 <= rate_pick[1] - ay <= 78:
        rate_none = _checkbox_in_window(
            drawings, rate_pick[0] + 60, rate_pick[0] + 80,
            rate_pick[1] + 18, rate_pick[1] + 28)
        if not rate_none:
            rate_text = _text_near(row_map, rate_pick[0], rate_pick[1],
                                   (5, 45), (8, 15))
            if rate_text:
                try:
                    rate_pct = float(rate_text.rstrip("%").strip())
                except ValueError:
                    rate_pct = None

    term = None
    term_pick = next(iter(_find_anchors(row_map, _C_TERM_LABEL)), None)
    if term_pick and 70 <= term_pick[1] - ay <= 78:
        term = _text_near(row_map, term_pick[0], term_pick[1], (0, 150), (8, 17))

    security_type = security_detail = None
    sec_pick = next(iter(_find_anchors(row_map, _C_SECURITY_LABEL)), None)
    if sec_pick and 112 <= sec_pick[1] - ay <= 122:
        security_type = _nearest_checkbox_label(
            drawings, sec_pick[0], sec_pick[1], (-5, 80), (10, 120),
            _C_SECURITY_CANDIDATES)
        if security_type and security_type != "None":
            candidate_dx, candidate_dy = next(
                (dx, dy) for dx, dy, lbl in _C_SECURITY_CANDIDATES
                if lbl == security_type)
            security_detail = _text_near(
                row_map, sec_pick[0] + candidate_dx, sec_pick[1] + candidate_dy,
                (10, 260), (-2, 45),
                exclude=("Street", "address", "City", "Guarantor", "Other",
                         "Real", "Property", "Personal", "residence"))

    return {
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
        "record_type": "loan",
        "source_name": None, "source_address": None, "business_activity": None,
        "business_position": None, "gross_income_range": None,
        "gross_income_min": None, "gross_income_max": None,
        "consideration": None, "consideration_describe": None,
        "lender_name": lender_name,
        "lender_address": address,
        "lender_business_activity": business_activity,
        "highest_balance_range": balance_range,
        "highest_balance_min": balance_min,
        "highest_balance_max": balance_max,
        "interest_rate_pct": rate_pct,
        "interest_rate_none": rate_none,
        "term": term,
        "security_type": security_type,
        "security_detail": security_detail,
    }


def parse_schedule_c(pdf_bytes, filing):
    """Extract Schedule C rows (income + loan records, tagged by
    record_type) from one Form 700 PDF -- see the module comment above."""
    rows_out = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            text_upper = page.get_text().upper()
            if "SCHEDULE C" not in text_upper or "INCOME, LOANS" not in text_upper:
                continue
            if "NAME OF LENDER" not in text_upper:
                continue  # cover-page mention of Schedule C, not the form page
            words = page.get_text("words")
            row_map = _words_by_row(words)
            drawings = page.get_drawings()

            for ax, ay in _find_anchors(row_map, _C_INCOME_SLOT_ANCHOR):
                row = _parse_c_income_slot(row_map, drawings, ax, ay, filing)
                if row:
                    rows_out.append(row)

            loan_row = _parse_c_loan(row_map, drawings, filing)
            if loan_row:
                rows_out.append(loan_row)
    finally:
        doc.close()
    return rows_out


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
# Each schedule: (parser, finalizer, output dir, output filename prefix).
# collect_year fetches each filing's PDF once and runs every enabled
# schedule's parser against it, so a multi-schedule run costs one PDF fetch
# per filing, not one per schedule.

SCHEDULES = {
    "a1": (parse_schedule_a1, _finalize, OUTPUT_DIR, "california_disclosures"),
    "b": (parse_schedule_b, _finalize,
          os.path.join("storage", "raw", "california_disclosures_property"),
          "california_disclosures_property"),
    "d": (parse_schedule_d, _finalize_generic,
          os.path.join("storage", "raw", "california_disclosures_gifts"),
          "california_disclosures_gifts"),
    "e": (parse_schedule_e, _finalize_generic,
          os.path.join("storage", "raw", "california_disclosures_travel_gifts"),
          "california_disclosures_travel_gifts"),
    "a2": (parse_schedule_a2, _finalize,
           os.path.join("storage", "raw", "california_disclosures_business"),
           "california_disclosures_business"),
    "c": (parse_schedule_c, _finalize_generic,
          os.path.join("storage", "raw", "california_disclosures_income_loans"),
          "california_disclosures_income_loans"),
}


def collect_year(session, agency_key, agency_label, year, mode, fetched_at,
                  schedules=("a1",)):
    print(f"\n[{agency_key} {year}] searching filings...")
    filings = fetch_filing_index(session, agency_label, year)
    if not filings:
        print(f"  no filings for {year}")
        return
    print(f"  {len(filings)} filings")

    os.makedirs(CACHE_DIR, exist_ok=True)
    state = {}
    for sched in schedules:
        _, _, out_dir, prefix = SCHEDULES[sched]
        os.makedirs(out_dir, exist_ok=True)
        checkpoint = os.path.join(
            CACHE_DIR, f"{sched}_{agency_key}_{year}_partial.parquet")
        rows, done = _load_checkpoint(checkpoint)
        if done:
            print(f"  [{sched}] resuming: {len(done)} filings already parsed")
        state[sched] = {"checkpoint": checkpoint, "rows": rows, "done": done,
                         "no_data": 0}

    # A filing is "fully done" once every requested schedule has processed
    # it -- resuming only re-fetches filings still missing from some schedule.
    fully_done = set.intersection(
        *(state[s]["done"] for s in schedules)) if schedules else set()

    for i, filing in enumerate(filings, 1):
        index_id = filing["index_id"]
        if index_id in fully_done:
            continue
        pending = [s for s in schedules if index_id not in state[s]["done"]]
        pdf_bytes = _fetch_pdf(session, index_id) if pending else None
        for sched in pending:
            parser_fn = SCHEDULES[sched][0]
            if pdf_bytes is None:
                state[sched]["no_data"] += 1
            else:
                try:
                    parsed = parser_fn(pdf_bytes, filing)
                except Exception as exc:
                    print(f"    [{sched}] parse failed for {index_id}: {exc}")
                    parsed = []
                if parsed:
                    state[sched]["rows"].extend(parsed)
                else:
                    state[sched]["no_data"] += 1
            state[sched]["done"].add(index_id)

        if i % CHECKPOINT_EVERY == 0:
            for sched in schedules:
                s = state[sched]
                _save_checkpoint(s["checkpoint"], s["rows"], s["done"])
            print(f"    {i}/{len(filings)} filings")

    for sched in schedules:
        s = state[sched]
        _, finalize_fn, out_dir, prefix = SCHEDULES[sched]
        df = finalize_fn(s["rows"], fetched_at)
        if df.empty:
            print(f"  [{sched}] {year}: no rows parsed")
            _clear_checkpoint(s["checkpoint"])
            continue
        path = write_partitioned(
            df, out_dir, f"{prefix}_{agency_key}_{mode}_{year}.parquet")
        print(f"  [{sched}] -> {path}  ({len(df):,} rows, "
              f"{df['filer_last_name'].nunique()} filers, "
              f"{s['no_data']} filings with no data / unfetchable)")
        _clear_checkpoint(s["checkpoint"])


def main():
    parser = argparse.ArgumentParser(
        description="California legislature Form 700 schedules A-1/A-2/B/C/D/E "
                    "(keyless, FPPC official source)")
    parser.add_argument("--backfill", action="store_true",
                        help=f"Full history ({FIRST_YEAR}+)")
    parser.add_argument("--years", nargs="+", type=int,
                        help="Explicit years to fetch (overrides --backfill)")
    parser.add_argument("--agency", choices=["senate", "assembly", "both"],
                        default="both", help="Limit to one chamber")
    parser.add_argument("--schedules", nargs="+", choices=list(SCHEDULES),
                        default=["a1"], help="Which schedules to parse")
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
          "(keyless, FPPC Form 700)")
    print(f"  mode={mode}  years={years[0]}-{years[-1]}  "
          f"agencies={[a for a, _ in agencies]}  schedules={args.schedules}")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    for agency_key, agency_label in agencies:
        for year in years:
            collect_year(session, agency_key, agency_label, year, mode,
                         fetched_at, schedules=args.schedules)

    print("\n--- CALIFORNIA DISCLOSURES PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
