"""Tests for the WRDS client and the Compustat point-in-time guard.

No credentials, no network, no `wrds` package. That is deliberate and is the
reason `wrds_client` imports `wrds` lazily -- the guard tests must be runnable on
a machine that has never had a WRDS account, and importing a credentialed client
at module scope would make that impossible.
"""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest

import compustat_pit as pit
import wrds_client


# ---------------------------------------------------------------------------
# wrds_client
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_is_ordered_by_phase_then_records(self):
        """Prerequisites first, then bulk unlocks by size.

        Records alone is the wrong sort: you cannot use crsp.daily until
        comp.g_link and comp.securities exist, so pulling 50M daily rows first
        only to discover the join key is missing wastes the afternoon.
        """
        keys = [(t.phase, -t.records) for t in wrds_client.MANIFEST]
        assert keys == sorted(keys)

    def test_prerequisites_precede_the_bulk_pulls(self):
        phases = {t.table: t.phase for t in wrds_client.MANIFEST}
        for prereq in ("crsp.msenames", "comp.securities", "comp.g_link"):
            assert phases[prereq] == wrds_client.PHASE_PREREQUISITE
        for bulk in ("crsp.daily", "comp.fundamentals_quarterly"):
            assert phases[bulk] == wrds_client.PHASE_BULK

    def test_link_table_precedes_the_bulk_pulls(self):
        """The join key must land before the tables that need it."""
        order = [t.table for t in wrds_client.MANIFEST]
        assert order.index("comp.g_link") < order.index("crsp.daily")
        assert order.index("comp.securities") < order.index("comp.fundamentals_quarterly")

    def test_every_target_names_its_library(self):
        for target in wrds_client.MANIFEST:
            library, name = wrds_client._split_table(target.table)
            assert library and name
            assert table_ok(target.table)

    def test_survivorship_unlocks_are_present(self):
        """The three tables that make the catalog's largest block testable."""
        tables = {t.table for t in wrds_client.MANIFEST}
        assert {"crsp.daily", "comp.fundamentals_quarterly", "crsp.msenames"} <= tables

    def test_link_table_present(self):
        """Fundamentals cannot join to prices without gvkey<->permno.

        Joining on ticker instead would rebuild the exact survivorship-prone
        match that SURVIVORSHIP.md measured as crediting coverage -- 900 of 1,067
        ticker-string matches being recycled tickers.
        """
        assert "comp.g_link" in {t.table for t in wrds_client.MANIFEST}

    def test_manifest_cli_runs_without_credentials(self, capsys, monkeypatch):
        monkeypatch.delenv("WRDS_USER", raising=False)
        monkeypatch.delenv("WRDS_PASS", raising=False)
        assert wrds_client.main(["manifest"]) == 0
        assert "crsp.daily" in capsys.readouterr().out


def table_ok(table: str) -> bool:
    library, _, name = table.partition(".")
    return library.islower() and name.islower()


class TestCredentials:
    def test_missing_credentials_raise_a_useful_error(self, monkeypatch, tmp_path):
        monkeypatch.delenv("WRDS_USER", raising=False)
        monkeypatch.delenv("WRDS_PASS", raising=False)
        monkeypatch.setattr(
            wrds_client, "__file__", str(tmp_path / "wrds_client.py")
        )
        with pytest.raises(wrds_client.WRDSUnavailable, match="not set"):
            wrds_client._credential("WRDS_USER")

    def test_error_message_does_not_ask_for_argv(self, monkeypatch, tmp_path):
        """argv lands in shell history and in `ps` output for other users."""
        monkeypatch.delenv("WRDS_USER", raising=False)
        monkeypatch.setattr(wrds_client, "__file__", str(tmp_path / "wrds_client.py"))
        with pytest.raises(wrds_client.WRDSUnavailable) as excinfo:
            wrds_client._credential("WRDS_USER")
        assert "command line" in str(excinfo.value)

    def test_env_value_is_used_without_touching_dotenv(self, monkeypatch):
        monkeypatch.setenv("WRDS_USER", "zander")
        assert wrds_client._credential("WRDS_USER") == "zander"

    def test_importing_the_client_does_not_require_the_wrds_package(self):
        """The module must import cleanly where `wrds` is not installed."""
        import importlib

        reloaded = importlib.import_module("wrds_client")
        assert hasattr(reloaded, "connection")


