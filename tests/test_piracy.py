"""
test_piracy.py — offline unit tests for piracy_pipeline parsers.

No network: exercises region classification, ransom parsing, wikitext
stripping, and the {{Hijacked ship}} template parser against embedded
samples mirroring the live page structure.
"""

import datetime
import os
import sys

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import piracy_pipeline as pp


NOW = datetime.datetime(2026, 8, 23, 12, 0, 0)


class TestRegionClassification:
    def test_gulf_of_aden(self):
        assert pp.classify_region(12.5, 48.0) == "gulf_of_aden_somalia"

    def test_somali_basin_south(self):
        assert pp.classify_region(-5.0, 55.0) == "gulf_of_aden_somalia"

    def test_strait_of_malacca(self):
        assert pp.classify_region(2.5, 101.0) == "southeast_asia"

    def test_gulf_of_guinea(self):
        assert pp.classify_region(4.0, 3.0) == "gulf_of_guinea"

    def test_callao(self):
        assert pp.classify_region(-12.0, -77.0) == "americas_caribbean"

    def test_other(self):
        assert pp.classify_region(50.0, 0.0) == "other"


class TestRansomParsing:
    def test_plain_usd(self):
        assert pp._parse_ransom_usd("US$315,000") == 315_000.0

    def test_million(self):
        assert pp._parse_ransom_usd("$3.5 million") == 3_500_000.0

    def test_billion(self):
        assert pp._parse_ransom_usd("US$1.2 billion") == 1_200_000_000.0

    def test_unknown(self):
        assert pp._parse_ransom_usd("unknown") is None
        assert pp._parse_ransom_usd("") is None


class TestWikitextStripping:
    def test_mv_template(self):
        assert pp._strip_wikitext("{{MV|Feisty Gas}}") == "Feisty Gas"

    def test_link_with_label(self):
        assert pp._strip_wikitext("[[bulk carrier]] with [[coal]]") == "bulk carrier with coal"

    def test_ref_and_html_removed(self):
        out = pp._strip_wikitext("Seized <ref>{{cite web|foo}}</ref> off [[Somalia]].<br/>Later released")
        assert "<ref" not in out and "[[" not in out and "<br" not in out
        assert "Somalia" in out

    def test_convert_template(self):
        assert "90 nmi" in pp._strip_wikitext("some {{convert|90|nmi|km}} off the coast")

    def test_ship_template_with_prefix(self):
        assert pp._strip_wikitext("{{Ship|FV|Ching Fong Hwa 168}}") == "FV Ching Fong Hwa 168"

    def test_ship_template_empty_prefix(self):
        assert pp._strip_wikitext("{{ship||OS 35}}") == "OS 35"

    def test_sclass_template(self):
        assert pp._strip_wikitext("{{Sclass|Ticonderoga|cruiser|1}}") == "Ticonderoga-class cruiser"

    def test_html_comment_removed(self):
        assert "Probably released" not in pp._strip_wikitext("unknown<!-- Probably released but needs source-->")

    def test_dual_vessel_name_join(self):
        fields = {"name": "{{Ship|FV|Mavuno No. 1}}", "name2": "{{Ship|FV|Mavuno No. 2}}"}
        assert pp._clean_vessel_name(fields) == "FV Mavuno No. 1 / FV Mavuno No. 2"


WIKI_SAMPLE = """
==List of ships captured or attacked off the Somali coast==

===2008===
{{Hijacked ship head}}
{{Hijacked ship
  |flag=Hong Kong
  |owner=Hong Kong
  |name={{MV|Feisty Gas}}
  |class=[[LPG carrier]]
  |crew=120
  |cargo=''unknown''
  |status=Released<br/>after ransom
  |ransom=[[United States dollar|US$]]315,000
  |cdate=2008-04-10
  |rdate=unknown
  |info=MV ''Feisty Gas'', a liquefied petroleum gas tanker, was seized by
[[Somali]] pirates some {{convert|90|nmi|km}} offshore.
}}
{{Hijacked ship
  |image=[[Image:SeabournSpirit.jpg|100px]]
  |flag=Bahamas
  |owner=United States
  |name={{MV|Seabourn Spirit}}
  |class=[[cruise ship]]
  |crew=210
  |cargo=cruise passengers
  |status=Failed attack
  |ransom=none demanded
  |cdate=2005-11-05
  |rdate=unknown
  |info=Attempted hijacking repelled.
}}
{{Hijacked ship
  |flag=Tanzania
  |owner=South Korea
  |num=2
  |name={{Ship|FV|Mavuno No. 1}}
  |class=[[fishing vessel]]
  |name2={{Ship|FV|Mavuno No. 2}}
  |class2=[[fishing vessel]]
  |crew=25
  |cargo=Fishing equipment
  |status=''unknown''<!-- Probably released but needs source-->
  |ransom=''none''
  |cdate=2007-05-15
  |rdate=2007-11-05
  |info=Taiwanese-owned fishing vessels hijacked together.
}}
"""


class TestWikiParser:
    def setup_method(self):
        self.df = pp.parse_wiki_wikitext(WIKI_SAMPLE, NOW)

    def test_row_count(self):
        assert len(self.df) == 3

    def test_vessel_names(self):
        assert self.df["vessel_name"].tolist() == [
            "Feisty Gas",
            "Seabourn Spirit",
            "FV Mavuno No. 1 / FV Mavuno No. 2",
        ]

    def test_section_year_tracks_heading(self):
        # Both templates sit under the ===2008=== heading regardless of cdate.
        assert (self.df["section_year"] == 2008).all()

    def test_dates_parsed(self):
        assert self.df["incident_date"].iloc[0] == pd.Timestamp("2008-04-10").date()
        assert pd.isna(self.df["release_date"].iloc[0])

    def test_ransom_extracted(self):
        assert self.df["ransom_usd"].iloc[0] == 315_000.0

    def test_crew_int(self):
        assert self.df["crew_count"].tolist() == [120, 210, 25]

    def test_description_cleaned(self):
        desc = self.df["description"].iloc[0]
        assert "{{" not in desc and "[[" not in desc and "90 nmi" in desc

    def test_row_num_sequential_within_section(self):
        assert self.df["row_num"].tolist() == [0, 1, 2]

    def test_empty_page_returns_empty_frame(self):
        assert pp.parse_wiki_wikitext("", NOW).empty
