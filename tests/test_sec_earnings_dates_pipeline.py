"""sec_earnings_dates_pipeline: which filings count as earnings releases, how
acceptance time maps to the trading session, and the failure paths. Block
shapes follow the live EDGAR submissions JSON (column-oriented lists)."""

import pandas as pd
import pytest

import sec_earnings_dates_pipeline as se


def _block(rows):
    keys = ["accessionNumber", "filingDate", "acceptanceDateTime", "form", "items"]
    return {k: [r[i] for r in rows] for i, k in enumerate(keys)}


BLOCK = _block([
    ("a1", "2026-11-02", "2026-11-02T21:30:23.000Z", "8-K", "2.02,9.01"),   # earnings
    ("a2", "2026-10-01", "2026-10-01T20:00:00.000Z", "8-K", "7.01,9.01"),   # Reg FD, not earnings
    ("a3", "2026-11-03", "2026-11-03T21:00:00.000Z", "8-K/A", "2.02"),      # amendment
    ("a4", "2026-08-01", "2026-08-01T12:00:00.000Z", "10-Q", ""),
    ("a5", "2026-07-30", "2026-07-30T20:30:28.000Z", "8-K", "5.02, 2.02"),  # spaced list
])


def test_only_original_8ks_with_item_2_02():
    assert se.earnings_filings(BLOCK)["accessionNumber"].tolist() == ["a1", "a5"]


def test_block_missing_columns_is_empty():
    assert se.earnings_filings({}).empty
    assert se.earnings_filings({"form": ["8-K"]}).empty


@pytest.mark.parametrize("utc, expected", [
    ("2026-07-14T13:29:59Z", "pre"),       # 09:29:59 EDT
    ("2026-07-14T13:30:00Z", "intraday"),  # 09:30 EDT
    ("2026-07-14T19:59:59Z", "intraday"),  # 15:59:59 EDT
    ("2026-07-14T20:00:00Z", "post"),      # 16:00 EDT
    ("2026-11-02T21:30:23Z", "post"),      # 16:30 EST -- after the DST change
    ("2026-11-02T14:00:00Z", "pre"),       # 09:00 EST; would be 10:00 if DST were ignored
])
def test_session_uses_new_york_time_including_dst(utc, expected):
    s = pd.to_datetime(pd.Series([utc]), utc=True)
    assert se.classify_session(s).iloc[0] == expected


def test_each_ticker_of_a_cik_gets_a_row():
    raw = se.earnings_filings(BLOCK).assign(cik=1652044)
    universe = pd.DataFrame({"symbol": ["GOOG", "GOOGL", "AAPL"],
                             "cik": [1652044, 1652044, 320193]})
    out = se.shape(raw, universe, "2026-11-21T00:00:00")
    assert sorted(out["symbol"].unique()) == ["GOOG", "GOOGL"]
    assert len(out) == 4
    row = out[(out.symbol == "GOOG") & (out.accession_number == "a1")].iloc[0]
    assert (row.filing_date, row.accepted_et, row.session) == \
        ("2026-11-02", "2026-11-02 16:30:23", "post")


def test_backfill_reads_predecessor_ciks_under_the_current_cik(monkeypatch):
    old = _block([("old1", "2012-05-08", "2012-05-08T20:15:00.000Z", "8-K", "2.02,9.01")])
    new = _block([("new1", "2026-11-17", "2026-11-17T21:05:00.000Z", "8-K", "2.02,9.01")])
    pages = {"CIK0001744489.json": {"filings": {"recent": new, "files": []}},
             "CIK0001001039.json": {"filings": {"recent": old, "files": []}}}
    asked = []
    monkeypatch.setattr(se, "_get_json", lambda session, name: asked.append(name) or pages[name])
    raw = se.company_filings(None, 1744489, full_history=True)
    out = se.shape(raw, pd.DataFrame({"symbol": ["DIS"], "cik": [1744489]}), "2026-11-21T00:00:00")
    assert out.set_index("accession_number")["filer_cik"].to_dict() == {"old1": 1001039, "new1": 1744489}
    assert (out["symbol"] == "DIS").all() and (out["cik"] == 1744489).all()
    asked.clear()
    se.company_filings(None, 1744489, full_history=False)   # incremental: current filer only
    assert asked == ["CIK0001744489.json"]


def _fake_company(fail_ciks=(), rows=True):
    def fake(session, cik, full_history):
        if cik in fail_ciks:
            raise RuntimeError("HTTP 500")
        return se.earnings_filings(BLOCK if rows else {}).assign(cik=cik)
    return fake


def test_too_many_failed_companies_fails_the_run(monkeypatch, tmp_path):
    monkeypatch.setattr(se, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(se, "company_filings", _fake_company(fail_ciks={2, 3}))
    universe = pd.DataFrame({"symbol": list("ABCDEFGHIJ"), "cik": range(1, 11)})
    assert se.run(universe, list(range(1, 11)), True, "backfill", "2026-11-21T00:00:00", None) == 1
    monkeypatch.setattr(se, "company_filings", _fake_company(fail_ciks={2}))
    assert se.run(universe, list(range(1, 11)), True, "backfill", "2026-11-21T00:00:00", None) == 0


def test_backfill_with_no_rows_fails_but_a_quiet_incremental_week_does_not(monkeypatch, tmp_path):
    monkeypatch.setattr(se, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(se, "company_filings", _fake_company(rows=False))
    universe = pd.DataFrame({"symbol": ["A"], "cik": [1]})
    assert se.run(universe, [1], True, "backfill", "2026-11-21T00:00:00", None) == 1
    assert se.run(universe, [1], False, "incremental", "2026-11-21T00:00:00", None) == 0


def test_two_runs_on_one_day_do_not_overwrite_each_other(monkeypatch, tmp_path):
    monkeypatch.setattr(se, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(se, "company_filings", _fake_company())
    universe = pd.DataFrame({"symbol": ["A", "B"], "cik": [1, 2]})
    se.run(universe, [1], True, "backfill", "2026-11-21T09:00:00", None)
    se.run(universe, [2], True, "backfill", "2026-11-21T15:30:00", None)
    written = pd.concat(pd.read_parquet(p) for p in tmp_path.rglob("*.parquet"))
    assert sorted(written["symbol"].unique()) == ["A", "B"]


def test_backfill_writes_as_it_goes(monkeypatch, tmp_path):
    """A backfill killed part-way must keep the companies it already fetched."""
    monkeypatch.setattr(se, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(se, "WRITE_EVERY", 2)
    calls = []

    def fake(session, cik, full_history):
        calls.append(cik)
        if cik == 5:
            raise KeyboardInterrupt
        return se.earnings_filings(BLOCK).assign(cik=cik)

    monkeypatch.setattr(se, "company_filings", fake)
    universe = pd.DataFrame({"symbol": list("ABCDE"), "cik": [1, 2, 3, 4, 5]})
    with pytest.raises(KeyboardInterrupt):
        se.run(universe, [1, 2, 3, 4, 5], True, "backfill", "2026-11-21T00:00:00", None)
    written = pd.concat(pd.read_parquet(p) for p in tmp_path.rglob("*.parquet"))
    assert sorted(written["symbol"].unique()) == ["A", "B", "C", "D"]
