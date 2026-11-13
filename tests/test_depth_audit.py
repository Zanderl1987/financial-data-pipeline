"""Tests for depth_audit.py -- depth, not presence."""

import datetime
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import depth_audit as d
import query as q


def _write(root, table, df):
    folder = root / table
    folder.mkdir(parents=True, exist_ok=True)
    df.to_parquet(folder / f"{table}.parquet", index=False)


def _panel(symbols, start, end):
    days = pd.bdate_range(start, end).strftime("%Y-%m-%d")
    return pd.DataFrame([(s, day, 1.0) for s in symbols for day in days],
                        columns=["symbol", "date", "close"])


TODAY = datetime.date(2026, 10, 26)


class TestMeasure:
    def test_counts_entities_span_and_dates(self, tmp_path):
        _write(tmp_path, "t", _panel(["A", "B", "C"], "2020-01-01", "2020-12-31"))
        row = d.measure("t", str(tmp_path), today=TODAY)
        assert row["rows"] == 3 * len(pd.bdate_range("2020-01-01", "2020-12-31"))
        assert (row["entity_col"], row["entities"]) == ("symbol", 3)
        assert row["first"] == datetime.date(2020, 1, 1)
        assert row["last"] == datetime.date(2020, 12, 31)
        assert row["dates"] == len(pd.bdate_range("2020-01-01", "2020-12-31"))
        assert row["future"] == 0

    def test_symbol_beats_country_as_the_entity(self, tmp_path):
        df = _panel(["A", "B"], "2020-01-01", "2020-01-10").assign(country="US")
        _write(tmp_path, "t", df)
        assert d.measure("t", str(tmp_path))["entity_col"] == "symbol"

    def test_counts_rows_dated_after_today(self, tmp_path):
        df = pd.DataFrame({"symbol": ["A", "A", "A"],
                           "date": ["2026-01-02", "2026-10-26", "2215-09-30"]})
        _write(tmp_path, "t", df)
        assert d.measure("t", str(tmp_path), today=TODAY)["future"] == 1

    def test_missing_snapshot_measures_nothing(self, tmp_path):
        assert d.measure("absent", str(tmp_path))["rows"] is None


class TestJudge:
    floor = {"min_entities": 100, "first_date_by": "2010-01-01", "why": "need a universe"}

    def row(self, **kw):
        base = {"rows": 10, "entity_col": "symbol", "entities": 500,
                "first": datetime.date(2005, 1, 3), "last": TODAY, "future": 0}
        base.update(kw)
        return base

    def test_options_history_shape_is_thin(self):
        # The motivating case: 4 symbols from 2023-12 read as coverage.
        verdict, note = d.judge(self.row(entities=4, first=datetime.date(2023, 12, 11)),
                                self.floor, TODAY)
        assert verdict == "THIN"
        assert "4 symbol < 100" in note and "starts 2023-12-11" in note
        assert "need a universe" in note

    def test_shallow_when_only_history_is_short(self):
        verdict, _ = d.judge(self.row(first=datetime.date(2015, 1, 2)), self.floor, TODAY)
        assert verdict == "SHALLOW"

    def test_ok_when_floor_met(self):
        assert d.judge(self.row(), self.floor, TODAY) == ("OK", "")

    def test_stale(self):
        floor = {"max_stale_days": 10, "why": "daily task"}
        verdict, note = d.judge(self.row(last=datetime.date(2026, 9, 11)), floor, TODAY)
        assert verdict == "STALE" and "45 days old" in note

    def test_no_entity_column_cannot_meet_an_entity_floor(self):
        verdict, note = d.judge(self.row(entity_col=None, entities=None), self.floor, TODAY)
        assert verdict == "THIN" and "no entity column" in note

    def test_unparseable_dates_cannot_meet_a_history_floor(self):
        verdict, _ = d.judge(self.row(first=None), {"first_date_by": "2010-01-01", "why": ""}, TODAY)
        assert verdict == "SHALLOW"

    def test_undeclared_and_empty_and_missing(self):
        assert d.judge(self.row(), None, TODAY)[0] == "UNDECLARED"
        assert d.judge(self.row(rows=0), self.floor, TODAY)[0] == "EMPTY"
        assert d.judge({"rows": None}, self.floor, TODAY) == ("NO SNAPSHOT", "")

    def test_no_snapshot_with_raw_files_says_so(self):
        verdict, note = d.judge({"rows": None, "raw_files": 7}, None, TODAY)
        assert verdict == "NO SNAPSHOT" and "7 raw files" in note

    def test_future_rows_are_noted_without_changing_the_verdict(self):
        verdict, note = d.judge(self.row(future=28, last=datetime.date(2215, 9, 30)), None, TODAY)
        assert verdict == "UNDECLARED" and "28 rows dated after today" in note


class TestAuditAndCli:
    def test_audit_orders_failures_first(self, tmp_path):
        _write(tmp_path, "deep", _panel(list("ABCDE"), "2000-01-03", "2000-01-14"))
        _write(tmp_path, "thin", _panel(["A"], "2000-01-03", "2000-01-14"))
        floors = {"deep": {"min_entities": 5, "why": ""}, "thin": {"min_entities": 5, "why": ""}}
        report = d.audit(["deep", "thin", "absent"], str(tmp_path), floors)
        assert report["table"].tolist() == ["absent", "thin", "deep"]
        assert report["verdict"].tolist() == ["NO SNAPSHOT", "THIN", "OK"]

    def test_raw_files_resolve_beside_the_audited_root(self, tmp_path, monkeypatch):
        raw = tmp_path / "storage" / "raw" / "src"
        raw.mkdir(parents=True)
        for i in range(3):
            (raw / f"part{i}.parquet").write_bytes(b"")
        monkeypatch.setitem(q.CATALOG, "rawonly", q._glob("src/**/*.parquet"))
        assert d._raw_files("rawonly", str(tmp_path / "storage" / "curated")) == 3

    def test_fail_on_thin_exit_code(self, tmp_path, monkeypatch, capsys):
        _write(tmp_path, "agriculture", _panel(["corn"], "2015-01-02", "2015-01-30"))
        monkeypatch.setattr(d, "DEPTH_FLOORS", {"agriculture": d.DEPTH_FLOORS["agriculture"]})
        args = ["--root", str(tmp_path), "--table", "agriculture"]
        assert d.main(args) == 0
        assert d.main(args + ["--fail-on-thin"]) == 1
        assert "1 DECLARED FLOOR(S) FAILED" in capsys.readouterr().out

    def test_known_gap_is_reported_but_does_not_fail(self, tmp_path, monkeypatch, capsys):
        _write(tmp_path, "options_history", _panel(["SPY"], "2023-12-11", "2023-12-29"))
        monkeypatch.setattr(d, "DEPTH_FLOORS", {"options_history": d.DEPTH_FLOORS["options_history"]})
        args = ["--root", str(tmp_path), "--table", "options_history", "--fail-on-thin"]
        assert d.main(args) == 0
        out = capsys.readouterr().out
        assert "options_history [THIN]: (known gap:" in out
        assert "0 DECLARED FLOOR(S) FAILED (1 known gap(s) not counted)" in out


class TestFloors:
    def test_every_floor_names_a_catalog_table_and_a_reason(self):
        for table, floor in d.DEPTH_FLOORS.items():
            assert table in q.CATALOG, table
            assert floor.get("why"), table
            assert set(floor) <= {"min_entities", "first_date_by", "min_rows",
                                  "max_stale_days", "known_gap", "why"}, table

    def test_options_history_keeps_its_floor(self):
        assert d.DEPTH_FLOORS["options_history"]["min_entities"] >= 100
