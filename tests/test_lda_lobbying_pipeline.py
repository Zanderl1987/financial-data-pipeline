"""
Tests for lda_lobbying_pipeline — Senate LDA lobbying filings.

All HTTP is mocked. The key behavior: a normal run fetches only filings posted
since the newest one already stored, instead of re-paging the whole year
(which outgrew run_all's 30-min timeout and failed every night).
"""

import datetime
import os
import sys
import urllib.parse

import pandas as pd
import pytest
import responses

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import lda_lobbying_pipeline as lda


def _filing(uuid, posted, year=2026):
    return {
        "filing_uuid": uuid, "filing_type": "Q3", "filing_year": year,
        "filing_period": "third_quarter", "income": "10000.00", "expenses": None,
        "dt_posted": posted, "termination_date": None,
        "registrant": {"id": 1, "name": "Acme Lobbying"},
        "client": {"id": 2, "name": "Widget Co", "general_description": "widgets"},
        "lobbyists": [{}], "gov_entities": [], "specific_issues": [],
    }


def _store(tmp_path, posted_values):
    d = tmp_path / lda.BASE_DIR / "year=2026" / "month=08"
    d.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"filing_uuid": [f"u{i}" for i in range(len(posted_values))],
                       "dt_posted": posted_values})
    df.to_parquet(d / f"stored_{len(list(d.iterdir()))}.parquet", index=False)


def _query(call):
    return urllib.parse.parse_qs(urllib.parse.urlparse(call.request.url).query)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(lda.time, "sleep", lambda s: None)


@pytest.fixture
def run_main(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def _run(*argv):
        monkeypatch.setattr(sys, "argv", ["lda_lobbying_pipeline.py", *argv])
        lda.main()
    return _run


class TestLatestPosted:
    def test_none_when_nothing_stored(self, tmp_path):
        assert lda.latest_posted(str(tmp_path / "missing")) is None

    def test_newest_across_files_ignoring_nulls(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _store(tmp_path, ["2026-08-01T10:00:00-04:00", None])
        _store(tmp_path, ["2026-08-26T14:03:48-04:00", "2026-08-20T09:00:00-04:00"])
        assert lda.latest_posted() == datetime.date(2026, 8, 26)


class TestIncremental:
    @responses.activate
    def test_fetches_only_filings_posted_since_newest_stored(self, tmp_path, run_main):
        _store(tmp_path, ["2026-08-26T14:03:48-04:00"])
        # real `next` links point at the lda.gov mirror
        page2 = "https://lda.gov/api/v1/filings/?filing_dt_posted_after=2026-08-24&page=2"
        responses.add(responses.GET, lda.API_URL, json={
            "count": 3, "next": page2,
            "results": [_filing("a", "2026-09-01T10:00:00-04:00"),
                        _filing("b", "2026-09-02T10:00:00-04:00", year=2025)],
        })
        responses.add(responses.GET, page2, json={
            "count": 3, "next": None,
            "results": [_filing("c", "2026-09-03T10:00:00-04:00")],
        })
        run_main()

        first = _query(responses.calls[0])
        assert first["filing_dt_posted_after"] == ["2026-08-24"]  # newest minus 2 days
        assert "filing_year" not in first  # late filings for past years count too
        new = [p for p in (tmp_path / lda.BASE_DIR).rglob("*since*.parquet")]
        assert len(new) == 1
        df = pd.read_parquet(new[0])
        assert sorted(df["filing_uuid"]) == ["a", "b", "c"]
        assert df["fetched_at"].notna().all()

    @responses.activate
    def test_no_new_filings_writes_nothing(self, tmp_path, run_main):
        _store(tmp_path, ["2026-09-28T10:00:00-04:00"])
        responses.add(responses.GET, lda.API_URL, json={"count": 0, "next": None, "results": []})
        run_main()
        assert not list((tmp_path / lda.BASE_DIR).rglob("*since*.parquet"))

    @responses.activate
    def test_fetch_failure_raises_instead_of_passing(self, tmp_path, run_main):
        _store(tmp_path, ["2026-09-28T10:00:00-04:00"])
        responses.add(responses.GET, lda.API_URL, status=500, body="down")
        with pytest.raises(RuntimeError):
            run_main()


class TestWholeYearModes:
    @responses.activate
    def test_nothing_stored_falls_back_to_current_year(self, tmp_path, run_main):
        responses.add(responses.GET, lda.API_URL, json={
            "count": 1, "next": None,
            "results": [_filing("a", "2026-09-01T10:00:00-04:00")],
        })
        run_main()
        q = _query(responses.calls[0])
        assert q["filing_year"] == [str(datetime.datetime.utcnow().year)]
        assert "filing_dt_posted_after" not in q

    @responses.activate
    def test_backfill_ignores_stored_data(self, tmp_path, run_main):
        _store(tmp_path, ["2026-09-28T10:00:00-04:00"])
        responses.add(responses.GET, lda.API_URL, json={"count": 0, "next": None, "results": []})
        run_main("--start-year", str(datetime.datetime.utcnow().year))
        assert "filing_year" in _query(responses.calls[0])
