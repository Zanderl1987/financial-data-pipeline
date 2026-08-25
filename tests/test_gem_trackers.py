"""
Tests for gem_trackers_pipeline — GEM tracker summary tables.

All HTTP is mocked. Synthetic fixtures mirror the real gviz CSV exports
probed live 2026-08-24: a metadata banner row whose last cell carries the
row-dimension label ("... Country/Area"), year columns, and aggregate rows
(TOTAL / regional groupings) after the country rows.
"""

import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import gem_trackers_pipeline as gem


PAGE_HTML = """
<div>
<a href="https://docs.google.com/spreadsheets/d/1j35F0WrRJ9dbIJhtRkm8fvPw0Vsf-JV6G95u7gT-DDw" target="_blank">
Newly Operating Coal Plants by Year (MW) View \uf061</a>
<a href="https://docs.google.com/spreadsheets/d/1t3gO35bzcVI8ekq9318jBUq6nd7UADcut4gY3vjHZMM/edit?usp=sharing">
Retired Coal Plants (MW) View \uf061</a>
<a href="https://example.com/not-a-sheet">ignore me</a>
</div>
"""

SHEET_CSV = (
    '"New Coal-fired Power Capacity by Country Global Coal Plant Tracker, '
    'Global Energy Monitor This data is based on the Global Coal Plant Tracker, '
    'updated July 2026 (Download here) Unit of measurement: Megawatts (MW) Country/Area",'
    '"2000","2001","Total","",""\n'
    '"Afghanistan","0","0","0","",""\n'
    '"Zimbabwe","0","100","100","",""\n'
    '"TOTAL","0","100","100","",""\n'
)


class TestDiscoverSheets:
    def test_extracts_text_to_id_pairs(self, monkeypatch):
        monkeypatch.setattr(gem, "_get", lambda url, timeout=60: PAGE_HTML.encode("utf-8"))
        sheets = gem.discover_sheets("https://example.com/page")
        assert sheets == {
            "Newly Operating Coal Plants by Year (MW)": "1j35F0WrRJ9dbIJhtRkm8fvPw0Vsf-JV6G95u7gT-DDw",
            "Retired Coal Plants (MW)": "1t3gO35bzcVI8ekq9318jBUq6nd7UADcut4gY3vjHZMM",
        }

    def test_page_failure_returns_empty_and_fallback_applies(self, monkeypatch):
        monkeypatch.setattr(gem, "_get", lambda url, timeout=60: None)
        assert gem.discover_sheets("https://example.com/page") == {}
        assert len(gem.FALLBACK_SHEETS) >= 6

    def test_browser_user_agent_required(self):
        assert "Mozilla/5.0" in gem.HEADERS["User-Agent"]


class TestParseSheet:
    def test_tidy_rows_per_country_year(self):
        df = gem.parse_sheet(SHEET_CSV, "Newly Operating Coal Plants by Year (MW)", "sid1")
        assert len(df) == 9   # 3 regions x (2 years + Total)
        zimbabwe_2001 = df[(df.country_or_region == "Zimbabwe") & (df.obs_year == 2001)]
        assert zimbabwe_2001.iloc[0].value == 100

    def test_metadata_extracted(self):
        df = gem.parse_sheet(SHEET_CSV, "ind", "sid1")
        assert (df.release_label == "July 2026").all()
        assert (df.unit == "Megawatts (MW)").all()

    def test_total_column_kept_without_year(self):
        df = gem.parse_sheet(SHEET_CSV, "ind", "sid1")
        total_col = df[df.column_label == "Total"]
        assert total_col.obs_year.isna().all()
        assert (total_col.value >= 0).all()
        assert total_col.iloc[-1].value == 100   # Zimbabwe / grand totals

    def test_aggregate_rows_retained(self):
        df = gem.parse_sheet(SHEET_CSV, "ind", "sid1")
        assert "TOTAL" in set(df.country_or_region)

    def test_tracker_sheet_and_indicator_stamped(self):
        df = gem.parse_sheet(SHEET_CSV, "My Indicator", "sheet-42")
        assert (df.tracker_sheet == "sheet-42").all()
        assert (df.indicator == "My Indicator").all()

    def test_non_numeric_values_skipped(self):
        csv = ('"Blob Unit of measurement: MW Country/Area","2000"\n'
               '"X","n/a"\n')
        df = gem.parse_sheet(csv, "ind", "sid")
        assert df.empty


class TestStageColumnSheets:
    def test_combustion_technology_layout(self):
        csv = (
            '"Coal Power Capacity by Combustion Technology Global Coal Plant Tracker '
            'updated July 2026 Unit of measurement: Megawatts (MW) Combustion technology",'
            '"Announced","Construction","Shelved"\n'
            '"Subcritical","10","20","30"\n'
        )
        df = gem.parse_sheet(csv, "Coal Plants by Combustion Technology", "sid")
        assert set(df.column_label) == {"Announced", "Construction", "Shelved"}
        assert df.obs_year.isna().all()
        assert len(df) == 3
