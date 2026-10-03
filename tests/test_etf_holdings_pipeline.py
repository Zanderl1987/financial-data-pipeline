"""
etf_holdings_pipeline resilience (2026-10-03).

securitiesdb.com went down 2026-10-02 (Cloudflare 522 on every request, ~20 s
each), so the run spent its whole 30-min timeout failing and was killed -- and
because all data is written once at the end, a timeout also discards every
fund already fetched. Healthy runs took 25-30 min against that same limit.
No network: fetch_etf_holdings is replaced with fakes.
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import etf_holdings_pipeline as ehp


def _df(t):
    return pd.DataFrame({"fund_ticker": [t], "holding_ticker": ["AAPL"]})


class Clock:
    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ehp.time, "sleep", lambda s: None)


def test_consecutive_upstream_errors_stop_the_run_early(monkeypatch):
    calls = []

    def down(ticker, name):
        calls.append(ticker)
        raise ehp.UpstreamError(f"{ticker}: HTTP 522")
    monkeypatch.setattr(ehp, "fetch_etf_holdings", down)
    frames, stop = ehp.fetch_all_etf_holdings(only_tickers=[f"E{i}" for i in range(50)])
    assert stop == "upstream_down"
    assert len(calls) == ehp.MAX_CONSECUTIVE_UPSTREAM_ERRORS
    assert frames == []


def test_a_success_resets_the_upstream_error_count(monkeypatch):
    seq = iter([False, False, False, True] * 20)   # 3 errors then a success, repeating

    def flaky(ticker, name):
        if next(seq):
            return _df(ticker)
        raise ehp.UpstreamError("522")
    monkeypatch.setattr(ehp, "fetch_etf_holdings", flaky)
    frames, stop = ehp.fetch_all_etf_holdings(only_tickers=[f"E{i}" for i in range(40)])
    assert stop is None and len(frames) == 10


def test_time_budget_stops_fetching_but_keeps_what_was_fetched(monkeypatch):
    monkeypatch.setattr(ehp, "fetch_etf_holdings", lambda t, n: _df(t))
    frames, stop = ehp.fetch_all_etf_holdings(only_tickers=[f"E{i}" for i in range(100)],
                                              budget_seconds=10, clock=Clock(1.0))
    assert stop == "time_budget"
    assert 0 < len(frames) < 100


def test_main_writes_partial_data_and_exits_nonzero_when_upstream_is_down(monkeypatch):
    written = []
    seq = iter([True, True] + [False] * 100)

    def mostly_down(ticker, name):
        if next(seq):
            return _df(ticker)
        raise ehp.UpstreamError("522")
    monkeypatch.setattr(ehp, "fetch_etf_holdings", mostly_down)
    monkeypatch.setattr(ehp, "write_to_iceberg", lambda frames: written.append(len(frames)))
    with pytest.raises(SystemExit) as e:
        ehp.main(etf_tickers=[f"E{i}" for i in range(30)])
    assert e.value.code == 2
    assert written == [2]              # the two good funds are kept, in one write


def test_main_exits_nonzero_when_nothing_was_fetched(monkeypatch):
    def down(ticker, name):
        raise ehp.UpstreamError("522")
    monkeypatch.setattr(ehp, "fetch_etf_holdings", down)
    monkeypatch.setattr(ehp, "write_to_iceberg", lambda frames: pytest.fail("no write expected"))
    with pytest.raises(SystemExit) as e:
        ehp.main(etf_tickers=["SPY", "IVV", "VOO", "QQQ", "VTI", "DIA"])
    assert e.value.code == 2


def test_upstream_errors_are_5xx_and_connection_failures_not_404(monkeypatch):
    class R:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise ehp.requests.exceptions.HTTPError(f"{self.status_code}")

        def json(self):
            return {}
    monkeypatch.setattr(ehp.requests, "get", lambda *a, **k: R(522))
    with pytest.raises(ehp.UpstreamError):
        ehp.fetch_etf_holdings("SPY", "x")
    monkeypatch.setattr(ehp.requests, "get", lambda *a, **k: R(404))
    assert ehp.fetch_etf_holdings("ZZZ", "x") is None

    def boom(*a, **k):
        raise ehp.requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(ehp.requests, "get", boom)
    with pytest.raises(ehp.UpstreamError):
        ehp.fetch_etf_holdings("SPY", "x")
