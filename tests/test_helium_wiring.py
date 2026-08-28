"""
Wiring tests for the helium/GEM batch: comtrade HS code coverage, catalog
storage dirs, and curated KEYS entries for the new tables.
"""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


class TestComtradeHelium:
    def test_hs_280429_registered(self):
        import comtrade_pipeline as ct
        assert "280429" in ct.HS_CODES
        name, category = ct.HS_CODES["280429"]
        assert name == "Helium"
        assert category == "industrial_gases"


class TestCuratedKeys:
    @pytest.mark.parametrize("table", [
        "usgs_mcs_helium",
        "usgs_ds140_helium",
        "gem_coal_summary",
    ])
    def test_new_tables_are_keyed(self, table):
        import curated
        assert table in curated.KEYS
