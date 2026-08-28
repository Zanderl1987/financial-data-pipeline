"""
Offline tests for congressional_trades_pipeline.

No network: the House index is built as an in-memory ZIP and the Senate detail
view as an HTML string, both shaped like the real responses that were captured
live on 2026-08-28.
"""

import io
import zipfile

import pandas as pd
import pytest

import congressional_trades_pipeline as ctp


# ── Amount brackets ────────────────────────────────────────────────────────

class TestParseAmountRange:

    def test_standard_bracket(self):
        raw, lo, hi = ctp.parse_amount_range("$1,001 - $15,000")
        assert (raw, lo, hi) == ("$1,001 - $15,000", 1001.0, 15000.0)

    def test_wrapped_bracket_is_whitespace_normalized(self):
        # The House PDF wraps the upper bound onto the next line.
        raw, lo, hi = ctp.parse_amount_range("$15,001 -   \n $50,000")
        assert raw == "$15,001 - $50,000"
        assert (lo, hi) == (15001.0, 50000.0)

    def test_open_ended_top_bracket_has_no_ceiling(self):
        raw, lo, hi = ctp.parse_amount_range("Over $50,000,000")
        assert lo == 50_000_000.0
        assert hi is None

    def test_no_figure_keeps_raw_text(self):
        assert ctp.parse_amount_range("--") == ("--", None, None)

    def test_empty(self):
        assert ctp.parse_amount_range(None) == (None, None, None)
        assert ctp.parse_amount_range("") == (None, None, None)


# ── Output contract ────────────────────────────────────────────────────────

class TestFinalize:

    def _rows(self):
        return [
            {"doc_id": "1", "member_name": "Alice A", "transaction_date": "2026-01-02"},
            {"doc_id": "1", "member_name": "Alice A", "transaction_date": "2026-01-02"},
            {"doc_id": "2", "member_name": "Bob B", "transaction_date": "2026-02-03"},
        ]

    def test_adds_required_columns(self):
        # transaction_type comes from the per-chamber parsers; _finalize is
        # responsible for chamber, fetched_at, row_index and date.
        df = ctp._finalize(self._rows(), "house", "2026-08-28T00:00:00")
        for col in ("chamber", "fetched_at", "row_index", "date"):
            assert col in df.columns, f"missing required column {col}"
        assert (df["chamber"] == "house").all()
        assert (df["fetched_at"] == "2026-08-28T00:00:00").all()
        assert (df["date"] == df["transaction_date"]).all()

    def test_row_index_is_per_filing_and_makes_the_key_unique(self):
        # Two identical transactions in one filing is legal and does happen;
        # curated.py dedups on (chamber, doc_id, row_index), so row_index is
        # what keeps the second one from being silently dropped.
        df = ctp._finalize(self._rows(), "house", "ts")
        assert list(df["row_index"]) == [0, 1, 0]
        assert not df.duplicated(
            subset=["chamber", "doc_id", "row_index"]).any()

    def test_drops_rows_without_a_member(self):
        rows = self._rows() + [{"doc_id": "3", "member_name": None,
                                "transaction_date": "2026-03-04"}]
        assert len(ctp._finalize(rows, "house", "ts")) == 3

    def test_empty_input_returns_empty_frame(self):
        assert ctp._finalize([], "house", "ts").empty

    def test_no_column_named_year_or_month(self):
        # Hive partitioning exposes year/month as virtual columns on read-back
        # and would silently overwrite same-named data columns.
        df = ctp._finalize(self._rows(), "senate", "ts")
        assert "year" not in df.columns
        assert "month" not in df.columns


# ── House index ────────────────────────────────────────────────────────────