class TestDateNormalization:
    def test_yyyymmdd_integers_are_parsed_as_calendar_dates(self):
        """CRSP daily dates arrive as 19800104, not as day offsets."""
        frame = pd.DataFrame({"date": [19800104, 20260902], "permno": [1, 2]})
        out = wrds_client._normalize_dates(frame)
        assert out["date"].iloc[0] == pd.Timestamp("1980-01-04")
        assert out["date"].iloc[1] == pd.Timestamp("2026-09-02")

    def test_sas_day_offsets_are_parsed_from_the_1960_epoch(self):
        """A SAS-native field counts days since 1960-01-01, not YYYYMMDD."""
        offset = (pd.Timestamp("2026-01-02") - pd.Timestamp("1960-01-01")).days
        frame = pd.DataFrame({"datadate": [offset]})
        out = wrds_client._normalize_dates(frame)
        assert out["datadate"].iloc[0] == pd.Timestamp("2026-01-02")

    def test_the_two_encodings_do_not_collide(self):
        """The distinction is the whole reason both are detected explicitly."""
        yyyymmdd = wrds_client._normalize_dates(pd.DataFrame({"date": [19800104]}))
        offset = (pd.Timestamp("1980-01-04") - pd.Timestamp("1960-01-01")).days
        dayoff = wrds_client._normalize_dates(pd.DataFrame({"date": [offset]}))
        assert yyyymmdd["date"].iloc[0] == dayoff["date"].iloc[0]
        assert offset != 19800104

    def test_already_datetime_dates_are_left_alone(self):
        frame = pd.DataFrame({"date": pd.to_datetime(["2026-01-02"])})
        out = wrds_client._normalize_dates(frame)
        assert out["date"].iloc[0] == pd.Timestamp("2026-01-02")

    def test_unrecognised_integer_dates_are_left_untouched(self):
        """A NaT column is obvious; a silently wrong date is not.

        Guessing at an unknown encoding is how a panel ends up full of plausible
        1970 dates, so an unrecognised column is passed through for
        validate.py to report instead of being coerced.
        """
        frame = pd.DataFrame({"date": [-99999999, 1e12]})
        out = wrds_client._normalize_dates(frame)
        assert out["date"].iloc[0] == -99999999

    def test_non_date_numeric_columns_are_not_coerced(self):
        frame = pd.DataFrame({"permno": [10004], "ret": [0.013]})
        out = wrds_client._normalize_dates(frame)
        assert out["permno"].iloc[0] == 10004
        assert out["ret"].iloc[0] == pytest.approx(0.013)


class TestRowCap:
    def test_oversized_result_raises_rather_than_truncating(self, monkeypatch):
        """A silently shortened panel biases toward survivors. Fail instead."""

        class FakeDB:
            def raw_sql(self, sql):
                return pd.DataFrame({"x": range(10)})

        with pytest.raises(wrds_client.RowCapExceeded):
            wrds_client._query(FakeDB(), "SELECT 1", max_rows=5)

    def test_row_cap_message_says_not_to_raise_the_cap(self, monkeypatch):
        class FakeDB:
            def raw_sql(self, sql):
                return pd.DataFrame({"x": range(10)})

        with pytest.raises(wrds_client.RowCapExceeded, match="do not raise the cap"):
            wrds_client._query(FakeDB(), "SELECT 1", max_rows=5)

    def test_entitlement_failure_is_typed(self):
        class FakeDB:
            def raw_sql(self, sql):
                raise RuntimeError("Table not authorized for this user")

        with pytest.raises(wrds_client.NotEntitled):
            wrds_client._query(FakeDB(), "SELECT 1", max_rows=100)

    def test_transient_failure_is_retried_then_raises(self, monkeypatch):
        monkeypatch.setattr(wrds_client, "RETRY_SLEEP_SECONDS", 0)
        attempts = []

        class FlakyDB:
            def raw_sql(self, sql):
                attempts.append(1)
                raise RuntimeError("connection reset")

        with pytest.raises(wrds_client.WRDSUnavailable):
            wrds_client._query(FlakyDB(), "SELECT 1", max_rows=100)
        assert len(attempts) == wrds_client.MAX_RETRIES


