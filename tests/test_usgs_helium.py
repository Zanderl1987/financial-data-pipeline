"""
Tests for the USGS helium pipelines (MCS ScienceBase releases + DS-140
historical statistics).

All HTTP is mocked — no network access. Synthetic payloads mirror the real
file layouts probed live 2026-08-24:
  - mcsYYYY-heliu_salient.csv (wide, cp1252, varying Grade-A column name)
  - mcsYYYY-heliu_world.csv   (year embedded in column headers)
  - MCS2026 combined Commodities_Data.csv (tidy, chapter-filtered)
  - ds140 single-sheet workbook (banner rows above the Year header)
"""

import io
import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import usgs_helium_mcs_pipeline as mcs
import usgs_ds140_pipeline as ds140


# ── MCS salient parser ─────────────────────────────────────────────────────────

SALIENT_CSV = (
    "DataSource,Commodity,Year,Extracted_mcm,Withdrawn_mcm,Grade-A-Salesmcm,"
    "Imports_mcm,Exports_mcm,Consump_mcm,NIR_pct\r\n"
    "MCS2022,Helium,2020,76,7,83,7,50,40,E\r\n"
    "MCS2022,Helium,2021,71,6,77,9,46,38,E\r\n"
).encode("cp1252")


class TestSalientParser:
    def test_long_reshape(self):
        df = mcs._parse_salient(SALIENT_CSV, "Mineral Commodity Summaries 2022 - HELIUM Data Release")
        assert set(df["series"]) == {
            "extracted", "withdrawn_from_storage", "grade_a_sales",
            "imports", "exports", "apparent_consumption",
        }
        row = df[(df.obs_year == 2020) & (df.series == "extracted")].iloc[0]
        assert row.value == 76
        assert row.unit == "million cubic meters"
        assert row.commodity == "helium"

    def test_footnote_code_becomes_nan_and_is_dropped(self):
        df = mcs._parse_salient(SALIENT_CSV, "t")
        assert not (df.series == "net_import_reliance").any()

    def test_release_title_preserved(self):
        df = mcs._parse_salient(SALIENT_CSV, "Release X")
        assert (df.source_release == "Release X").all()

    def test_varying_grade_a_column_name(self):
        renamed = SALIENT_CSV.replace(b"Grade-A-Salesmcm", b"Grade-A-Salescm")
        df = mcs._parse_salient(renamed, "t")
        assert (df.series == "grade_a_sales").sum() == 2


# ── MCS world parser ───────────────────────────────────────────────────────────

WORLD_CSV = (
    'Source,Country,Type,Prod_mcm_2020,Prod_mcm_Est_2021,Reserves_mcm,,\r\n'
    'MCS2024,United States (extracted from natural gas),"Mine production, helium",65,60,8500,,\r\n'
    'MCS2024,Qatar,"Mine production, helium",59,66,NA,,\r\n'
    'MCS2024,World total (rounded),Mine production,160,170,,,\r\n'
).encode("cp1252")


class TestWorldParser:
    def test_year_from_header_and_country_slug(self):
        df = mcs._parse_world(WORLD_CSV, "t")
        qatar = df[df.country == "qatar"]
        assert set(qatar.series) == {"world_production_qatar"}
        assert sorted(qatar.obs_year) == [2020, 2021]

    def test_parenthetical_kept_for_uniqueness(self):
        df = mcs._parse_world(WORLD_CSV, "t")
        assert "world_production_united_states_extracted_from_natural_gas" in set(df.series)

    def test_rounded_suffix_stripped(self):
        df = mcs._parse_world(WORLD_CSV, "t")
        assert "world_production_world_total" in set(df.series)

    def test_reserves_fall_back_to_latest_prod_year(self):
        df = mcs._parse_world(WORLD_CSV, "t")
        reserves = df[df.series.str.startswith("reserves")]
        assert (reserves.obs_year == 2021).all()
        # Qatar reserves are NA -> dropped; US survives
        assert set(reserves.country) == {"united_states_extracted_from_natural_gas"}


# ── MCS combined (2026+) parser ────────────────────────────────────────────────

COMBINED_CSV = (
    "MCS chapter,Section,Commodity,Country,Statistics,Statistics_detail,Unit,Year,Value,Notes\r\n"
    "HELIUM AND RARE GASES,Salient Statistics-US,"
    "Helium,United States,Sold or used,Sold or Used: Grade-A helium,million cubic meters,2024,81,\r\n"
    "HELIUM AND RARE GASES,World Production and Reserves,Helium,Qatar,Production,"
    "Helium Production,million cubic meters,2025,63,\r\n"
    "HELIUM AND RARE GASES,Series with text year,Helium,United States,Import sources,"
    "Import sources 2021-24,percent,2021-24,E,\r\n"
    "FELDSPAR,Salient,Feldspar,United States,Production,Prod,thousand metric tons,2023,430,\r\n"
).encode("cp1252")