HOUSE_XML = """<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
  <Member>
    <Last>Alford</Last><First>Mark</First>
    <FilingType>P</FilingType><StateDst>MO04</StateDst>
    <Year>2026</Year><FilingDate>3/31/2026</FilingDate><DocID>20034201</DocID>
  </Member>
  <Member>
    <Last>Ignored</Last><First>Annual</First>
    <FilingType>C</FilingType><StateDst>TX01</StateDst>
    <Year>2026</Year><FilingDate>5/15/2026</FilingDate><DocID>10078673</DocID>
  </Member>
  <Member>
    <Last>NoDoc</Last><First>Missing</First>
    <FilingType>P</FilingType><StateDst>CA12</StateDst>
    <Year>2026</Year><FilingDate>5/15/2026</FilingDate><DocID></DocID>
  </Member>
</FinancialDisclosure>
"""


class _FakeResponse:
    def __init__(self, content):
        self.content = content
        self.status_code = 200


class TestFetchHouseIndex:

    def _session(self, monkeypatch, content):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("2026FD.xml", content)
        monkeypatch.setattr(ctp, "_get",
                            lambda *a, **k: _FakeResponse(buf.getvalue()))

    def test_keeps_only_periodic_transaction_reports(self, monkeypatch):
        self._session(monkeypatch, HOUSE_XML)
        filings = ctp.fetch_house_index(None, 2026)
        # The annual report (type C) and the DocID-less row are both dropped.
        assert [f["doc_id"] for f in filings] == ["20034201"]

    def test_splits_state_and_district(self, monkeypatch):
        self._session(monkeypatch, HOUSE_XML)
        filing = ctp.fetch_house_index(None, 2026)[0]
        assert filing["member_name"] == "Mark Alford"
        assert filing["state"] == "MO"
        assert filing["district"] == "04"
        assert filing["disclosure_date"] == "2026-03-31"

    def test_missing_index_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ctp, "_get", lambda *a, **k: None)
        assert ctp.fetch_house_index(None, 1999) == []

    def test_non_zip_payload_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ctp, "_get",
                            lambda *a, **k: _FakeResponse(b"<html>404</html>"))
        assert ctp.fetch_house_index(None, 2026) == []


# ── House ticker extraction ────────────────────────────────────────────────

class TestHouseTickerPatterns:

    @pytest.mark.parametrize("desc,ticker,kind", [
        ("Apple Inc. - Common Stock (AAPL) [ST]", "AAPL", "ST"),
        ("Berkshire Hathaway Inc. New Common Stock (BRK.B) [ST]", "BRK.B", "ST"),
        ("AT&T Inc. (T) [ST]", "T", "ST"),
    ])
    def test_parenthesized_form(self, desc, ticker, kind):
        m = ctp._HOUSE_TICKER_RE.search(desc)
        assert m and m.group(1) == ticker and m.group(2) == kind

    def test_exchange_qualified_fallback(self):
        desc = ("DIA - State Street SPDR Dow Jones Indust Avg ETF Trust "
                "NYSEARCA: DIA [OT]")
        assert ctp._HOUSE_TICKER_RE.search(desc) is None
        m = ctp._HOUSE_EXCHANGE_RE.search(desc)
        assert m and m.group(1) == "DIA" and m.group(2) == "OT"

    def test_asset_with_no_ticker_matches_nothing(self):
        desc = "Invesco QQQ [OT]"
        assert ctp._HOUSE_TICKER_RE.search(desc) is None
        assert ctp._HOUSE_EXCHANGE_RE.search(desc) is None


class TestHouseColumnAssignment:
    """x0 -> column mapping, using the real geometry measured from a live PDF."""

    COLS = {"owner": 65.07, "asset": 104.07, "type": 262.32,
            "date": 326.82, "notif": 381.57, "amount": 446.07}

    @pytest.mark.parametrize("x,expected", [
        (25.3, "id"),
        (65.7, "owner"),
        (104.7, "asset"),
        (262.2, "type"),
        (326.7, "date"),
        (381.4, "notif"),
        (445.9, "amount"),
        (524.8, "amount"),   # the Cap. Gains column shares the rightmost bucket
    ])
    def test_word_lands_in_the_right_column(self, x, expected):
        assert ctp._assign_column(x, self.COLS) == expected


# ── Senate detail view ─────────────────────────────────────────────────────

