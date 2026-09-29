"""
Tests for epa_fuel_pipeline — EPA GHG energy emissions + RFS RIN data.

All HTTP is mocked. The GHG fixture mirrors a real time slice of the EPA
GHG Inventory Table 3-1 ("CO2, CH4, and N2O Emissions from Energy, MMT
CO2 Eq.") with quoted thousands separators and footnote rows. The RFS
fixture mirrors the EPA rindata CSV columns and its cumulative-history shape.
"""

import io
import os
import sys
import zipfile

import pandas as pd
import pytest
import responses

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import epa_fuel_pipeline as epa


GHG_ZIP_URL = epa.GHG_ZIP_URL
RFS_PAGE_URL = epa.RFS_PAGE_URL

RFS_PAGE_HTML = """
<div>
<a href="https://www.epa.gov/system/files/other-files/2026-08/rindata_jul2026.csv">jul 2026</a>
<a href="https://www.epa.gov/system/files/other-files/2026-07/rindata_jun2026.csv">jun 2026</a>
<a href="https://www.epa.gov/sites/default/files/2017-10/rindata_jan2013.csv">jan 2013</a>
</div>
"""

# Two fuel codes x four months, cumulative-history shape (oldest first)
RFS_CSV = (
    "FUEL_CODE,RIN_YEAR,Production Month,RIN_QUANTITY,BATCH_VOLUME\n"
    "6,2014,1,100500,100400\n"
    "4,2014,1,30020,18012\n"
    "6,2014,2,110700,110600\n"
    "4,2014,2,31020,18612\n"
)

# Gas group total + source rows for CO2, then a Total row; footnotes to skip.
# Years truncated to 1990/2022 to keep the fixture small; each data row carries
# exactly one value per year column.
GHG_CSV = (
    '"Table 3-1:  CO2, CH4, and N2O Emissions from Energy (MMT CO2 Eq.)"\n'
    "Gas/Source,1990,2022\n"
    'CO2," 5,381.0 "," 4,875.5 "\n'
    'Fossil Fuel Combustion," 5,196.8 "," 4,592.4 "\n'
    'Transportation," 1,468.9 "," 1,751.3 "\n'
    'Electricity Generation," 1,820.0 "," 1,531.7 "\n'
    "+ Does not exceed 0.05 MMT CO2 Eq.,,\n"
    "a Emissions from biomass and biofuel consumption are not included.,,\n"
    "Note: Totals may not sum due to independent rounding.,,\n"
)


# Real 2022 CO2 block of Table 3-1 (1990-2022 inventory), footnote letters and
# all. Sources sum to the gas total; sectors sum to Fossil Fuel Combustion.
GHG_CO2_2022_CSV = (
    "Gas/Source,2022\n"
    'CO2," 4,875.5 "\n'
    'Fossil Fuel Combustion," 4,699.4 "\n'
    'Transportation," 1,751.3 "\n'
    'Electricity Generation," 1,531.7 "\n'
    "Industrial,801.1\n"
    "Residential,334.1\n"
    "Commercial,258.7\n"
    "U.S. Territories,22.6\n"
    "Non-Energy Use of Fuels,102.8\n"
    "Natural Gas Systems,36.5\n"
    "Petroleum Systems,22.0\n"
    "Incineration of Waste,12.4\n"
    "Coal Mining,2.5\n"
    "Biomass-Wooda,195.3\n"
    "International Bunker Fuelsb,98.2\n"
    "Biofuels-Ethanola,79.6\n"
    "Biofuels-Biodiesela,15.6\n"
    "Biomass-MSWa,14.9\n"
)


class TestGHGHierarchy:
    def test_source_rows_sum_to_gas_total(self):
        df = epa._parse_ghg_table(GHG_CO2_2022_CSV).set_index("source")
        total = df.loc["Total", "value"]
        sources = df[df["level"] == "source"]["value"].sum()
        assert abs(sources - total) < 0.5  # EPA rounds each row independently
        # the naive all-rows sum is what the level column exists to prevent
        assert df["value"].sum() > 1.9 * total

    def test_sectors_sum_to_fossil_fuel_combustion(self):
        df = epa._parse_ghg_table(GHG_CO2_2022_CSV).set_index("source")
        sectors = df[df["level"] == "sub_source"]["value"].sum()
        assert abs(sectors - df.loc["Fossil Fuel Combustion", "value"]) < 0.5

    def test_footnote_letters_stripped_and_memo_flagged(self):
        df = epa._parse_ghg_table(GHG_CO2_2022_CSV)
        memo = set(df[df["level"] == "memo"]["source"])
        assert memo == {
            "Biomass-Wood", "International Bunker Fuels", "Biofuels-Ethanol",
            "Biofuels-Biodiesel", "Biomass-MSW",
        }

    def test_non_memo_labels_untouched(self):
        # a label that happens to end in "a"/"b" must keep its last letter
        csv = "Gas/Source,2022\nCH4,10.0\nFlora,1.0\n"
        df = epa._parse_ghg_table(csv)
        assert "Flora" in df["source"].tolist()