class TestCombinedParser:
    def test_filters_chapter_and_maps_series(self):
        df = mcs._parse_combined(COMBINED_CSV, "Combined release")
        assert len(df) == 2
        assert set(df.commodity) == {"helium"}
        assert set(df.series) == {"sold_or_used", "production"}

    def test_non_numeric_year_rows_dropped(self):
        df = mcs._parse_combined(COMBINED_CSV, "t")
        assert "import_sources" not in set(df.series)


# ── Release discovery (mocked HTTP) ────────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload=None, content=b"", status_code=200):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


def _item(item_id, title, files=None):
    return {"id": item_id, "title": title, "files": files or []}


def test_fetch_release_dedicated_item(monkeypatch):
    calls = []

    def fake_get_json(url, params):
        calls.append(params.get("q", ""))
        if "- HELIUM Data Release" in params.get("q", ""):
            return {"items": [_item("abc123", str(params["q"]).strip('"'))]}
        return {"items": []}

    def fake_files(item_id):
        return [
            {"name": "mcs2022-heliu_meta.xml", "url": "u1"},
            {"name": "mcs2022-heliu_salient.csv", "url": "u2"},
        ]

    monkeypatch.setattr(mcs, "_get_json", fake_get_json)
    monkeypatch.setattr(mcs, "_sb_files", fake_files)
    monkeypatch.setattr(mcs, "_get_bytes", lambda url: SALIENT_CSV)
    monkeypatch.setattr(mcs.time, "sleep", lambda s: None)

    frames, title = mcs._fetch_release(2022)
    assert len(frames) == 1
    assert "2022" in title
    assert not frames[0].empty


def test_fetch_release_combined_fallback(monkeypatch):
    def fake_get_json(url, params):
        q = params.get("q", "")
        if "Data Release\"" in q and "HELIUM" in q:
            return {"items": []}
        return {"items": [_item("xyz789", "MCS 2026 Data Release - Commodity Salient U.S. and World Statistics")]}

    monkeypatch.setattr(mcs, "_get_json", fake_get_json)
    monkeypatch.setattr(mcs, "_sb_files", lambda iid: [{"name": "MCS2026_Commodities_Data.csv", "url": "u"}])
    monkeypatch.setattr(mcs, "_get_bytes", lambda url: COMBINED_CSV)
    monkeypatch.setattr(mcs.time, "sleep", lambda s: None)

    frames, title = mcs._fetch_release(2026)
    assert "Commodity Salient" in title
    assert len(frames) == 1


# ── DS-140 parser ──────────────────────────────────────────────────────────────

def _ds140_workbook() -> bytes:
    raw = pd.DataFrame([
        ["HELIUM STATISTICS", None, None, None],
        ["U.S. GEOLOGICAL SURVEY", None, None, None],
        ["Year", "Production", "Shipments", "Unit value ($/t)"],
        [1935, 49, None, None],
        [1936, 22.4, None, 2830],
        ["NA Not available.", None, None, None],
    ])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        raw.to_excel(writer, index=False, header=False)
    return buf.getvalue()


class TestDs140Parser:
    def test_long_reshape_skips_footnotes(self):
        df = ds140._parse_ds140(_ds140_workbook())
        assert sorted(df.obs_year.unique()) == [1935, 1936]
        assert set(df.metric) == {"production", "unit_value_nominal"}
        row = df[(df.obs_year == 1936) & (df.metric == "production")].iloc[0]
        assert row.value == 22.4
        assert row.unit == "metric tons helium"

    def test_metric_units_split(self):
        df = ds140._parse_ds140(_ds140_workbook())
        assert set(df.loc[df.metric == "production", "unit"]) == {"metric tons helium"}
        assert set(df.loc[df.metric == "unit_value_nominal", "unit"]) == {"dollars per metric ton"}


class TestDs140HashSkip:
    def _run(self, monkeypatch, tmp_path, content):
        monkeypatch.setattr(ds140, "BASE_DIR", str(tmp_path))
        monkeypatch.setattr(ds140, "HASH_FILE", str(tmp_path / ".ds140_sha256.txt"))
        monkeypatch.setattr(ds140, "_get_bytes", lambda url: content)
        monkeypatch.setattr(ds140.time, "sleep", lambda s: None)
        ds140.main()

    def test_second_run_with_same_bytes_writes_one_file(self, monkeypatch, tmp_path, capsys):
        content = _ds140_workbook()
        self._run(monkeypatch, tmp_path, content)
        self._run(monkeypatch, tmp_path, content)
        parquets = list(tmp_path.glob("**/*.parquet"))
        assert len(parquets) == 1
        assert "skipping" in capsys.readouterr().out.lower()

    def test_changed_bytes_trigger_reparse(self, monkeypatch, tmp_path, capsys):
        self._run(monkeypatch, tmp_path, _ds140_workbook())
        first_hash = (tmp_path / ".ds140_sha256.txt").read_text(encoding="utf-8")
        self._run(monkeypatch, tmp_path, _ds140_workbook() + b"x")
        out = capsys.readouterr().out.lower()
        assert "skipping" not in out
        assert (tmp_path / ".ds140_sha256.txt").read_text(encoding="utf-8") != first_hash
