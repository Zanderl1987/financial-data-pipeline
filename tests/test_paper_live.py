import sys
import types
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from paper import live
from paper.accounts import ACCOUNTS
from paper.decide import Decision, OrderIntent

NY = ZoneInfo("America/New_York")
TODAY = date(2026, 11, 23)
SECRET = "PKTESTSECRET123"
EQUITY = 5123.45


def _intent(sym, acct="joey_5k"):
    return OrderIntent(sym, "buy", 10, "entry_long", f"pt|{acct}|20261123|entry_long|{sym}|20261120")


class FakeBroker:
    instances = []
    done_accounts = set()
    fail_accounts = set()
    reject_symbols = set()
    hour, minute = 10, 0
    open_day = True
    lots = []
    stop_accounts = set()
    clock_calls = 0
    late_after_first = False

    def __init__(self, key, secret):
        self.key, self.secret = key, secret
        self.submitted = []
        FakeBroker.instances.append(self)

    def clock(self):
        FakeBroker.clock_calls += 1
        if FakeBroker.late_after_first and FakeBroker.clock_calls > 1:
            return datetime(2026, 11, 23, 15, 41, tzinfo=NY), self.open_day
        return datetime(2026, 11, 23, self.hour, self.minute, tzinfo=NY), self.open_day

    def close_et(self, d):
        return datetime(2026, 11, 23, 16, 0, tzinfo=NY)

    def prev_trading_day(self, d):
        return date(2026, 11, 20)

    def orders_today(self, prefix):
        if self.key in FakeBroker.done_accounts:
            return [prefix + "|x"]
        return []

    def fee_inputs(self):
        if self.key in FakeBroker.fail_accounts:
            raise RuntimeError("boom " + SECRET)
        return EQUITY, list(FakeBroker.lots)

    def held(self):
        return []

    def recently_exited(self, prev):
        return set()

    def stop_dates(self):
        return [date(2026, 11, 20)] if self.key in FakeBroker.stop_accounts else []

    def shortable(self, s):
        return True

    def submit(self, intent):
        if intent.symbol in FakeBroker.reject_symbols:
            raise ValueError("rejected")
        self.submitted.append(intent)
        return "id"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FakeBroker.instances = []
    FakeBroker.done_accounts = set()
    FakeBroker.fail_accounts = set()
    FakeBroker.reject_symbols = set()
    FakeBroker.hour, FakeBroker.minute, FakeBroker.open_day = 10, 0, True
    FakeBroker.lots = []
    FakeBroker.stop_accounts = set()
    FakeBroker.clock_calls = 0
    FakeBroker.late_after_first = False
    for a in ACCOUNTS:
        monkeypatch.setenv(a.env_key, SECRET)
        monkeypatch.setenv(a.env_secret, SECRET)
    monkeypatch.setattr(live, "decide_orders",
                        lambda acct, *a, **k: Decision(
                            orders=[_intent("AAA", acct.key), _intent("BBB", acct.key)],
                            notes=["n1"]))


class Rec:
    def __init__(self):
        self.records = []

    def __call__(self, record, repo=None, token=None, path=None):
        self.records.append(record)


class Fetch:
    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return {}


def _run(**kw):
    log, fetch = Rec(), Fetch()
    rc = live.run(today=TODAY, broker_factory=FakeBroker, log=log, fetch=fetch, **kw)
    return rc, log, fetch


def test_second_run_same_day_is_noop():
    FakeBroker.done_accounts = {SECRET}
    rc, log, _ = _run()
    assert rc == 0
    assert all(not b.submitted for b in FakeBroker.instances)
    assert log.records == []


def test_outside_window_does_nothing():
    FakeBroker.hour, FakeBroker.minute = 15, 45
    rc, log, fetch = _run()
    assert rc == 0 and fetch.calls == 0 and log.records == []
    assert all(not b.submitted for b in FakeBroker.instances)


def test_non_trading_day_does_nothing():
    FakeBroker.open_day = False
    rc, log, fetch = _run()
    assert rc == 0 and fetch.calls == 0 and log.records == []


def test_one_account_error_others_continue(monkeypatch):
    for a in ACCOUNTS:
        monkeypatch.setenv(a.env_key, a.key)
    FakeBroker.fail_accounts = {ACCOUNTS[1].key}
    rc, log, _ = _run()
    assert rc == 1
    assert sum(1 for b in FakeBroker.instances if b.submitted) == 5
    errs = [r for r in log.records if r["status"] == "error"]
    assert len(errs) == 1 and errs[0]["account"] == ACCOUNTS[1].key
    assert errs[0]["notes"] == ["RuntimeError"]  # type name only, no message


def test_rejected_order_continues():
    FakeBroker.reject_symbols = {"AAA"}
    rc, log, _ = _run()
    assert rc == 0
    assert all([i.symbol for i in b.submitted] == ["BBB"]
               for b in FakeBroker.instances if b.submitted)
    assert any(n.startswith("submit_failed:AAA") for n in log.records[0]["notes"])
    assert log.records[0]["n_orders"] == 1


def test_dry_run_submits_nothing():
    rc, log, _ = _run(dry_run=True)
    assert rc == 0
    assert all(not b.submitted for b in FakeBroker.instances)
    assert {r["status"] for r in log.records} == {"dry_run"}
    assert len(log.records) == 6


def test_record_fields_exact():
    _, log, _ = _run(dry_run=True)
    assert set(log.records[0]) == {"date", "account", "status", "n_orders", "notes", "run_id"}


