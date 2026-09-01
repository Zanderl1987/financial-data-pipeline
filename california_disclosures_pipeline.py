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
  - Schedule A-1 (investments), Schedule D (gifts), and Schedule E (income --
    gifts -- travel payments) are parsed, each to its own output table -- one
    PDF fetch per filing serves every enabled schedule via SCHEDULES /
    --schedules. Schedule D has 5 source slots/page, up to 3 gift line items
    each, no checkboxes. Same digit-duplication render artifact as A-1's
    dates -- see _digits_near. Schedule E has 4 source slots/page (2 cols x
    2 rows), a travel date RANGE per slot (not a single date), and three
    checkbox concepts (501(c)(3) toggle, Gift/Income, Speech-or-Panel/Other)
    -- its checkbox marks have more column-to-column positional slop than
    A-1's (~10pt vs ~4pt), so nearest-label distance matching is used
    instead of a fixed-offset table -- see _nearest_checkbox_label.
  - Schedules B, C, A-2 not yet built -- see
    work-notes/financial-data-pipeline/TASKS.md.

CLI:
  python california_disclosures_pipeline.py                 # current year, Schedule A-1
  python california_disclosures_pipeline.py --backfill       # full history
  python california_disclosures_pipeline.py --years 2024 2025
  python california_disclosures_pipeline.py --agency senate  # one chamber
  python california_disclosures_pipeline.py --schedules a1 d e # multiple schedules

Outputs:
  storage/raw/california_disclosures/california_disclosures_{mode}_{YYYY}.parquet
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
    "d": (parse_schedule_d, _finalize_generic,
          os.path.join("storage", "raw", "california_disclosures_gifts"),
          "california_disclosures_gifts"),
    "e": (parse_schedule_e, _finalize_generic,
          os.path.join("storage", "raw", "california_disclosures_travel_gifts"),
          "california_disclosures_travel_gifts"),
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
        description="California legislature Form 700 Schedule A-1 "
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
