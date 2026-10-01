"""Tests for massive_option_listings_pipeline (no network)."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import massive_option_listings_pipeline as mol


def _c(ticker, strike, exp, typ="call", shares=100, extra=None):
    d = {"ticker": ticker, "strike_price": strike, "expiration_date": exp,
         "contract_type": typ, "shares_per_contract": shares}
    if extra:
        d["additional_underlyings"] = extra
    return d


def test_standard_frame_drops_adjusted_and_odd_size_contracts():
    rows = [
        _c("O:FDX261016C00195000", 195.0, "2026-10-16"),
        _c("O:FDX1261016C00195000", 195.0, "2026-10-16",
           extra=[{"type": "equity", "underlying": "FDXF", "amount": 50}]),
        _c("O:FDX261016P00195000", 195.0, "2026-10-16", typ="put", shares=150),
    ]
    df = mol.standard_frame(rows, "FDX")
    assert df["contract_ticker"].tolist() == ["O:FDX261016C00195000"]
    assert df["put_call"].tolist() == ["CALL"]


def test_live_on_excludes_expired():
    df = mol.standard_frame([_c("O:A260918C00100000", 100.0, "2026-09-18"),
                             _c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    assert mol.live_on(df, "2026-10-01")["expiration_date"].tolist() == ["2026-10-16"]


def test_first_day_is_initial_then_added_and_removed():
    d1 = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    d2 = mol.standard_frame([_c("O:A261016C00110000", 110.0, "2026-10-16")], "A")
    first = mol.diff_contracts(None, d1, "A", "2026-10-01")
    assert first["change"].tolist() == ["initial"]
    nxt = mol.diff_contracts(d1, d2, "A", "2026-10-02").sort_values("change")
    assert nxt[["contract_ticker", "change"]].values.tolist() == [
        ["O:A261016C00110000", "added"], ["O:A261016C00100000", "removed"]]
    assert list(nxt.columns) == mol.CHANGE_COLS


def test_expiry_shows_as_removed_not_as_a_listing():
    d1 = mol.standard_frame([_c("O:A261002C00100000", 100.0, "2026-10-02"),
                             _c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    d2 = mol.live_on(d1, "2026-10-05")  # next trading day after the 10-02 expiry
    out = mol.diff_contracts(d1, d2, "A", "2026-10-05")
    assert out[["contract_ticker", "change"]].values.tolist() == [
        ["O:A261002C00100000", "removed"]]


def test_summary_and_missing_status():
    d = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16"),
                            _c("O:A261120P00080000", 80.0, "2026-11-20", typ="put")], "A")
    s = mol.summarize(d, "A", "2026-10-01", "ok")
    assert (s["n_contracts"], s["n_expirations"], s["min_strike"], s["max_strike"]) == (2, 2, 80.0, 100.0)
    empty = mol.summarize(d.iloc[0:0], "A", "2026-10-02", "missing")
    assert empty["status"] == "missing" and empty["n_contracts"] == 0
    assert empty["min_strike"] is None


def test_replay_reconstructs_live_set():
    d1 = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    d2 = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16"),
                             _c("O:A261016C00110000", 110.0, "2026-10-16")], "A")
    d3 = mol.standard_frame([_c("O:A261016C00110000", 110.0, "2026-10-16")], "A")
    ch = pd.concat([mol.diff_contracts(None, d1, "A", "2026-10-01"),
                    mol.diff_contracts(d1, d2, "A", "2026-10-02"),
                    mol.diff_contracts(d2, d3, "A", "2026-10-05")])
    assert mol.replay_live_set(ch, "2026-10-02") == {"O:A261016C00100000", "O:A261016C00110000"}
    assert mol.replay_live_set(ch, "2026-10-05") == {"O:A261016C00110000"}


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body, str(body)[:100]

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0)


def _client(responses):
    s = FakeSession(responses)
    return mol.MassiveClient("KEY", session=s, interval=0, sleep=lambda x: None), s


def test_every_page_is_authenticated_and_all_pages_are_read(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", (None,))
    p1 = {"results": [_c("O:A261016C00100000", 100.0, "2026-10-16")],
          "next_url": "https://api.massive.com/v3/reference/options/contracts?cursor=abc"}
    p2 = {"results": [_c("O:A261016C00110000", 110.0, "2026-10-16")]}
    client, s = _client([_Resp(200, p1), _Resp(200, p2)])
    df = mol.fetch_contracts(client, "A", "2026-10-01")
    assert len(df) == 2
    assert all(params.get("apiKey") == "KEY" for _, params in s.calls)
    assert s.calls[1][0].endswith("cursor=abc")


def test_429_backs_off_then_succeeds(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", (None,))
    client, s = _client([_Resp(429, {}), _Resp(200, {"results": []})])
    assert mol.fetch_contracts(client, "A", "2026-10-01").empty
    assert len(s.calls) == 2


def test_persistent_failure_raises(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", (None,))
    client, _ = _client([_Resp(500, {})] * mol.MAX_RETRIES)
    import pytest
    with pytest.raises(RuntimeError):
        mol.fetch_contracts(client, "A", "2026-10-01")


def test_both_expired_queries_are_unioned_and_filtered_to_live(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", ("false", "true"))
    live = {"results": [_c("O:A261016C00100000", 100.0, "2026-10-16")]}
    gone = {"results": [_c("O:A261002C00100000", 100.0, "2026-10-02"),
                        _c("O:A260918C00100000", 100.0, "2026-09-18")]}
    client, s = _client([_Resp(200, live), _Resp(200, gone)])
    df = mol.fetch_contracts(client, "A", "2026-10-01")
    assert sorted(df["expiration_date"]) == ["2026-10-02", "2026-10-16"]
    assert [p.get("expired") for _, p in s.calls] == ["false", "true"]