class TestLicensedRoot:
    def test_default_root_is_outside_curated(self):
        """`upload_huggingface` publishes storage/curated wholesale."""
        assert wrds_client.LICENSED_ROOT != wrds_client.LICENSED_ROOT.parent / "curated"
        assert wrds_client.LICENSED_ROOT.name == "licensed"
        assert wrds_client.LICENSED_ROOT.parent.name == "storage"

    def test_cache_path_is_scoped_by_library_and_table(self):
        path = wrds_client._cache_dir("crsp", "daily", root=pathlib.Path("/tmp/x"))
        assert path.as_posix() == "/tmp/x/crsp/daily"


# ---------------------------------------------------------------------------
# compustat_pit
# ---------------------------------------------------------------------------


def restated_panel():
    """Three versions of one fiscal period, plus a second firm.

    gvkey 1004, datadate 2019-06-30, fq 2:
        rvq 2019-08-31  niq 10  -- original
        rvq 2021-08-31  niq 12  -- restated
        rvq 2026-06-30  niq 99  -- restated again, latest=1, garbage on purpose
    """
    return pd.DataFrame(
        {
            "gvkey": [1004, 1004, 1004],
            "datadate": pd.to_datetime(["2019-06-30"] * 3),
            "fq": [2, 2, 2],
            "rvq": pd.to_datetime(["2019-08-31", "2021-08-31", "2026-06-30"]),
            "niq": [10.0, 12.0, 99.0],
            "latest": [None, None, "1"],
        }
    )


class TestPITSnapshot:
    def test_as_of_before_any_restatement_sees_the_original(self):
        snap = pit.pit_snapshot(restated_panel(), "2020-01-15")
        assert snap["niq"].iloc[0] == 10.0
        assert snap["rvq"].iloc[0] == pd.Timestamp("2019-08-31")

    def test_as_of_between_restatements_sees_the_first_restatement(self):
        snap = pit.pit_snapshot(restated_panel(), "2022-01-15")
        assert snap["niq"].iloc[0] == 12.0

    def test_as_of_after_all_restatements_sees_the_last(self):
        snap = pit.pit_snapshot(restated_panel(), "2026-09-01")
        assert snap["niq"].iloc[0] == 99.0

    def test_latest_flag_is_never_used_as_the_filter(self):
        """The whole module exists because `latest=1` looks right and is not.

        It selects the final restatement regardless of when it was filed, so it
        hands 2026 values to a 2020 cross-section.
        """
        panel = restated_panel()
        for asof in ("2020-01-15", "2022-01-15"):
            snap = pit.pit_snapshot(panel, asof)
            assert snap["niq"].iloc[0] != 99.0, (
                f"as-of {asof} picked up a restatement filed years later"
            )

    def test_filing_lag_delays_visibility(self):
        """rvq 2019-08-31 + 45 days is 2019-10-15, not 2019-08-31."""
        panel = restated_panel()
        assert pit.pit_snapshot(panel, "2019-09-01", verify=False).empty
        assert len(pit.pit_snapshot(panel, "2019-11-01", verify=False)) == 1

    def test_one_row_per_observation(self):
        snap = pit.pit_snapshot(restated_panel(), "2026-09-01")
        assert len(snap) == 1

    def test_available_from_is_carried_for_downstream_lags(self):
        snap = pit.pit_snapshot(restated_panel(), "2026-09-01")
        assert snap["available_from"].iloc[0] == pd.Timestamp("2026-08-14")

    def test_empty_result_is_an_empty_frame_not_an_error(self):
        snap = pit.pit_snapshot(restated_panel(), "1990-01-01")
        assert snap.empty

    def test_duplicate_rows_from_overlapping_pulls_collapse(self):
        """Incremental re-pulls produce exact duplicates; they are not versions."""
        panel = pd.concat([restated_panel(), restated_panel()], ignore_index=True)
        snap = pit.pit_snapshot(panel, "2022-01-15")
        assert len(snap) == 1
        assert snap["niq"].iloc[0] == 12.0

    def test_multiple_firms_are_snapshotted_independently(self):
        panel = restated_panel()
        other = panel.copy()
        other["gvkey"] = 2001
        other["niq"] = 55.0
        snap = pit.pit_snapshot(pd.concat([panel, other], ignore_index=True), "2022-01-15")
        assert len(snap) == 2
        assert set(snap["gvkey"]) == {1004, 2001}


