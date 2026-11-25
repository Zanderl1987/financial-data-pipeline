"""paper.key_check against fake brokers (no network). Also: its stdout never
carries equity, account numbers, order ids or keys (the repo is public)."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

import alpaca.trading.client as alpaca_client
from paper import key_check as kc
from paper.accounts import ACCOUNTS
from paper.broker import NY, Broker

NOW = datetime(2026, 11, 25, 11, 0, tzinfo=NY)          # a Wednesday, mid-morning


def weekdays(start, end):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class FakeBroker:
    def __init__(self, key, secret, *, equity, number, positions=0, open_orders=0,
                 status="ACTIVE", zero_day=False, cancel_works=True, submit_error=None):
        self.key, self.equity, self.number = key, equity, number
        self._pos, self._open, self.status = positions, open_orders, status
        self.zero_day, self.cancel_works, self.submit_error = zero_day, cancel_works, submit_error
        self.submitted, self.cancelled, self._orders = [], [], {}

    def account(self):
        return SimpleNamespace(status=SimpleNamespace(value=self.status),
                               equity=str(self.equity), account_number=self.number)

    def positions(self):
        return [object()] * self._pos

    def open_orders(self):
        return [object()] * self._open

    def calendar_days(self, start, end):
        return weekdays(start, end)

    def portfolio_history(self, start, end=None):
        days = [d for d in weekdays(start, NOW.date() - timedelta(days=1))]
        ts = [int(datetime(d.year, d.month, d.day, tzinfo=NY).timestamp()) for d in days]
        eq = [self.equity] * len(ts)
        if self.zero_day:
            eq[0] = 0.0
        return SimpleNamespace(timestamp=ts, equity=eq)

    def submit(self, intent):
        if self.submit_error:
            raise self.submit_error
        self.submitted.append(intent)
        oid = f"oid-{len(self.submitted)}"
        self._orders[oid] = SimpleNamespace(
            status=SimpleNamespace(value="accepted"), time_in_force=SimpleNamespace(value="cls"),
            client_order_id=intent.client_order_id)
        return oid

    def get_order(self, oid):
        return self._orders[oid]

    def orders_today(self, prefix):
        return [o.client_order_id for o in self._orders.values()
                if o.client_order_id.startswith(prefix)]

    def cancel(self, oid):
        self.cancelled.append(oid)
        if self.cancel_works:
            self._orders[oid].status = SimpleNamespace(value="canceled")


def env_all(**override):
    env = {"PAPER_LOG_REPO": "owner/log", "PAPER_LOG_TOKEN": "tok-SECRET", "GITHUB_RUN_ID": "1"}
    for s in ACCOUNTS:
        env[s.env_key] = f"PK{s.key.upper()}KEYVALUE"
        env[s.env_secret] = f"SECRETVALUE{s.key}"
    env.update(override)
    return env


def factory(overrides=None, made=None):
    """Brokers keyed by API key; each account gets its planned equity and a
    distinct account number unless overridden."""
    by_key = {f"PK{s.key.upper()}KEYVALUE": s for s in ACCOUNTS}
    overrides = overrides or {}

    def make(key, secret):
        spec = by_key.get(key)
        kw = dict(equity=spec.start_equity if spec else 5000.0,
                  number=f"PA{spec.key if spec else key}9999")
        kw.update(overrides.get(spec.key if spec else key, {}))
        b = FakeBroker(key, secret, **kw)
        if made is not None:
            made.append(b)
        return b
    return make


def run(capsys, env=None, overrides=None, made=None, now=NOW, log=None):
    logged = []
    code = kc.run(broker_factory=factory(overrides, made), env=env or env_all(),
                  log=log or (lambda rec, repo, tok, path=None: logged.append((rec, path))),
                  now_et=now)
    return code, capsys.readouterr().out, logged


def test_all_good_passes_and_prints_nothing_sensitive(capsys):
    made = []
    code, out, logged = run(capsys, made=made)
    assert code == 0 and "KEY CHECK PASS" in out
    assert "distinct accounts: yes" in out
    assert "cancelled=yes" in out and "client_id_round_trip=yes" in out
    assert "labels_on_trading_days=yes" in out and "label_hours_et=0" in out
    for bad in ("KEYVALUE", "SECRETVALUE", "tok-SECRET", "9999", "5000", "10000", "oid-"):
        assert bad not in out
    # one order, on the first account only, outside the job's id namespace, then cancelled
    first = made[0]
    assert len(first.submitted) == 1 and first.cancelled == ["oid-1"]
    o = first.submitted[0]
    assert (o.symbol, o.side, o.qty) == ("SPY", "buy", 1)
    assert o.client_order_id.startswith("keycheck|") and not o.client_order_id.startswith("pt|")
    assert all(not b.submitted for b in made[1:])
    assert logged and logged[0][1] == "checks/key_check.jsonl"
    assert logged[0][0]["status"] == "key_check"


def test_default_100k_balance_fails_start_equity(capsys):
    code, out, _ = run(capsys, overrides={"cap_5k": {"equity": 100000.0}})
    assert code == 1 and "KEY CHECK FAIL" in out
    line = [l for l in out.splitlines() if l.startswith("cap_5k:")][0]
    assert "start_equity=NO" in line and "100000" not in out


def test_5k_and_10k_swapped_fails(capsys):
    code, out, _ = run(capsys, overrides={"joey_5k": {"equity": 10000.0},
                                          "joey_10k": {"equity": 5000.0}})
    assert code == 1
    assert out.count("start_equity=NO") == 2


def test_same_key_pasted_twice_flags_the_pair(capsys):
    env = env_all(APCA_KEY_CAP_10K="PKCAP_5KKEYVALUE")
    code, out, _ = run(capsys, env=env)
    assert code == 1
    assert "distinct accounts: NO (same account behind cap_5k+cap_10k)" in out


def test_two_keys_on_one_account_fail_even_when_equity_matches(capsys):
    # both 5k accounts on login A report the same account: every per-account check passes
    code, out, _ = run(capsys, overrides={"rsibb_5k": {"number": "PAjoey_5k9999"}})
    assert code == 1 and "KEY CHECK FAIL" in out
    assert "distinct accounts: NO (same account behind joey_5k+rsibb_5k)" in out
    assert out.count("start_equity=yes") == 6


def test_missing_secret_and_live_key(capsys):
    env = env_all(APCA_KEY_RSIBB_5K="", APCA_KEY_JOEY_10K="AKLIVEKEY")
    code, out, _ = run(capsys, env=env)
    assert code == 1
    assert "rsibb_5k: NO (secret APCA_KEY_RSIBB_5K / APCA_SECRET_RSIBB_5K missing)" in out
    assert "joey_10k: NO (key is not a paper key" in out
    assert "AKLIVEKEY" not in out


def test_rejected_key_reports_error_type_only(capsys):
    def bad(key, secret):
        raise PermissionError("forbidden " + key)
    code = kc.run(broker_factory=bad, env=env_all(), log=lambda *a, **k: None, now_et=NOW)
    out = capsys.readouterr().out
    assert code == 1 and out.count("key rejected or API error: PermissionError") == 6
    assert "KEYVALUE" not in out


def test_positions_open_orders_or_inactive_fail(capsys):
    code, out, _ = run(capsys, overrides={"joey_5k": {"positions": 1},
                                          "cap_5k": {"open_orders": 2},
                                          "rsibb_5k": {"status": "ACCOUNT_UPDATED"}})
    assert code == 1
    assert "no_positions=NO" in out and "no_open_orders=NO" in out and "active=NO" in out


def test_zero_equity_placeholder_flagged(capsys):
    code, out, _ = run(capsys, overrides={"cap_10k": {"zero_day": True}})
    assert code == 1
    line = [l for l in out.splitlines() if l.startswith("cap_10k:")][0]
    assert "no_zero_equity=NO" in line


def test_cancel_not_confirmed_tells_zander_to_cancel(capsys, monkeypatch):
    monkeypatch.setattr(kc, "CANCEL_WAIT_S", 0)
    code, out, _ = run(capsys, overrides={"joey_5k": {"cancel_works": False}})
    assert code == 1
    assert "cancelled=NO" in out and "cancel the SPY order by hand" in out


def test_submit_rejected_still_reports(capsys):
    code, out, _ = run(capsys, overrides={"joey_5k": {"submit_error": ValueError("market closed")}})
    assert code == 1 and "order_accepted=NO (ValueError: market closed)" in out


@pytest.mark.parametrize("hh,mm", [(15, 30), (15, 59), (16, 15)])
def test_no_order_near_the_close(capsys, hh, mm):
    made = []
    code, out, _ = run(capsys, made=made, now=NOW.replace(hour=hh, minute=mm))
    assert code == 1 and "skipped (too close to 16:00 ET)" in out
    assert not made[0].submitted


def test_order_placed_outside_the_close_window(capsys):
    made = []
    code, out, _ = run(capsys, made=made, now=NOW.replace(hour=16, minute=16))
    assert code == 0 and len(made[0].submitted) == 1


def test_log_write_failure_and_missing_log_secrets(capsys):
    def boom(*a, **k):
        raise RuntimeError("401")
    code, out, _ = run(capsys, log=boom)
    assert code == 1 and "run log write: NO (RuntimeError)" in out
    code, out, _ = run(capsys, env=env_all(PAPER_LOG_TOKEN=""))
    assert code == 1 and "run log write: NO (PAPER_LOG_REPO / PAPER_LOG_TOKEN missing)" in out


# ---- the Broker methods key_check adds, with the real alpaca request classes

class FakeClient:
    last = None

    def __init__(self, key, secret, paper=False, **kw):
        assert paper is True
        self.calls = []
        FakeClient.last = self

    def get_orders(self, req):
        assert isinstance(req, GetOrdersRequest)
        self.calls.append(("get_orders", req.status))
        return ["o1", "o2"]

    def get_account(self):
        return "acct"

    def get_order_by_id(self, oid):
        self.calls.append(("get_order_by_id", oid))
        return "order"

    def cancel_order_by_id(self, oid):
        self.calls.append(("cancel_order_by_id", oid))


def test_broker_key_check_methods(monkeypatch):
    monkeypatch.setattr(alpaca_client, "TradingClient", FakeClient)
    b = Broker("k", "s")
    c = FakeClient.last
    assert b.account() == "acct"
    assert b.open_orders() == ["o1", "o2"]
    assert b.get_order("x") == "order"
    b.cancel("x")
    assert c.calls == [("get_orders", QueryOrderStatus.OPEN), ("get_order_by_id", "x"),
                       ("cancel_order_by_id", "x")]
