"""Tests for strategies/ledger.py -- unified paper-trade holdings ledger."""

import json

import numpy as np
import pandas as pd
import pytest

from strategies import ledger
from evaluation.contracts import TradeRule


def _tiny_state():
    return {"paper_start": "2026-09-10", "last_refresh": "2026-09-09",
            "live_signal_date": "2026-08-31", "pending_rebalance_signal_date": "2026-09-30",
            "construction": "long-only test construction", "n_active": 2}


def _bdate(n=8):
    return pd.bdate_range("2026-08-03", periods=n)


def _trivial_rule(n=8):
    def le(df):
        out = np.zeros(len(df), dtype=bool)
        out[1] = True
        return out

    def lx(df):
        return np.zeros(len(df), dtype=bool)

    def se(df):
        return np.zeros(len(df), dtype=bool)

    def sx(df):
        return np.zeros(len(df), dtype=bool)

    return TradeRule(name="test_union", side="both",
                     entries=le, exits=lx, short_entries=se, short_exits=sx)


class TestResolveCarry:
    def test_reads_book_state_and_weights(self, tmp_path):
        state = _tiny_state()
        with open(tmp_path / "book_state.json", "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        pd.Series({"CL": 0.12, "GC": 0.0, "6E": 0.08}).to_frame().to_parquet(
            tmp_path / "position_current.parquet")

        rows, meta = ledger.resolve_carry(paper_dir=str(tmp_path))

        assert [r["symbol"] for r in rows] == ["CL", "6E"]
        assert all(r["strategy"] == "carry_futures" for r in rows)
        assert all(r["side"] == "long" for r in rows)
        assert rows[0]["weight"] == 0.12
        assert rows[1]["weight"] == 0.08
        assert rows[0]["signal_date"] == "2026-08-31"
        assert rows[0]["entry_date"] is None
        assert meta["n"] == 2
        assert meta["data_edge"] == "2026-09-09"
        assert meta["pending_rebalance_signal_date"] == "2026-09-30"
        assert len(meta["source_files"]) == 2

    def test_missing_artifacts_return_empty_with_note(self, tmp_path):
        rows, meta = ledger.resolve_carry(paper_dir=str(tmp_path))
        assert rows == []
        assert meta["n"] == 0
        assert "not found" in meta["note"]

    def test_zero_weight_symbol_dropped(self, tmp_path):
        with open(tmp_path / "book_state.json", "w", encoding="utf-8") as fh:
            json.dump(_tiny_state(), fh)
        pd.Series({"CL": 0.0}).to_frame().to_parquet(tmp_path / "position_current.parquet")
        rows, meta = ledger.resolve_carry(paper_dir=str(tmp_path))
        assert rows == []
        assert meta["n"] == 0


class TestOpenPositionsFromFlags:
    def test_open_long_at_data_edge(self):
        idx = _bdate(8)
        close = pd.Series([100.0] * 8, index=idx)
        le = np.zeros(8, dtype=bool)
        le[1] = True
        hits = ledger._open_positions_from_flags(idx, close, le, np.zeros(8, bool),
                                                 np.zeros(8, bool), np.zeros(8, bool))
        assert len(hits) == 1
        assert hits[0]["side"] == "long"
        assert hits[0]["signal_date"] == "2026-08-04"
        assert hits[0]["entry_date"] == "2026-08-05"
        assert hits[0]["entry_price"] == 100.0

    def test_closed_trade_resumed_then_later_entry_still_open(self):
        idx = _bdate(10)
        close = pd.Series([100.0] * 10, index=idx)
        le = np.zeros(10, dtype=bool)
        le[2] = True
        le[8] = True
        lx = np.zeros(10, dtype=bool)
        lx[5] = True
        hits = ledger._open_positions_from_flags(idx, close, le, lx,
                                                 np.zeros(10, bool), np.zeros(10, bool))
        assert len(hits) == 1
        assert hits[0]["signal_date"] == "2026-08-13"
        assert hits[0]["entry_date"] == "2026-08-14"

    def test_entry_inside_open_window_is_consumed(self):
        idx = _bdate(10)
        close = pd.Series([100.0] * 10, index=idx)
        le = np.zeros(10, dtype=bool)
        le[2] = True
        le[4] = True
        lx = np.zeros(10, dtype=bool)
        lx[5] = True
        hits = ledger._open_positions_from_flags(idx, close, le, lx,
                                                 np.zeros(10, bool), np.zeros(10, bool))
        assert hits == []

    def test_all_trades_closed_returns_empty(self):
        idx = _bdate(8)
        close = pd.Series([100.0] * 8, index=idx)
        le = np.zeros(8, dtype=bool)
        le[1] = True
        lx = np.zeros(8, dtype=bool)
        lx[2] = True
        hits = ledger._open_positions_from_flags(idx, close, le, lx,
                                                 np.zeros(8, bool), np.zeros(8, bool))
        assert hits == []

    def test_last_bar_entry_open_without_pricing(self):
        idx = _bdate(8)
        close = pd.Series([100.0] * 8, index=idx)
        le = np.zeros(8, dtype=bool)
        le[7] = True
        hits = ledger._open_positions_from_flags(idx, close, le, np.zeros(8, bool),
                                                 np.zeros(8, bool), np.zeros(8, bool))
        assert len(hits) == 1
        assert hits[0]["entry_date"] is None
        assert hits[0]["entry_price"] is None