SENATE_HTML = """
<html><body>
<table class="table">
 <thead><tr class="header">
   <th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th>
   <th>Asset Name</th><th>Asset Type</th><th>Type</th><th>Amount</th>
   <th>Comment</th>
 </tr></thead>
 <tbody>
  <tr>
    <td>1</td><td>08/07/2026</td><td>Spouse</td><td>--</td>
    <td>W.L. Gore &amp; Associates, Inc.
        <div class="text-muted"><em>Company:</em> W.L. Gore &amp; Associates, Inc.</div>
        <div class="text-muted"><em>Description:</em>&nbsp;Advanced materials</div>
    </td>
    <td>Non-Public Stock</td><td>Sale (Partial)</td>
    <td>$100,001 - $250,000</td><td>--</td>
  </tr>
  <tr>
    <td>2</td><td>07/23/2026</td><td>Joint</td><td>MSFT</td>
    <td>Microsoft Corp</td><td>Stock</td><td>Purchase</td>
    <td>$1,001 - $15,000</td><td>see note</td>
  </tr>
 </tbody>
</table>
</body></html>
"""

FILING = {"url": "https://efdsearch.senate.gov/search/view/ptr/abc-123/",
          "member_name": "Jane Doe",
          "disclosure_date": "2026-08-28"}


class TestParseSenatePtr:

    @pytest.fixture()
    def rows(self):
        return ctp.parse_senate_ptr(SENATE_HTML, FILING)

    def test_extracts_every_transaction_row(self, rows):
        assert len(rows) == 2

    def test_em_dash_placeholder_becomes_null(self, rows):
        assert rows[0]["ticker"] is None      # source shows "--"
        assert rows[0]["comment"] is None
        assert rows[1]["ticker"] == "MSFT"
        assert rows[1]["comment"] == "see note"

    def test_asset_name_strips_nested_company_and_description(self, rows):
        assert rows[0]["asset_description"] == "W.L. Gore & Associates, Inc."

    def test_dates_and_amounts(self, rows):
        assert rows[0]["transaction_date"] == "2026-08-07"
        assert rows[0]["amount_min"] == 100001.0
        assert rows[0]["amount_max"] == 250000.0

    def test_disclosure_date_comes_from_the_search_index(self, rows):
        # The detail view has no submission date of its own; it is carried in
        # from the search result. transaction_date must never stand in for it.
        assert all(r["disclosure_date"] == "2026-08-28" for r in rows)
        assert rows[0]["transaction_date"] != rows[0]["disclosure_date"]

    def test_doc_id_is_the_filing_uuid(self, rows):
        assert all(r["doc_id"] == "abc-123" for r in rows)

    def test_page_without_a_transaction_table_yields_nothing(self):
        html = "<html><body><table><thead><tr><th>Other</th></tr></thead>" \
               "<tbody><tr><td>x</td></tr></tbody></table></body></html>"
        assert ctp.parse_senate_ptr(html, FILING) == []


# ── Checkpoint resume ──────────────────────────────────────────────────────

class TestCheckpoint:

    def test_round_trip_preserves_rows_and_attempted_set(self, tmp_path):
        path = str(tmp_path / "house_2020_partial.parquet")
        rows = [{"doc_id": "A", "member_name": "X", "transaction_date": None}]
        # "B" was attempted but parsed to zero rows (an image-only scan).
        ctp._save_checkpoint(path, rows, {"A", "B"})

        back_rows, done = ctp._load_checkpoint(path)
        assert done == {"A", "B"}, \
            "an attempted-but-empty filing must not be re-fetched on resume"
        assert len(back_rows) == 1

    def test_missing_checkpoint_is_a_clean_start(self, tmp_path):
        rows, done = ctp._load_checkpoint(str(tmp_path / "nope.parquet"))
        assert rows == [] and done == set()

    def test_clear_removes_both_files(self, tmp_path):
        path = str(tmp_path / "senate_2019_partial.parquet")
        ctp._save_checkpoint(path, [{"doc_id": "A"}], {"A"})
        ctp._clear_checkpoint(path)
        import os
        assert not os.path.exists(path)
        assert not os.path.exists(ctp._done_path(path))
