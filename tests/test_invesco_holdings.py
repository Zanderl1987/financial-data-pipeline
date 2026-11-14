"""Invesco dng-api: an unknown CUSIP comes back as HTTP 200 with an empty JSON
list, not an error. PDBC carried a wrong CUSIP (46138B707) and failed every run
with "'list' object has no attribute 'get'"."""

import sys
import types

import pytest

import invesco_holdings_pipeline as ivz


def _fake_curl(monkeypatch, payload):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    mod = types.ModuleType("curl_cffi")
    mod.requests = types.SimpleNamespace(get=lambda *a, **k: _Resp())
    monkeypatch.setitem(sys.modules, "curl_cffi", mod)


def test_empty_list_response_names_the_cusip(monkeypatch):
    _fake_curl(monkeypatch, [])
    with pytest.raises(ValueError, match="46138B707"):
        ivz.fetch_invesco_holdings("PDBC", "46138B707")


def test_pdbc_uses_its_real_cusip():
    assert ivz.INVESCO_FUNDS["PDBC"][0] == "46090F100"
