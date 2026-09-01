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
