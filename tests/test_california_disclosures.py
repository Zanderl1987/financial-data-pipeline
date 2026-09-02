"""
Offline tests for california_disclosures_pipeline.

No network: SearchDocuments/GetRedactedFormPdf responses are built as the same
double-JSON-encoded shape captured live from form700search.fppc.ca.gov on
2026-08-31. The Schedule A-1/D PDF parsers themselves (parse_schedule_a1,
parse_schedule_d) are exercised only live -- same precedent as
congressional_trades_pipeline's parse_house_ptr, which has no offline PDF
fixture either.
"""

import json
import os

import pandas as pd
import pytest

import california_disclosures_pipeline as cdp


class _FakeResponse:
    def __init__(self, json_payload=None, content=b"", status_code=200,
                 content_type="application/pdf"):
        self._json_payload = json_payload
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def json(self):
        return self._json_payload


class _FakeSession:
    """Records calls and returns queued responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(("post", url, json))
        return self._responses.pop(0)

    def get(self, url, timeout=None):
        self.calls.append(("get", url, None))
        return self._responses.pop(0)


# ── SearchDocuments (double-JSON-encoded) ───────────────────────────────────

SEARCH_PAYLOAD = {
    "total": 2,
    "documents": [
        {
            "filingInfo": {"noReportableInterests": False,
                            "isAmendment": False,
                            "filedDate": "2026-02-27T10:43:00"},
            "filer": {"lastName": "Choi", "firstName": "Steven",
                      "middleName": "S"},
            "indexID": "abc-123",
            "filingPositions": [
                {"agency": "State Senate", "dueDate": "03/02/2026",
                 "filingType": "Annual", "filingYear": 2024,
                 "position": "Senator"},
            ],
        },
        {
            # Two positions -- must pick the one matching the requested
            # agency, not just the first.
            "filingInfo": {"noReportableInterests": True,
                            "isAmendment": True,
                            "filedDate": "2026-03-13T00:00:00"},
            "filer": {"lastName": "Roth", "firstName": "Richard"},
            "indexID": "def-456",
            "filingPositions": [
                {"agency": "City of Riverside", "filingType": "Leaving",
                 "filingYear": 2024, "position": "Council Member"},
                {"agency": "State Senate", "filingType": "Leaving",
                 "filingYear": 2024, "position": "Senator"},
            ],
        },
    ],
}


class TestFetchFilingIndex:

    def _session(self):
        # The real endpoint returns a JSON string containing the payload,
        # not the payload itself.
        return _FakeSession([_FakeResponse(json.dumps(SEARCH_PAYLOAD))])

    def test_parses_filer_and_filing_fields(self):
        filings = cdp.fetch_filing_index(self._session(), "State Senate", 2024)
        assert len(filings) == 2
        choi = filings[0]
        assert choi["index_id"] == "abc-123"
        assert choi["filer_last_name"] == "Choi"
        assert choi["filer_first_name"] == "Steven"
        assert choi["agency"] == "State Senate"
        assert choi["position"] == "Senator"
        assert choi["filing_type"] == "Annual"
        assert choi["filing_year"] == 2024
        assert choi["filed_date"] == "2026-02-27"
        assert choi["is_amendment"] is False
        assert choi["no_reportable_interests"] is False

    def test_picks_the_position_matching_the_requested_agency(self):
        filings = cdp.fetch_filing_index(self._session(), "State Senate", 2024)
        roth = filings[1]
        assert roth["agency"] == "State Senate"
        assert roth["position"] == "Senator"

    def test_handles_a_plain_dict_response_too(self):
        # Defensive: parse cleanly even if the double-encoding is ever fixed.
        session = _FakeSession([_FakeResponse(SEARCH_PAYLOAD)])
        filings = cdp.fetch_filing_index(session, "State Senate", 2024)
        assert len(filings) == 2

    def test_malformed_response_returns_empty_after_retries(self):
        session = _FakeSession([_FakeResponse("not json{{"),
                                 _FakeResponse("not json{{"),
                                 _FakeResponse("not json{{")])
        assert cdp.fetch_filing_index(session, "State Senate", 2024) == []


# ── PDF fetch (session-cookie-bound download) ───────────────────────────────

class TestFetchPdf:

    def test_returns_pdf_bytes_on_success(self):
        session = _FakeSession([
            _FakeResponse({"PDFDownloadUrl": cdp.BASE + "/Home/DownloadPdf?key=x"}),
            _FakeResponse(content=b"%PDF-1.4 ...", content_type="application/pdf"),
        ])
        assert cdp._fetch_pdf(session, "abc-123") == b"%PDF-1.4 ..."

    def test_missing_download_url_returns_none(self):
        session = _FakeSession([_FakeResponse({"PDFDownloadUrl": None})])
        assert cdp._fetch_pdf(session, "abc-123") is None

    def test_error_page_instead_of_pdf_returns_none(self):
        # form700search occasionally 200s an HTML error page in place of the
        # PDF (session/backend hiccup) -- must not be mistaken for real data.
        session = _FakeSession([
            _FakeResponse({"PDFDownloadUrl": cdp.BASE + "/Home/DownloadPdf?key=x"}),
            _FakeResponse(content=b"<html>ERROR</html>", content_type="text/html"),
        ])
        assert cdp._fetch_pdf(session, "abc-123") is None


# ── Date parsing ─────────────────────────────────────────────────────────

class TestMmddyyToIso:

    def test_two_digit_year_assumed_2000s(self):
        assert cdp._mmddyy_to_iso("02", "10", "24") == "2024-02-10"

    def test_four_digit_year_passthrough(self):
        assert cdp._mmddyy_to_iso("02", "10", "2024") == "2024-02-10"

    def test_invalid_date_returns_none(self):
        assert cdp._mmddyy_to_iso("13", "40", "24") is None


class TestToDate:

    def test_iso_timestamp(self):
        assert cdp._to_date("2026-02-27T10:43:16") == "2026-02-27"

    def test_empty(self):
        assert cdp._to_date(None) is None
        assert cdp._to_date("") is None


# ── Slot/checkbox geometry helpers ──────────────────────────────────────

class TestFindAnchorsAndTextNear:

    def _rows(self, words):
        return cdp._words_by_row(words)

    def test_finds_both_columns_on_one_row(self):
        # Regression: an earlier version stopped after the first match per
        # row, silently dropping every right-column investment slot.
        words = [
            (48.5, 113.2, 52.0, 120.0, "NAME", 0, 0, 0),
            (60.0, 113.2, 70.0, 120.0, "OF", 0, 0, 0),
            (73.0, 113.2, 100.0, 120.0, "BUSINESS", 0, 0, 0),
            (103.0, 113.2, 130.0, 120.0, "ENTITY", 0, 0, 0),
            (326.0, 113.2, 330.0, 120.0, "NAME", 0, 0, 0),
            (338.0, 113.2, 348.0, 120.0, "OF", 0, 0, 0),
            (351.0, 113.2, 378.0, 120.0, "BUSINESS", 0, 0, 0),
            (381.0, 113.2, 408.0, 120.0, "ENTITY", 0, 0, 0),
        ]
        rows = self._rows(words)
        anchors = cdp._find_anchors(rows, cdp._SLOT_ANCHOR)
        assert [round(x, 1) for x, y in anchors] == [48.5, 326.0]

    def test_text_near_joins_words_in_order_and_excludes_labels(self):
        words = [
            (48.5, 113.2, 52.0, 120.0, "NAME", 0, 0, 0),
            (50.0, 122.0, 90.0, 130.0, "Coinbase", 0, 0, 0),
            (95.0, 122.0, 130.0, 130.0, "Global", 0, 0, 0),
        ]
        rows = self._rows(words)
        text = cdp._text_near(rows, 48.5, 113.2, (0, 260), (5, 13),
                               exclude=("NAME",))
        assert text == "Coinbase Global"

    def test_text_near_returns_none_when_nothing_in_window(self):
        rows = self._rows([(48.5, 113.2, 52.0, 120.0, "NAME", 0, 0, 0)])
        assert cdp._text_near(rows, 48.5, 113.2, (0, 260), (5, 13)) is None


class TestCheckboxSelected:

    def test_detects_a_blue_mark_inside_the_checkbox(self):
        drawings = [
            {"color": (0.0, 0.0, 0.0), "rect": _Rect(47.9, 184.6, 56.3, 193.0)},
            {"color": (0.0, 0.0, 1.0), "rect": _Rect(49.9, 186.0, 54.0, 190.0)},
        ]
        assert cdp._checkbox_selected(drawings, 48.1, 173.2, -0.3, 11.0) is True

    def test_no_blue_mark_means_unselected(self):
        drawings = [
            {"color": (0.0, 0.0, 0.0), "rect": _Rect(47.9, 184.6, 56.3, 193.0)},
        ]
        assert cdp._checkbox_selected(drawings, 48.1, 173.2, -0.3, 11.0) is False

    def test_blue_mark_elsewhere_on_the_page_does_not_match(self):
        drawings = [
            {"color": (0.0, 0.0, 1.0), "rect": _Rect(400.0, 400.0, 404.0, 404.0)},
        ]
        assert cdp._checkbox_selected(drawings, 48.1, 173.2, -0.3, 11.0) is False


class TestDigitsNear:

    def test_sorts_by_x_not_by_duplicate_y(self):
        # Regression: the PDF renderer sometimes draws a digit run twice at a
        # slightly different y (a visual-weight artifact). Sorting by (y, x)
        # instead of x can interleave the duplicate with the next digit and
        # scramble the date -- found via a real Schedule D filing where
        # 04/30/24 came out as 04/24/30.
        words = [
            (52.9, 417.5, 60.0, 425.0, "04", 0, 0, 0),
            (93.9, 417.5, 100.0, 425.0, "24", 0, 0, 0),
            (73.5, 417.6, 80.0, 425.0, "30", 0, 0, 0),
        ]
        rows = cdp._words_by_row(words)
        digits = cdp._digits_near(rows, 50.4, 293.4, (0, 46), (118, 138))
        assert digits == ["04", "30", "24"]

    def test_excludes_non_digit_tokens(self):
        words = [
            (52.9, 214.3, 60.0, 222.0, "/", 0, 0, 0),
            (73.8, 214.3, 80.0, 222.0, "18", 0, 0, 0),
        ]
        rows = cdp._words_by_row(words)
        digits = cdp._digits_near(rows, 50.4, 115.4, (0, 46), (93, 112))
        assert digits == ["18"]


class TestFinalizeGeneric:

    def test_empty_input_returns_empty_frame(self):
        assert cdp._finalize_generic([], "2026-09-01T00:00:00").empty

    def test_adds_row_index_and_fetched_at(self):
        rows = [
            {"index_id": "1", "source_name": "Acme"},
            {"index_id": "1", "source_name": "Beta"},
            {"index_id": "2", "source_name": "Gamma"},
        ]
        df = cdp._finalize_generic(rows, "2026-09-01T00:00:00")
        assert df[df["index_id"] == "1"]["row_index"].tolist() == [0, 1]
        assert (df["fetched_at"] == "2026-09-01T00:00:00").all()


class _Rect:
    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1


class TestCheckboxInWindow:

    def test_true_when_fill_inside_window(self):
        drawings = [{"color": (0.0, 0.0, 1.0), "rect": _Rect(50.0, 300.0, 55.0, 305.0)}]
        assert cdp._checkbox_in_window(drawings, 40, 60, 295, 310) is True

    def test_false_when_fill_outside_window(self):
        drawings = [{"color": (0.0, 0.0, 1.0), "rect": _Rect(500.0, 500.0, 505.0, 505.0)}]
        assert cdp._checkbox_in_window(drawings, 40, 60, 295, 310) is False

    def test_ignores_non_blue_fills(self):
        drawings = [{"color": (0.0, 0.0, 0.0), "rect": _Rect(50.0, 300.0, 55.0, 305.0)}]
        assert cdp._checkbox_in_window(drawings, 40, 60, 295, 310) is False


class TestNearestCheckboxLabel:
    # Regression coverage for the real-filing finding that Schedule E's
    # checkbox marks sit ~10pt further from their option text in the right
    # column than the left -- too much slop for a fixed-offset match, but
    # nearest-distance classification against both option positions still
    # resolves correctly since the options are spaced far apart.

    CANDIDATES = [(91.2, 139.6, "Gift"), (148.5, 139.9, "Income")]

    def test_picks_nearest_when_offset_is_off_template(self):
        ax, ay = 333.0, 220.3
        drawings = [{"color": (0.0, 0.0, 1.0),
                     "rect": _Rect(ax + 73.07, ay + 143.14, ax + 77.6, ay + 147.7)}]
        label = cdp._nearest_checkbox_label(
            drawings, ax, ay, (60, 170), (135, 152), self.CANDIDATES)
        assert label == "Gift"

    def test_picks_income_when_closer(self):
        ax, ay = 46.8, 220.3
        drawings = [{"color": (0.0, 0.0, 1.0),
                     "rect": _Rect(ax + 138.0, ay + 143.0, ax + 142.5, ay + 147.5)}]
        label = cdp._nearest_checkbox_label(
            drawings, ax, ay, (60, 170), (135, 152), self.CANDIDATES)
        assert label == "Income"

    def test_none_when_no_fill_in_window(self):
        label = cdp._nearest_checkbox_label(
            [], 46.8, 220.3, (60, 170), (135, 152), self.CANDIDATES)
        assert label is None


class TestPlausibleDate:

    def test_keeps_date_within_slack(self):
        assert cdp._plausible_date("2024-09-16", 2024) == "2024-09-16"

    def test_nones_out_implausible_year(self):
        # Regression: an older Schedule E form revision (2023/2024) shifted
        # the date row enough that the digit window grabbed unrelated
        # digits, producing dates like 2002-02-16 and 2031-01-23 for a
        # 2023 filing year.
        assert cdp._plausible_date("2002-02-16", 2023) is None
        assert cdp._plausible_date("2031-01-23", 2023) is None

    def test_passes_through_missing_input(self):
        assert cdp._plausible_date(None, 2024) is None
        assert cdp._plausible_date("2024-09-16", None) == "2024-09-16"


class TestScheduleEAmountCap:
    # Regression: a source PDF with a corrupted embedded font decoded an
    # AMT: $ token into garbage digits, producing a $1.12 billion "travel
    # gift" for a real 2022 filing (Cervantes). parse_schedule_e caps
    # amount rather than trust an implausible extracted value.

    def _page_words(self, amt_text):
        # Minimal slot: NAME OF SOURCE anchor + a source name + an AMT token
        # at the offsets parse_schedule_e expects.
        ax, ay = 46.8, 220.3
        return [
            (ax, ay, ax + 20, ay + 8, "NAME", 0, 0, 0),
            (ax + 24, ay, ax + 30, ay + 8, "OF", 0, 0, 0),
            (ax + 34, ay, ax + 44, ay + 8, "SOURCE", 0, 0, 0),
            (ax, ay + 10, ax + 40, ay + 18, "Acme", 0, 0, 0),
            (ax + 178, ay + 108.6, ax + 220, ay + 116.6, amt_text, 0, 0, 0),
            (ax + 32, ay + 108.6, ax + 40, ay + 116.6, "09", 0, 0, 0),
            (ax + 50, ay + 108.6, ax + 58, ay + 116.6, "16", 0, 0, 0),
            (ax + 68, ay + 108.6, ax + 76, ay + 116.6, "22", 0, 0, 0),
        ]

    def _parse(self, amt_text, monkeypatch):
        class _FakePage:
            def get_text(self, kind=None):
                return ("SCHEDULE E Income Gifts Travel Payments"
                        if kind is None else self._words)
            def get_drawings(self):
                return []
        page = _FakePage()
        page._words = self._page_words(amt_text)

        class _FakeDoc:
            def __iter__(self):
                return iter([page])
            def close(self):
                pass
        monkeypatch.setattr(cdp.fitz, "open", lambda **kw: _FakeDoc())
        filing = {"index_id": "1", "filer_last_name": "X", "filer_first_name": "Y",
                  "filer_middle_name": None, "agency": "State Senate",
                  "position": "Senator", "filing_type": "Annual",
                  "filing_year": 2022, "filed_date": "2022-01-01",
                  "is_amendment": False}
        return cdp.parse_schedule_e(b"", filing)

    def test_caps_implausible_amount(self, monkeypatch):
        rows = self._parse("$1120000000", monkeypatch)
        assert rows[0]["amount"] is None

    def test_keeps_plausible_large_amount(self, monkeypatch):
        rows = self._parse("$836459", monkeypatch)
        assert rows[0]["amount"] == 836459.0


# ── Schedule B (Interests in Real Property) ────────────────────────────────
# parse_schedule_b's slot geometry was derived from two live 2025 filings
# (Petrie-Norris, Lowenthal) and is template-constant across the page's two
# property columns. This synthetic page pins the exact offsets: left-column
# template coords, one filled slot with FMV/nature/rental blue marks, tenant
# names in the rental-sources box (with the static "None" bubble alongside),
# and a second blank right-column slot that must be skipped.

_B_LEFT = {
    "anchor": 58.12, "ay": 120.38,
    "parcel_y": 131.48,
    "city_y": 149.74, "city_val_y": 161.60,
    "fmv_y": 183.22, "nature_y": 242.72,
    "rental_y": 301.71, "sources_y": 350.14,
    "date_x": 168.62, "date_y": 183.22, "date_digits_y": 200.34,
}


def _b_words(_B=_B_LEFT):
    a = _B["anchor"]
    words = [
        (a, _B["ay"], a + 40, _B["ay"] + 9, "ASSESSOR\u2019S", 0, 0, 0),
        (a + 47.2, _B["ay"], a + 90, _B["ay"] + 9, "PARCEL", 0, 0, 0),
        (a + 77.2, _B["ay"], a + 107, _B["ay"] + 9, "NUMBER", 0, 0, 0),
        (a + 110.3, _B["ay"], a + 120, _B["ay"] + 9, "OR", 0, 0, 0),
        (a + 123.6, _B["ay"], a + 154, _B["ay"] + 9, "STREET", 0, 0, 0),
        (a + 154.9, _B["ay"], a + 190, _B["ay"] + 9, "ADDRESS", 0, 0, 0),
        (a + 1.8, _B["parcel_y"], a + 17, _B["parcel_y"] + 13, "932", 0, 0, 0),
        (a + 21.3, _B["parcel_y"], a + 58, _B["parcel_y"] + 13, "Catalina", 0, 0, 0),
        (a + 60.8, _B["parcel_y"], a + 87, _B["parcel_y"] + 13, "Street", 0, 0, 0),
        (a, _B["city_y"], a + 16, _B["city_y"] + 9, "CITY", 0, 0, 0),
        (a + 1.8, _B["city_val_y"], a + 36, _B["city_val_y"] + 13, "Laguna", 0, 0, 0),
        (a + 38.0, _B["city_val_y"], a + 71, _B["city_val_y"] + 13, "Beach,", 0, 0, 0),
        (a + 71.9, _B["city_val_y"], a + 85, _B["city_val_y"] + 13, "CA", 0, 0, 0),
        # Acquired + disposed dates on the "IF APPLICABLE, LIST DATE:" row.
        (_B["date_x"], _B["date_y"], _B["date_x"] + 10, _B["date_y"] + 9, "IF", 0, 0, 0),
        (_B["date_x"] + 9, _B["date_y"], _B["date_x"] + 56, _B["date_y"] + 9, "APPLICABLE,", 0, 0, 0),
        (_B["date_x"] + 59, _B["date_y"], _B["date_x"] + 78, _B["date_y"] + 9, "LIST", 0, 0, 0),
        (_B["date_x"] + 77.3, _B["date_y"], _B["date_x"] + 98, _B["date_y"] + 9, "DATE:", 0, 0, 0),
        (_B["date_x"] + 16.6, _B["date_digits_y"], _B["date_x"] + 19, _B["date_digits_y"] + 12, "04", 0, 0, 0),
        (_B["date_x"] + 35.8, _B["date_digits_y"], _B["date_x"] + 38, _B["date_digits_y"] + 12, "15", 0, 0, 0),
        (_B["date_x"] + 79.6, _B["date_digits_y"], _B["date_x"] + 82, _B["date_digits_y"] + 12, "25", 0, 0, 0),
        (_B["date_x"] + 98.8, _B["date_digits_y"], _B["date_x"] + 101, _B["date_digits_y"] + 12, "06", 0, 0, 0),
        (_B["date_x"] + 118.0, _B["date_digits_y"], _B["date_x"] + 121, _B["date_digits_y"] + 12, "20", 0, 0, 0),
        (_B["date_x"] + 138.0, _B["date_digits_y"], _B["date_x"] + 141, _B["date_digits_y"] + 12, "25", 0, 0, 0),
        # Group labels.
        (a, _B["fmv_y"], a + 16, _B["fmv_y"] + 9, "FAIR", 0, 0, 0),
        (a + 19.3, _B["fmv_y"], a + 49, _B["fmv_y"] + 9, "MARKET", 0, 0, 0),
        (a + 52.5, _B["fmv_y"], a + 75.9, _B["fmv_y"] + 9, "VALUE", 0, 0, 0),
        (a, _B["nature_y"], a + 20, _B["nature_y"] + 9, "NATURE", 0, 0, 0),
        (a + 32.5, _B["nature_y"], a + 42, _B["nature_y"] + 9, "OF", 0, 0, 0),
        (a + 45.4, _B["nature_y"], a + 81, _B["nature_y"] + 9, "INTEREST", 0, 0, 0),
        (a, _B["rental_y"], a + 6, _B["rental_y"] + 9, "IF", 0, 0, 0),
        (a + 9.4, _B["rental_y"], a + 37, _B["rental_y"] + 9, "RENTAL", 0, 0, 0),
        (a + 40.5, _B["rental_y"], a + 81, _B["rental_y"] + 9, "PROPERTY,", 0, 0, 0),
        (a + 84.6, _B["rental_y"], a + 113, _B["rental_y"] + 9, "GROSS", 0, 0, 0),
        (a + 113.8, _B["rental_y"], a + 143, _B["rental_y"] + 9, "INCOME", 0, 0, 0),
        (a + 146.0, _B["rental_y"], a + 183, _B["rental_y"] + 9, "RECEIVED", 0, 0, 0),
        (a, _B["sources_y"], a + 36, _B["sources_y"] + 9, "SOURCES", 0, 0, 0),
        (a + 39.1, _B["sources_y"], a + 49, _B["sources_y"] + 9, "OF", 0, 0, 0),
        (a + 52.0, _B["sources_y"], a + 84, _B["sources_y"] + 9, "RENTAL", 0, 0, 0),
        (a + 83.0, _B["sources_y"], a + 114, _B["sources_y"] + 9, "INCOME:", 0, 0, 0),
        # Static "None" bubble + two tenant names inside the sources box.
        (a + 15.3, _B["sources_y"] + 32.2, a + 44, _B["sources_y"] + 41.8, "None", 0, 0, 0),
        (a + 2.0, _B["sources_y"] + 42.0, a + 41, _B["sources_y"] + 51.6, "Maureen", 0, 0, 0),
        (a + 44.2, _B["sources_y"] + 42.0, a + 70, _B["sources_y"] + 51.6, "Smith", 0, 0, 0),
        (a + 2.0, _B["sources_y"] + 53.5, a + 29, _B["sources_y"] + 63.1, "Lucas", 0, 0, 0),
        (a + 31.4, _B["sources_y"] + 53.5, a + 68, _B["sources_y"] + 63.1, "Stevens", 0, 0, 0),
        # Blank right-column slot (anchor label only, no value).
        (320.24, _B["ay"], 360.0, _B["ay"] + 9, "ASSESSOR\u2019S", 0, 0, 0),
        (320.24 + 47.2, _B["ay"], 360.0, _B["ay"] + 9, "PARCEL", 0, 0, 0),
        (320.24 + 77.2, _B["ay"], 360.0, _B["ay"] + 9, "NUMBER", 0, 0, 0),
        (320.24 + 110.3, _B["ay"], 360.0, _B["ay"] + 9, "OR", 0, 0, 0),
        (320.24 + 123.6, _B["ay"], 360.0, _B["ay"] + 9, "STREET", 0, 0, 0),
        (320.24 + 154.9, _B["ay"], 360.0, _B["ay"] + 9, "ADDRESS", 0, 0, 0),
    ]
    return words


def _b_drawings(bm):
    drawings = [
        # FMV: first bracket checked; NATURE: Ownership; RENTAL: $0 - $499.
        {"color": (0.0, 0.0, 1.0), "rect": _Rect(bm["anchor"] - 0.2, bm["fmv_y"] + 10.3,
                                                 bm["anchor"] + 2.5, bm["fmv_y"] + 14.6)},
        {"color": (0.0, 0.0, 1.0), "rect": _Rect(bm["anchor"] - 0.2, bm["nature_y"] + 13.4,
                                                 bm["anchor"] + 2.5, bm["nature_y"] + 17.7)},
        {"color": (0.0, 0.0, 1.0), "rect": _Rect(bm["anchor"], bm["rental_y"] + 15.2,
                                                 bm["anchor"] + 2.7, bm["rental_y"] + 19.5)},
    ]
    return drawings


class _BPage:

    def __init__(self, words, drawings, header):
        self._words = words
        self._drawings = drawings
        self._header = header

    def get_text(self, kind=None):
        return self._header if kind is None else self._words

    def get_drawings(self):
        return self._drawings


def _parse_b(words, drawings, header, monkeypatch):
    class _FakeDoc:
        def __iter__(self):
            return iter([_BPage(words, drawings, header)])
        def close(self):
            pass
    monkeypatch.setattr(cdp.fitz, "open", lambda **kw: _FakeDoc())
    filing = {"index_id": "1", "filer_last_name": "Petrie-Norris",
              "filer_first_name": "Cottie", "filer_middle_name": None,
              "agency": "State Assembly", "position": "Assembly Member",
              "filing_type": "Annual", "filing_year": 2025,
              "filed_date": "2026-03-02", "is_amendment": False}
    return cdp.parse_schedule_b(b"", filing)


class TestScheduleB:
    HEADER = ("SCHEDULE B\nInterests in Real Property\n(Including Rental Income)")

    def test_parses_full_left_slot_and_skips_blank_right_slot(self, monkeypatch):
        rows = _parse_b(_b_words(), _b_drawings(_B_LEFT), self.HEADER, monkeypatch)
        assert len(rows) == 1, "the blank right-column slot must be skipped"
        row = rows[0]
        assert row["property_address"] == "932 Catalina Street"
        assert row["city"] == "Laguna Beach, CA"
        assert row["fmv_range"] == "$2,000 - $10,000"
        assert row["fmv_min"] == 2000 and row["fmv_max"] == 10000
        assert row["nature_of_interest"] == "Ownership/Deed of Trust"
        assert row["rental_income_range"] == "$0 - $499"
        assert row["rental_sources"] == "Maureen Smith Lucas Stevens"
        assert row["acquired_date"] == "2025-04-15"
        assert row["disposed_date"] == "2025-06-20"

    def test_skips_cover_page_mention(self, monkeypatch):
        header = "Schedule B - Real Property - schedule attached"
        rows = _parse_b(_b_words(), [], header, monkeypatch)
        assert rows == []

    def test_selection_metrics_are_per_slot(self, monkeypatch):
        # The same page holds two slots; a mark in the other column's window
        # must never bleed into this slot's result.
        drawings = [{"color": (0.0, 0.0, 1.0),
                     "rect": _Rect(320.24 - 0.2, _B_LEFT["fmv_y"] + 42.1,
                                   320.24 + 2.5, _B_LEFT["fmv_y"] + 46.4)}]
        rows = _parse_b(_b_words(), drawings, self.HEADER, monkeypatch)
        assert rows[0]["fmv_range"] is None, \
            "a right-column FMV mark must not satisfy the left slot"

    def test_result_carries_filer_identity(self, monkeypatch):
        rows = _parse_b(_b_words(), _b_drawings(_B_LEFT), self.HEADER, monkeypatch)
        assert rows[0]["filer_last_name"] == "Petrie-Norris"
        assert rows[0]["filing_year"] == 2025

    def test_other_nature_captures_describe_text(self, monkeypatch):
        # "Other" is checked and a description is written inline after the
        # Other label (measured: label at rel dx~174 dy~42, describe follows
        # on the same baseline). The describe must render as "Other: <text>".
        words = _b_words()
        ax, ay = _B_LEFT["anchor"], _B_LEFT["nature_y"]
        words.append((ax + 173.9, ay + 41.7, ax + 198.0, ay + 51.0,
                      "Other", 0, 0, 0))
        words.append((ax + 202.0, ay + 41.7, ax + 234.0, ay + 51.0,
                      "Quitclaim", 0, 0, 0))
        drawings = [{"color": (0.0, 0.0, 1.0),
                     "rect": _Rect(ax + 127.7, ay + 35.6,
                                   ax + 130.7, ay + 39.9)}]
        rows = _parse_b(words, drawings, self.HEADER, monkeypatch)
        assert rows[0]["nature_of_interest"] == "Other: Quitclaim"


# ── Output contract ─────────────────────────────────────────────────────

class TestFinalize:

    def _rows(self):
        return [
            {"index_id": "1", "business_entity": "Coinbase", "acquired_date": "2024-02-10",
             "disposed_date": None},
            {"index_id": "1", "business_entity": "Etherum", "acquired_date": None,
             "disposed_date": "2024-05-05"},
            {"index_id": "2", "business_entity": "Lockbox", "acquired_date": None,
             "disposed_date": None},
        ]

    def test_empty_input_returns_empty_frame(self):
        assert cdp._finalize([], "2026-08-31T00:00:00").empty

    def test_adds_row_index_per_filing(self):
        df = cdp._finalize(self._rows(), "2026-08-31T00:00:00")
        assert df[df["index_id"] == "1"]["row_index"].tolist() == [0, 1]
        assert df[df["index_id"] == "2"]["row_index"].tolist() == [0]

    def test_date_falls_back_to_disposed_when_acquired_missing(self):
        df = cdp._finalize(self._rows(), "2026-08-31T00:00:00")
        dates = dict(zip(df["business_entity"], df["date"]))
        assert dates["Coinbase"] == "2024-02-10"
        assert dates["Etherum"] == "2024-05-05"
        assert pd.isna(dates["Lockbox"])

    def test_sets_fetched_at(self):
        df = cdp._finalize(self._rows(), "2026-08-31T00:00:00")
        assert (df["fetched_at"] == "2026-08-31T00:00:00").all()


# ── Resumable checkpoints ────────────────────────────────────────────────

class TestCheckpoint:

    def test_round_trip_preserves_rows_and_attempted_set(self, tmp_path):
        path = str(tmp_path / "senate_2024_partial.parquet")
        rows = [{"index_id": "A", "business_entity": "X"}]
        cdp._save_checkpoint(path, rows, {"A", "B"})

        back_rows, done = cdp._load_checkpoint(path)
        assert done == {"A", "B"}, \
            "a filing that parsed to zero A-1 rows must not be re-fetched"
        assert len(back_rows) == 1

    def test_missing_checkpoint_is_a_clean_start(self, tmp_path):
        rows, done = cdp._load_checkpoint(str(tmp_path / "nope.parquet"))
        assert rows == [] and done == set()

    def test_clear_removes_both_files(self, tmp_path):
        path = str(tmp_path / "assembly_2024_partial.parquet")
        cdp._save_checkpoint(path, [{"index_id": "A"}], {"A"})
        cdp._clear_checkpoint(path)
        assert not os.path.exists(path)
        assert not os.path.exists(cdp._done_path(path))
