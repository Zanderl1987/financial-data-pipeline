"""usda_beef_pipeline: parsing of the LMR DataMart JSON, and the failure paths
that must not read as a PASS. Row shapes are copied from live responses
(2026-11-21): numbers arrive as strings with thousands separators, grade
labels can carry a leading space, primal names are prefixed "Primal "."""

import datetime

import pandas as pd
import pytest

import usda_beef_pipeline as ub


CUTOUT = [{"report_date": "11/20/2026", "choice_600_900_current": "352.84",
           "select_600_900_current": "342.92", "published_date": "11/20/2026 14:42:56"},
          {"report_date": "11/19/2026", "choice_600_900_current": "353.52",
           "select_600_900_current": "341.97", "published_date": "11/19/2026 14:43:54"}]
VOLUME = [{"report_date": "11/20/2026", "choice_volume_loads": "95.34",
           "select_volume_loads": "7.37", "trimmings_volume_loads": "9.62",
           "coarse_volume_loads": "9.07", "choice_volume_pounds": "3,813,500"}]
CATTLE = [{"report_date": "11/16/2026", "class_description": "STEER",
           "selling_basis_description": "LIVE FOB", "grade_description": " 0 - 35% Choice",
           "head_count": "12,571", "weight_range_avg": "1,633", "price_range_low": "210.00",
           "price_range_high": "218.00", "weighted_avg_price": "214.27",
           "published_date": "11/16/2026 11:55:26"}]


def test_cutout_values_and_volume_join_on_date():
    df = ub.parse_cutout(CUTOUT, VOLUME).set_index("date")
    assert df.loc["2026-11-20", "choice_600_900"] == pytest.approx(352.84)
    assert df.loc["2026-11-20", "select_600_900"] == pytest.approx(342.92)
    assert df.loc["2026-11-20", "choice_volume_loads"] == pytest.approx(95.34)
    assert df.loc["2026-11-20", "published_at"] == "2026-11-20T14:42:56"
    # a day with prices but no volume row keeps its prices
    assert df.loc["2026-11-19", "choice_600_900"] == pytest.approx(353.52)
    assert pd.isna(df.loc["2026-11-19", "choice_volume_loads"])


def test_cattle_numbers_with_commas_and_padded_grade():
    row = ub.parse_cattle(CATTLE).iloc[0]
    assert row["head_count"] == 12571
    assert row["weight_avg"] == 1633
    assert row["weighted_avg_price"] == pytest.approx(214.27)
    assert row["grade"] == "0 - 35% Choice"
    assert (row["class"], row["selling_basis"]) == ("steer", "live fob")
    assert row["report_date"] == "2026-11-16"


def test_primal_names_are_normalised():
    rows = [{"report_date": "11/20/2026", "primal_desc": "Primal Rib",
             "choice_600_900": "679.88", "select_600_900": "633.89",
             "published_date": "11/20/2026 14:42:56"}]
    assert ub.parse_primals(rows)["primal"].tolist() == ["rib"]


def test_rows_without_numbers_are_dropped():
    # live 2002 rows: placeholders with every value null; live cattle rows for a
    # category where nothing traded that week
    blank_cutout = [{"report_date": "06/04/2002", "choice_600_900_current": None,
                     "select_600_900_current": None, "published_date": "05/27/2004 14:58:16"}]
    assert ub.parse_cutout(blank_cutout + CUTOUT, []).shape[0] == 2
    blank_primal = [{"report_date": "06/04/2002", "primal_desc": "Primal Rib",
                     "choice_600_900": None, "select_600_900": None,
                     "published_date": "05/27/2004 14:58:16"}]
    assert ub.parse_primals(blank_primal).empty
    no_trades = [dict(CATTLE[0], grade_description="35 - 65% Choice", head_count=None,
                      weighted_avg_price=None, price_range_low=None, price_range_high=None)]
    assert ub.parse_cattle(no_trades + CATTLE)["grade"].tolist() == ["0 - 35% Choice"]
    # a Select-only day is real data and stays
    select_only = [dict(CUTOUT[0], choice_600_900_current=None)]
    assert len(ub.parse_cutout(select_only, [])) == 1


def test_empty_responses_parse_to_empty_frames():
    assert ub.parse_cutout([], []).empty
    assert ub.parse_primals([]).empty
    assert ub.parse_cattle([]).empty


def test_truncated_response_raises(monkeypatch):
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"stats": {"totalRows:": 120000, "returnedRows:": 99999},
                                "results": [{}] * 3}
    monkeypatch.setattr(ub.requests, "get", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match="truncated"):
        ub._fetch(2453, "Detail", datetime.date(2020, 1, 1), datetime.date(2020, 12, 31))


def test_a_table_with_no_rows_fails_the_run(monkeypatch, tmp_path):
    monkeypatch.setattr(ub, "RAW_ROOT", str(tmp_path))
    monkeypatch.setattr(ub, "REQUEST_GAP", 0)
    monkeypatch.setattr(ub, "_fetch", lambda slug, section, a, b: [] if slug == 2477 else
                        (CUTOUT if section == "Current Cutout Values" else
                         VOLUME if section == "Current Volume" else
                         [{"report_date": "11/20/2026", "primal_desc": "Primal Rib",
                           "choice_600_900": "679.88", "select_600_900": "633.89",
                           "published_date": "11/20/2026 14:42:56"}]))
    code = ub.run(datetime.date(2026, 11, 1), datetime.date(2026, 11, 20), "incremental",
                  "2026-11-21T00:00:00")
    assert code == 1
    assert list((tmp_path / "beef_cutout").rglob("*.parquet"))      # the good tables still land
    assert not list((tmp_path / "cattle_cash").rglob("*.parquet"))


def test_backfill_chunks_cover_the_range_without_gaps_or_overlap():
    start, end = datetime.date(2001, 1, 1), datetime.date(2026, 11, 21)
    chunks = list(ub._chunks(start, end))
    assert chunks[0][0] == start and chunks[-1][1] == end
    for (a1, b1), (a2, b2) in zip(chunks, chunks[1:]):
        assert a2 == b1 + datetime.timedelta(days=1)