class TestLookAheadAssertion:
    """The differential guard: `snapshot` comes from OUTSIDE, and is checked.

    An earlier version of this file computed both sides from the same filtered
    frame, which made the comparison trivially true -- and the test for a planted
    look-ahead caught it. Hence these tests supply the snapshot explicitly.
    """

    def test_correct_snapshot_passes(self):
        panel = restated_panel()
        pit.assert_no_lookahead(pit.pit_snapshot(panel, "2022-01-15"), panel, "2022-01-15")

    def test_latest_flag_selection_is_caught(self):
        """The real leak, not a synthetic one.

        `latest=1` is what almost everyone writes first and it hands the final
        restatement to every cross-section. If the guard passes this, the guard
        is broken.
        """
        panel = restated_panel()
        leaky = pit.latest_flag_snapshot(panel)
        assert leaky["niq"].iloc[0] == 99.0, "precondition: latest=1 leaks"
        with pytest.raises(AssertionError, match="look-ahead detected"):
            pit.assert_no_lookahead(leaky, panel, "2022-01-15")

    def test_planted_future_row_is_caught_by_cardinality(self):
        panel = restated_panel()
        cutoff = pd.Timestamp("2022-01-15") - pd.Timedelta(days=pit.FILING_LAG_DAYS)
        contaminated = panel[pd.to_datetime(panel["rvq"]) <= cutoff]
        extra = panel[pd.to_datetime(panel["rvq"]) > cutoff].head(1)
        planted = pd.concat([contaminated, extra], ignore_index=True)
        planted = planted[planted["gvkey"] != 1004].copy()
        planted.loc[len(planted)] = {
            "gvkey": 9999, "datadate": pd.Timestamp("2019-06-30"), "fq": 2,
            "rvq": pd.Timestamp("2026-06-30"), "niq": 42.0, "latest": "1",
        }
        with pytest.raises(AssertionError, match="look-ahead detected"):
            pit.assert_no_lookahead(planted, panel, "2022-01-15")

    def test_planted_wrong_values_are_caught_by_content(self):
        """Same row count, wrong restatement: a cardinality check would pass."""
        panel = restated_panel()
        truth = pit.pit_snapshot(panel, "2022-01-15")
        tampered = truth.copy()
        tampered["niq"] = 99.0
        with pytest.raises(AssertionError, match="look-ahead detected"):
            pit.assert_no_lookahead(tampered, panel, "2022-01-15")

    def test_restating_history_does_not_change_history(self):
        """One-sidedness: a later revision must not move an earlier answer."""
        before = pit.pit_snapshot(restated_panel(), "2022-01-15")
        panel = restated_panel()
        panel.loc[len(panel)] = {
            "gvkey": 1004, "datadate": pd.Timestamp("2019-06-30"), "fq": 2,
            "rvq": pd.Timestamp("2030-01-15"), "niq": -1.0, "latest": "1",
        }
        after = pit.pit_snapshot(panel, "2022-01-15")
        pd.testing.assert_frame_equal(before, after)
        pit.assert_no_lookahead(after, panel, "2022-01-15")

    def test_snapshot_runs_its_internal_invariant_by_default(self):
        """`verify=True` is the default: a guard nobody calls is not a guard."""
        snap = pit.pit_snapshot(restated_panel(), "2022-01-15")
        cutoff = pd.Timestamp("2022-01-15") - pd.Timedelta(days=pit.FILING_LAG_DAYS)
        assert (pd.to_datetime(snap["rvq"]) <= cutoff).all()

    def test_internal_invariant_fires_on_a_leaky_selection(self):
        """`assert_versions_visible` needs only the result, so it can police
        anyone else's selection -- an adapter's output, a panel built elsewhere."""
        leaky = pit.latest_flag_snapshot(restated_panel())
        assert leaky["niq"].iloc[0] == 99.0, "precondition: latest=1 leaks"
        with pytest.raises(AssertionError, match="look-ahead detected"):
            pit.assert_versions_visible(leaky, "2022-01-15")

    def test_internal_invariant_passes_a_clean_snapshot(self):
        snap = pit.pit_snapshot(restated_panel(), "2022-01-15")
        pit.assert_versions_visible(snap, "2022-01-15")

    def test_internal_invariant_accepts_an_empty_snapshot(self):
        pit.assert_versions_visible(pd.DataFrame(), "2022-01-15")


