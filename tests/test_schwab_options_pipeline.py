"""
Fetch logic for schwab_options_pipeline.py against a fake Schwab client.

Full chains (widened 2026-10-01) exceed Schwab's response-size cap for SPY/QQQ
-- it answers 502 "Body buffer overflow" -- so the pipeline bisects the
expiration-date range until each window fits. No network or credentials needed.
"""

import datetime
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

pytest.importorskip("schwabdev")
os.environ.setdefault("SCHWAB_API_KEY", "x")
os.environ.setdefault("SCHWAB_APP_SECRET", "x")
import schwab_options_pipeline as sop  # noqa: E402


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        return self._body


def _contract(exp: str, strike: float, sym: str):
    return {"putCall": "CALL", "symbol": sym, "nonStandard": sym[3] != " ", "mark": 1.0}


class FakeClient:
    """Serves a chain of expirations; any window spanning more than
    `max_exps` expirations is rejected the way Schwab rejects big bodies."""

    def __init__(self, expirations, max_exps):
        self.expirations = expirations
        self.max_exps = max_exps
        self.calls = []

    def option_chains(self, symbol, fromDate=None, toDate=None, **kw):
        self.calls.append((fromDate, toDate))
        assert kw.get("range") == "ALL" and "strikeCount" not in kw
        exps = [e for e in self.expirations
                if (fromDate is None or e >= fromDate) and (toDate is None or e <= toDate)]
        if len(exps) > self.max_exps:
            return _Resp(502, text='{"fault":{"detail":{"errorcode":"protocol.http.TooBigBody"}}}')
        calls = {f"{e}:1": {"100.0": [_contract(e, 100.0, f"SPY   {e}")]} for e in exps}
        return _Resp(200, {"status": "SUCCESS", "underlyingPrice": 500.0,
                           "callExpDateMap": calls, "putExpDateMap": {}})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sop.time, "sleep", lambda s: None)


def _exps(n):
    today = datetime.date.today()
    return [(today + datetime.timedelta(days=30 * i)).isoformat() for i in range(n)]


def test_small_chain_is_one_unbounded_request():
    client = FakeClient(_exps(5), max_exps=10)
    df = sop.fetch_option_chain(client, "KO")
    assert client.calls == [(None, None)]
    assert df["expiration_date"].nunique() == 5


def test_oversized_chain_is_bisected_without_gaps_or_duplicates():
    exps = _exps(30)
    client = FakeClient(exps, max_exps=4)
    df = sop.fetch_option_chain(client, "SPY")
    assert sorted(df["expiration_date"]) == sorted(exps)
    assert len(client.calls) > 2


def test_a_failed_window_fails_the_whole_symbol():
    # A partial chain would look like strikes were delisted -- the exact
    # signal this table is being collected for -- so it must not be written.
    client = FakeClient(_exps(30), max_exps=4)
    real = client.option_chains

    def flaky(symbol, fromDate=None, toDate=None, **kw):
        if fromDate is not None and fromDate > _exps(20)[-1]:
            return _Resp(500, text="boom")
        return real(symbol, fromDate=fromDate, toDate=toDate, **kw)

    client.option_chains = flaky
    assert sop.fetch_option_chain(client, "SPY") is None


def test_contract_symbol_and_non_standard_are_captured():
    rows = sop._flatten_contracts(
        {"2026-10-16:15": {"195.0": [
            {"putCall": "CALL", "symbol": "FDX   261016C00195000", "nonStandard": False},
            {"putCall": "CALL", "symbol": "FDX1  261016C00195000", "nonStandard": True},
        ]}}, "FDX", 250.0)
    assert [r["contract_symbol"] for r in rows] == ["FDX   261016C00195000", "FDX1  261016C00195000"]
    assert [r["non_standard"] for r in rows] == [False, True]