def test_stdout_has_no_secrets(capsys, monkeypatch):
    FakeBroker.fail_accounts = {SECRET}
    _run()
    out = capsys.readouterr().out
    assert SECRET not in out and "5123.45" not in out
    assert len([l for l in out.splitlines() if l]) == 6


def test_broker_is_paper_only(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, key, secret, paper=False, **kw):
            seen.update(key=key, secret=secret, paper=paper, kw=kw)

    mod = types.ModuleType("alpaca.trading.client")
    mod.TradingClient = FakeClient
    monkeypatch.setitem(sys.modules, "alpaca.trading.client", mod)
    from paper.broker import Broker
    Broker("k", "s")
    assert seen["paper"] is True and not seen["kw"]


def test_broker_rejects_no_endpoint_param():
    import inspect
    from paper.broker import Broker
    assert list(inspect.signature(Broker.__init__).parameters) == ["self", "key", "secret"]


def test_workflow_live_gate():
    from pathlib import Path
    t = Path(".github/workflows/paper_trade.yml").read_text()
    assert "vars.PAPER_LIVE" in t and "cancel-in-progress: false" in t
    assert 'cron: "0 14-21 * * 1-5"' in t
    assert t.count("secrets.APCA_") == 12


@pytest.mark.parametrize("step", ["fetch", "prev"])
def test_shared_step_failure_is_contained(capsys, monkeypatch, step):
    log = Rec()

    def boom(*a, **k):
        raise RuntimeError("token=SECRET123")

    if step == "prev":
        monkeypatch.setattr(FakeBroker, "prev_trading_day", boom)
        fetch = Fetch()
    else:
        fetch = boom
    rc = live.run(today=TODAY, broker_factory=FakeBroker, log=log, fetch=fetch)
    cap = capsys.readouterr()
    assert rc == 1
    assert "SECRET123" not in cap.out + cap.err
    lines = [l for l in cap.out.splitlines() if l]
    assert lines == [f"{a.key}: error, 0 orders" for a in ACCOUNTS]
    assert len(log.records) == 6
    assert all(r["status"] == "error" and r["notes"][0].endswith(":RuntimeError")
               for r in log.records)


def test_fee_adjusted_equity_with_symbol_lots(monkeypatch):
    from datetime import timedelta
    FakeBroker.lots = [("SSS", 36500.0, TODAY - timedelta(days=10), None)]
    seen = []

    def spy(acct, *a, **k):
        seen.append(a[5])  # fee_adj positional arg
        return Decision(orders=[], notes=[])
    monkeypatch.setattr(live, "decide_orders", spy)
    rc, log, _ = _run()
    assert rc == 0 and len(seen) == 6
    # 36500 * 270bps/yr * 10/365 = 27.0
    assert seen[0] == pytest.approx(EQUITY - 27.0)


def test_stop_latch_comes_from_order_history(monkeypatch):
    latched = ACCOUNTS[2]
    FakeBroker.stop_accounts = {latched.key}
    for a in ACCOUNTS:
        monkeypatch.setenv(a.env_key, a.key)
    seen = {}

    def spy(acct, *a, **k):
        seen[acct.key] = k.get("stop_latched")
        return Decision(orders=[], notes=[])
    monkeypatch.setattr(live, "decide_orders", spy)
    rc, _, _ = _run()
    assert rc == 0
    assert seen == {a.key: a.key == latched.key for a in ACCOUNTS}


def test_past_cutoff_before_submit_places_nothing(capsys):
    """Fetch finished after the cls cutoff: nothing is submitted and the day is
    recorded as missed_cutoff, not as a quiet traded day."""
    FakeBroker.late_after_first = True
    rc, log, _ = _run()
    assert rc == 1
    assert all(not b.submitted for b in FakeBroker.instances)
    assert {r["status"] for r in log.records} == {"missed_cutoff"}
    assert all(r["n_orders"] == 0 for r in log.records)
    assert "joey_5k: missed_cutoff, 0 orders" in capsys.readouterr().out


def test_cutoff_recheck_uses_close_minus_20_on_half_days(monkeypatch):
    # 13:00 close: cutoff 12:40; a 12:45 re-check is too late
    monkeypatch.setattr(FakeBroker, "close_et",
                        lambda self, d: datetime(2026, 11, 23, 13, 0, tzinfo=NY))
    calls = iter([datetime(2026, 11, 23, 10, 0, tzinfo=NY)]
                 + [datetime(2026, 11, 23, 12, 45, tzinfo=NY)] * 20)
    monkeypatch.setattr(FakeBroker, "clock", lambda self: (next(calls), True))
    rc, log, _ = _run()
    assert {r["status"] for r in log.records} == {"missed_cutoff"}


def test_every_submit_failing_is_an_error():
    FakeBroker.reject_symbols = {"AAA", "BBB"}
    rc, log, _ = _run()
    assert rc == 1
    assert {r["status"] for r in log.records} == {"error"}
    assert all(r["n_orders"] == 0 for r in log.records)
    assert all(sum(n.startswith("submit_failed:") for n in r["notes"]) == 2
               for r in log.records)


def test_no_orders_needs_no_clock_recheck(monkeypatch):
    FakeBroker.late_after_first = True
    monkeypatch.setattr(live, "decide_orders",
                        lambda acct, *a, **k: Decision(orders=[], notes=[]))
    rc, log, _ = _run()
    assert rc == 0 and {r["status"] for r in log.records} == {"ok"}