class TestPITUniverse:
    @staticmethod
    def securities():
        return pd.DataFrame(
            {
                "gvkey": [1, 2, 3],
                "first_date": pd.to_datetime(["1990-01-01"] * 3),
                "last_date": pd.to_datetime(
                    ["2008-06-01", "2026-01-01", "2004-12-31"]
                ),
            }
        )

    def test_dead_firm_is_present_while_alive(self):
        """gvkey 1 delisted in 2008 and must be IN the 2005 cross-section."""
        universe = pit.pit_universe(self.securities(), "2005-01-01")
        assert 1 in set(universe)

    def test_dead_firm_is_gone_after_delisting(self):
        assert 1 not in set(pit.pit_universe(self.securities(), "2010-01-01"))

    def test_future_firm_is_absent_before_it_starts(self):
        universe = pit.pit_universe(self.securities(), "1985-01-01")
        assert universe.empty

    def test_universe_is_sorted_and_unique(self):
        universe = pit.pit_universe(self.securities(), "2005-01-01")
        assert universe.is_unique
        assert universe.is_monotonic_increasing


class TestBuildPanel:
    def test_wide_panel_is_gvkey_by_datadate(self):
        frame = pit.build_panel(restated_panel(), "2022-01-15", ["niq"])
        assert isinstance(frame.index, pd.MultiIndex)
        assert frame.index.names == ["gvkey", "datadate"]
        assert frame.loc[(1004, pd.Timestamp("2019-06-30")), "niq"] == 12.0

    def test_long_form_lists_only_requested_columns(self):
        out = pit.build_panel(restated_panel(), "2022-01-15", ["niq"], wide=False)
        assert set(out.columns) == {"gvkey", "datadate", "fq", "niq"}

    def test_absent_column_raises_rather_than_returning_empty(self):
        with pytest.raises(KeyError, match="absent from the pull"):
            pit.build_panel(restated_panel(), "2022-01-15", ["not_a_column"])


class TestDemo:
    def test_demo_runs(self, capsys):
        assert pit.main(["--demo"]) == 0
        assert "99.0" in capsys.readouterr().out