class TestParseGHG:
    def test_parses_gas_and_source_rows(self):
        df = epa._parse_ghg_table(GHG_CSV)
        assert len(df) == 8  # 4 rows x 2 years
        assert sorted(df["gas"].unique()) == ["CO2"]
        assert set(df["source"].unique()) == {
            "Total", "Fossil Fuel Combustion", "Transportation",
            "Electricity Generation",
        }
        total = df[(df["source"] == "Total") & (df["date"] == pd.Timestamp("2022-07-01"))]
        assert total["value"].iloc[0] == 4875.5
        assert (df["unit"] == "MMT CO2 Eq.").all()

    def test_skips_footnotes(self):
        df = epa._parse_ghg_table(GHG_CSV)
        assert "Note:" not in df["source"].astype(str).tolist()
        assert "+ Does" not in df["source"].astype(str).tolist()
        assert (df["value"] > 0).all()  # no NaN for "+" placeholders


class TestParseRFS:
    def test_normalizes_cumulative_csv(self):
        df = epa._parse_rfs_csv(RFS_CSV)
        assert list(df.columns) == [
            "date", "fuel_code", "fuel_name", "rin_year", "prod_month",
            "rin_quantity", "batch_volume", "fetched_at",
        ]
        assert len(df) == 4
        assert df["fuel_code"].tolist() == [6, 4, 6, 4]
        assert df.iloc[0]["rin_quantity"] == 100500
        assert df.iloc[0]["date"] == pd.Timestamp("2014-01-01")
        assert df.iloc[0]["fuel_name"] == "Renewable fuel"

    def test_missing_column_raises(self):
        renamed = RFS_CSV.replace("Production Month", "Prod Mo")
        with pytest.raises(ValueError, match="prod_month"):
            epa._parse_rfs_csv(renamed)


JUL_URL = "https://www.epa.gov/system/files/other-files/2026-08/rindata_jul2026.csv"
JUN_URL = "https://www.epa.gov/system/files/other-files/2026-07/rindata_jun2026.csv"
# An older vintage of the same months, before EPA's upward revisions
RFS_CSV_OLD = RFS_CSV.replace("100500", "90000")


def _read_rfs(tmp_path):
    files = list((tmp_path / epa.RFS_DIR).rglob("*.parquet"))
    assert len(files) == 1
    return pd.read_parquet(files[0])


class TestPipelineEndToEnd:
    @responses.activate
    def test_fetch_rfs_rin_single_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        responses.add(responses.GET, RFS_PAGE_URL, body=RFS_PAGE_HTML, status=200)
        responses.add(responses.GET, JUL_URL, body=RFS_CSV, status=200,
                      content_type="text/csv")
        n = epa.fetch_rfs_rin()
        assert n == 4

    @responses.activate
    def test_rfs_uses_newest_file_only(self, tmp_path, monkeypatch):
        # Older vintages must never overwrite the newest, revised numbers
        monkeypatch.chdir(tmp_path)
        responses.add(responses.GET, RFS_PAGE_URL, body=RFS_PAGE_HTML, status=200)
        responses.add(responses.GET, JUL_URL, body=RFS_CSV, status=200)
        responses.add(responses.GET, JUN_URL, body=RFS_CSV_OLD, status=200)
        epa.fetch_rfs_rin()
        df = _read_rfs(tmp_path)
        assert 100500 in df["rin_quantity"].tolist()
        assert 90000 not in df["rin_quantity"].tolist()
        assert [c.request.url for c in responses.calls] == [RFS_PAGE_URL, JUL_URL]

    @responses.activate
    def test_rfs_falls_back_when_newest_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(epa, "REQUEST_INTERVAL", 0)
        responses.add(responses.GET, RFS_PAGE_URL, body=RFS_PAGE_HTML, status=200)
        responses.add(responses.GET, JUL_URL, status=404)
        responses.add(responses.GET, JUN_URL, body=RFS_CSV_OLD, status=200)
        assert epa.fetch_rfs_rin() == 4
        assert 90000 in _read_rfs(tmp_path)["rin_quantity"].tolist()

    def test_main_fails_when_a_table_is_empty(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["epa_fuel_pipeline.py"])
        monkeypatch.setattr(epa, "fetch_ghg_energy", lambda: 8)
        monkeypatch.setattr(epa, "fetch_rfs_rin", lambda: 0)
        assert epa.main() == 1
        monkeypatch.setattr(epa, "fetch_rfs_rin", lambda: 4)
        assert epa.main() == 0

    @responses.activate
    def test_fetch_ghg_energy(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("energy_10-15/Table 3-1.csv", GHG_CSV)
        responses.add(
            responses.GET, GHG_ZIP_URL, body=buf.getvalue(),
            status=200, content_type="application/zip",
        )
        n = epa.fetch_ghg_energy()
        assert n == 8