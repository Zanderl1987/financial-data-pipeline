"""
test_backadjust.py — backadjust.py classification + resumable no-reference sweep.

No network: yfinance is stubbed (`_yf_history_detailed`), the store is stubbed
(`q.sql`). Dates are strings on both sides, matching what the real store and
yfinance fixture actually produce.
"""

import glob

import numpy as np
import pandas as pd
import pytest

import backadjust as ba


def _str_dates(n=80):
    return pd.bdate_range("2023-01-02", periods=n).strftime("%Y-%m-%d").tolist()


def _ours(closes=100.0, n=10):
    return pd.DataFrame({"date": _str_dates(n), "close": [closes] * n})


class TestClassifyOne:
    def test_no_history_empty_reference(self):
        r = ba.classify_one("AAA", _ours(), pd.DataFrame())
        assert r["status"] == "no_history"

    def test_no_compare_empty_ours(self):
        r = ba.classify_one("AAA", pd.DataFrame(),
                            pd.DataFrame({"date": _str_dates(3),
                                          "ref_close": [1.0] * 3}))
        assert r["status"] == "no_compare"

    def test_clean_tiny_noise(self):
        ours = _ours()
        ref = pd.DataFrame({"date": ours["date"],
                            "ref_close": ours["close"] + 0.005})
        r = ba.classify_one("AAA", ours, ref)
        assert r["status"] == "clean"

    def test_corrected_constant_offset(self):
        ours = _ours()
        ref = pd.DataFrame({"date": ours["date"],
                            "ref_close": ours["close"] + 39.0})
        r = ba.classify_one("COST", ours, ref)
        assert r["status"] == "corrected"
        assert len(r["steps"]) == 1
        assert r["steps"]["offset"].iloc[0] == pytest.approx(39.0)
        assert r["steps"]["symbol"].iloc[0] == "COST"

    def test_rejected_non_piecewise(self):
        ours = _ours(n=80)
        ratio = np.linspace(1.0, 2.0, 80)
        ref = pd.DataFrame({"date": ours["date"],
                            "ref_close": ours["close"] * ratio})
        r = ba.classify_one("AAA", ours, ref)
        assert r["status"] == "rejected"
        assert r["n_steps"] > ba.MAX_STEPS
        assert r["median_offset"] is not None
        assert r["median_ratio"] is not None


class TestMergeOffsets:
    def test_new_when_existing_empty_gets_fetched_at(self):
        steps = pd.DataFrame({"symbol": ["FIXME"], "start_date": ["2023-01-02"],
                              "end_date": ["2024-01-01"], "offset": [39.0],
                              "n_days": [80]})
        out = ba._merge_offsets(pd.DataFrame(), steps)
        assert len(out) == 1
        assert "fetched_at" in out.columns

    def test_dedupe_keeps_newest_by_fetched_at(self):
        older = pd.DataFrame({"symbol": ["FIXME"], "start_date": ["2023-01-02"],
                              "end_date": ["2024-01-01"], "offset": [39.0],
                              "n_days": [80],
                              "fetched_at": ["2026-09-01T00:00:00"]})
        newer = pd.DataFrame({"symbol": ["FIXME"], "start_date": ["2023-01-02"],
                              "end_date": ["2024-01-01"], "offset": [40.0],
                              "n_days": [80],
                              "fetched_at": ["2026-09-11T00:00:00"]})
        out = ba._merge_offsets(older, newer)
        assert len(out) == 1
        assert out["offset"].iloc[0] == pytest.approx(40.0)

    def test_empty_new_is_noop(self):
        existing = pd.DataFrame({"symbol": ["X"], "start_date": ["2023-01-01"],
                                 "end_date": ["2024-01-01"], "offset": [1.0],
                                 "fetched_at": ["2026-09-01T00:00:00"]})
        assert ba._merge_offsets(existing, pd.DataFrame()).equals(existing)


class TestSweepProgress:
    def test_missing_progress_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ba, "_SWEEP_PROGRESS",
                            str(tmp_path / "none_progress.csv"))
        assert ba._load_sweep_progress() == {}

    def test_stray_header_rows_are_dropped(self, tmp_path, monkeypatch):
        p = tmp_path / "progress.csv"
        rows = ["symbol,status,fetched_at",
                "AAA,clean,2026-09-11T00:00:00",
                "symbol,status,fetched_at",          # stray duplicate header
                "BBB,error,2026-09-11T00:00:01"]
        p.write_text("\n".join(rows), encoding="utf-8")
        monkeypatch.setattr(ba, "_SWEEP_PROGRESS", str(p))
        assert ba._load_sweep_progress() == {"AAA": "clean", "BBB": "error"}


