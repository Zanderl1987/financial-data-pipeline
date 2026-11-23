"""Run log: one file per New York month, and never overwrite a file whose
content the API did not return (GitHub's contents API omits content > 1 MB)."""
import base64
import json
from datetime import date

import pytest

from paper import dashboard_data as dd
from paper import runlog

REC = {"date": "2026-11-23", "account": "joey_5k", "status": "ok", "n_orders": 2,
       "notes": ["n1"], "run_id": "42", "equity": 5123.45}


class Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeAPI:
    def __init__(self, get_resp):
        self.get_resp, self.gets, self.puts = get_resp, [], []

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        return self.get_resp

    def put(self, url, headers=None, json=None, timeout=None):
        self.puts.append((url, json))
        return Resp(201)


@pytest.fixture
def api(monkeypatch):
    def make(get_resp):
        a = FakeAPI(get_resp)
        monkeypatch.setattr(runlog.requests, "get", a.get)
        monkeypatch.setattr(runlog.requests, "put", a.put)
        return a
    return make


def _b64(s):
    return base64.b64encode(s.encode()).decode()


def _written(a):
    return base64.b64decode(a.puts[0][1]["content"]).decode()


def test_new_month_file_created(api):
    a = api(Resp(404))
    runlog.append_run(REC, "me/log", "tok")
    url, body = a.puts[0]
    assert url.endswith("/repos/me/log/contents/runs/2026-11.jsonl")
    assert "sha" not in body
    line = json.loads(_written(a))
    assert set(line) == {"date", "account", "status", "n_orders", "notes", "run_id"}


def test_month_follows_record_date(api):
    a = api(Resp(404))
    runlog.append_run({**REC, "date": "2026-12-01"}, "me/log", "tok")
    assert a.puts[0][0].endswith("/contents/runs/2026-12.jsonl")


def test_appends_to_existing(api):
    old = '{"date":"2026-11-20"}\n'
    a = api(Resp(200, {"sha": "abc", "size": len(old), "encoding": "base64",
                       "content": _b64(old)}))
    runlog.append_run(REC, "me/log", "tok")
    assert a.puts[0][1]["sha"] == "abc"
    text = _written(a)
    assert text.startswith(old) and text.count("\n") == 2


@pytest.mark.parametrize("body", [
    {"sha": "abc", "size": 1_200_000, "encoding": "none", "content": ""},  # > 1 MB
    {"sha": "abc", "size": 500, "encoding": "base64", "content": ""},
    {"sha": "abc", "size": 500},                                           # no content
])
def test_refuses_to_overwrite_when_content_missing(api, body):
    a = api(Resp(200, body))
    with pytest.raises(RuntimeError):
        runlog.append_run(REC, "me/log", "tok")
    assert a.puts == []


def test_empty_existing_file_is_fine(api):
    a = api(Resp(200, {"sha": "abc", "size": 0, "encoding": "base64", "content": ""}))
    runlog.append_run(REC, "me/log", "tok")
    assert _written(a).count("\n") == 1


def test_dashboard_reads_months_since_launch():
    assert dd.log_months(date(2026, 11, 24), date(2027, 2, 3)) == [
        "2026-11", "2026-12", "2027-01", "2027-02"]
    assert dd.log_months(date(2026, 11, 24), date(2026, 11, 30)) == ["2026-11"]
    assert dd.log_path("2026-11") == "runs/2026-11.jsonl" == \
        runlog.log_path("2026-11-23")