class TestResolveTv:
    def test_injected_cache_and_rule(self):
        idx = _bdate()
        cache = {"TEST": pd.DataFrame({"close": [100.0] * 8}, index=idx)}
        rows, meta = ledger.resolve_tv(cache=cache, rule=_trivial_rule(),
                                       slugs=["test_union"])
        assert len(rows) == 1
        assert rows[0]["strategy"] == "tv_survivor"
        assert rows[0]["symbol"] == "TEST"
        assert rows[0]["side"] == "long"
        assert rows[0]["weight"] is None
        assert rows[0]["signal_date"] == "2026-08-04"
        assert rows[0]["entry_date"] == "2026-08-05"
        assert meta["n"] == 1
        assert meta["slugs"] == ["test_union"]
        assert meta["data_edge"] == "2026-08-12"

    def test_no_slugs_returns_empty_with_note(self):
        rows, meta = ledger.resolve_tv(cache={}, slugs=[])
        assert rows == []
        assert meta["n"] == 0
        assert "no Stage 5 survivors" in meta["note"]

    def test_all_closed_returns_empty(self):
        idx = _bdate()
        le = np.zeros(8, dtype=bool)
        le[1] = True
        cache = {"TEST": pd.DataFrame({"close": [100.0] * 8}, index=idx)}

        def closed_exits(df):
            out = np.zeros(len(df), dtype=bool)
            out[2] = True
            return out

        rule = TradeRule(name="closure", side="both", entries=lambda df: le,
                         exits=closed_exits,
                         short_entries=lambda df: np.zeros(len(df), bool),
                         short_exits=lambda df: np.zeros(len(df), bool))
        rows, meta = ledger.resolve_tv(cache=cache, rule=rule, slugs=["closure"])
        assert rows == []
        assert meta["n"] == 0


class TestWriteSnapshot:
    def test_persists_parquet_and_state(self, tmp_path):
        df = pd.DataFrame([{"strategy": "carry_futures", "symbol": "CL",
                            "side": "long", "weight": 0.12, "signal_date": "2026-08-31",
                            "entry_date": None, "entry_price": None,
                            "source": "carry forward book"}],
                          columns=ledger.COLUMNS)
        meta = {"carry": {"n": 1, "data_edge": "2026-09-09", "note": "x", "source_files": []}}
        info = ledger.write_snapshot(df, meta, out_dir=str(tmp_path), run_ts="20260915T000000Z")

        assert info["parquet"] == str(tmp_path / "holdings_current.parquet")
        assert info["json"] == str(tmp_path / "ledger_state.json")
        back = pd.read_parquet(info["parquet"])
        assert list(back.columns) == ledger.COLUMNS
        assert len(back) == 1
        with open(info["json"], encoding="utf-8") as fh:
            state = json.load(fh)
        assert state["run_ts"] == "20260915T000000Z"
        assert state["rows"] == 1
        assert state["strategies"]["carry"]["n"] == 1