class TestSweepNoReference:
    """End-to-end sweep mechanics with yfinance + DuckDB stubbed out."""

    @pytest.fixture
    def patched(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ba, "OUT_DIR", str(tmp_path))
        monkeypatch.setattr(ba, "_SWEEP_PROGRESS",
                            str(tmp_path / "progress.csv"))
        monkeypatch.setattr(ba, "_SWEEP_WORK", str(tmp_path / "work.parquet"))
        monkeypatch.setattr(ba, "REQUEST_PAUSE", 0.0)
        monkeypatch.setattr(ba, "no_reference_symbols",
                            lambda: ["CLEAN", "FIXME", "DEAD", "THROTTLED"])

        state = {"throttled": 0}

        def fake_store(sql, **kw):
            import re
            m = re.search(r"symbol = '(.+?)'", sql)
            return _ours() if m else pd.DataFrame()

        def fake_history(symbol):
            def ref_like(ours):
                return pd.DataFrame({"date": ours["date"],
                                     "ref_close": ours["close"].to_numpy()})

            if symbol == "CLEAN":
                return ref_like(_ours()), None
            if symbol == "FIXME":
                ours = _ours()
                return pd.DataFrame({"date": ours["date"],
                                     "ref_close": ours["close"] + 39.0}), None
            if symbol == "DEAD":
                return pd.DataFrame(), "empty"
            if symbol == "THROTTLED":
                state["throttled"] += 1
                if state["throttled"] == 1:
                    return pd.DataFrame(), "HTTP 429 Too Many Requests"
                return ref_like(_ours()), None
            return pd.DataFrame(), "empty"

        monkeypatch.setattr(ba, "_yf_history_detailed", fake_history)
        monkeypatch.setattr(ba.q, "sql", fake_store, raising=False)
        return tmp_path

    def test_resume_retries_errors_and_is_idempotent(self, patched):
        n1 = ba.sweep_no_reference(verbose=False, checkpoint_every=2)
        assert n1 == 1                        # FIXME corrected, THROTTLED errored

        n2 = ba.sweep_no_reference(verbose=False, checkpoint_every=2)
        assert n2 == 0                        # only THROTTLED retried, now clean

        n3 = ba.sweep_no_reference(verbose=False)
        assert n3 == 0                        # everything terminal already

        prog = ba._load_sweep_progress()
        assert prog["FIXME"] == "corrected"
        assert prog["THROTTLED"] == "clean"
        assert len(prog) == 4

        work = ba._load_sweep_work()
        assert not work.empty
        assert set(work["symbol"]) == {"FIXME"}

        dated = glob.glob(str(patched) + "/**/*parquet", recursive=True)
        dated = [f for f in dated if "price_backadjust_" in f]
        assert dated, "no dated offset file written on completion"

    def test_rejected_is_unioned_not_clobbered(self, monkeypatch, tmp_path):
        # Prior rejected verdicts (with stats columns) must survive a run that
        # also adds a new rejected symbol.
        rej = tmp_path / "REJECTED_not_piecewise_constant.csv"
        rej.write_text("symbol,n_steps,n_compared,median_offset,median_ratio\n"
                       "OLD,300,5000,-12.5,1.0001\n", encoding="utf-8")

        monkeypatch.setattr(ba, "OUT_DIR", str(tmp_path))
        monkeypatch.setattr(ba, "_SWEEP_PROGRESS",
                            str(tmp_path / "progress2.csv"))
        monkeypatch.setattr(ba, "_SWEEP_WORK", str(tmp_path / "work2.parquet"))
        monkeypatch.setattr(ba, "REQUEST_PAUSE", 0.0)
        monkeypatch.setattr(ba, "no_reference_symbols", lambda: ["RATIO"])

        def fake_store(sql, **kw):
            return _ours(n=80)

        def fake_history(symbol):
            ours = _ours(n=80)
            ratio = np.linspace(1.0, 2.0, 80)
            return pd.DataFrame({"date": ours["date"],
                                 "ref_close": ours["close"] * ratio}), None

        monkeypatch.setattr(ba, "_yf_history_detailed", fake_history)
        monkeypatch.setattr(ba.q, "sql", fake_store, raising=False)
        ba.sweep_no_reference(verbose=False)

        out = pd.read_csv(rej)
        assert set(out["symbol"].astype(str)) == {"OLD", "RATIO"}
        old = out[out["symbol"].astype(str) == "OLD"]
        assert old["median_offset"].iloc[0] == pytest.approx(-12.5)