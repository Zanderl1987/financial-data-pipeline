"""Broker order-history logic against a fake TradingClient (no network).

Order objects mimic alpaca-py's Order model: string quantities and prices,
UTC datetimes, enum status/side. The real alpaca-py request classes are used,
so a renamed field or enum fails here rather than on the first live morning.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

import alpaca.trading.client as alpaca_client
from paper.broker import Broker
from paper.decide import OrderIntent, client_order_id

UTC = timezone.utc
ACCT = "rsibb_5k"
_seq = [0]


def utc(y, m, d, hh=21, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def order(action, sym, today, sig, *, side=None, status=OrderStatus.FILLED,
          qty=10, filled_qty=None, price=50.0, filled_at="auto", acct=ACCT,
          cid=None):
    """today/sig are NY dates. filled_at defaults to 21:00Z on `today`
    (16:00 ET in winter); pass None for orders Alpaca reports without one."""
    _seq[0] += 1
    if side is None:
        side = {"entry_long": "buy", "entry_short": "sell"}.get(action, "sell")
    fq = qty if filled_qty is None else filled_qty
    if filled_at == "auto":
        filled_at = utc(today.year, today.month, today.day) if fq else None
    sub = utc(today.year, today.month, today.day, 15, 0) + timedelta(seconds=_seq[0])
    return SimpleNamespace(
        id=f"id{_seq[0]}",
        client_order_id=cid or client_order_id(acct, today, action, sym, sig),
        symbol=sym, side=OrderSide(side), status=status, qty=str(qty),
        filled_qty=str(fq), filled_avg_price=(str(price) if fq else None),
        filled_at=filled_at, submitted_at=sub, updated_at=sub + timedelta(hours=7),
        created_at=sub)


def position(sym, qty, side="long", price=50.0):
    return SimpleNamespace(symbol=sym, qty=str(qty), side=SimpleNamespace(value=side),
                           avg_entry_price=str(price), unrealized_pl="0")


class FakeClient:
    last = None

    def __init__(self, key, secret, paper=False, **kw):
        self.init = dict(key=key, secret=secret, paper=paper, kw=kw)
        self.orders, self.positions, self.equity = [], [], "5000"
        self.requests, self.submitted = [], []
        FakeClient.last = self

    def get_orders(self, req):
        assert isinstance(req, GetOrdersRequest)
        self.requests.append(req)
        rows = [o for o in self.orders
                if req.until is None or o.submitted_at <= req.until]
        rows.sort(key=lambda o: o.submitted_at, reverse=True)   # Alpaca: desc
        return rows[:req.limit or 50]

    def get_all_positions(self):
        return list(self.positions)

    def get_account(self):
        return SimpleNamespace(equity=self.equity)

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="new-order-id")


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setattr(alpaca_client, "TradingClient", FakeClient)
    b = Broker("k", "s")
    return b, FakeClient.last


D = date(2026, 11, 2)   # a Monday; NY dates below are D + n days


def d(n):
    return D + timedelta(days=n)


# ---- held() --------------------------------------------------------------

def test_held_tracked_untracked_and_reentry(broker):
    b, c = broker
    c.orders = [
        order("entry_long", "AAA", d(1), d(0)),
        order("exit", "AAA", d(3), d(0)),
        order("entry_long", "AAA", d(8), d(7)),         # re-entry: new sig_date
        order("entry_long", "BBB", d(2), d(1)),
    ]
    c.positions = [position("AAA", 10), position("BBB", 5), position("ZZZ", 3)]
    h = {x.symbol: x for x in b.held()}
    assert (h["AAA"].sig_date, h["AAA"].opened) == (d(7), d(8))
    assert (h["BBB"].sig_date, h["BBB"].opened) == (d(1), d(2))
    assert h["ZZZ"].sig_date is None                       # untracked
    assert h["AAA"].side == "long" and h["AAA"].qty == 10


@pytest.mark.parametrize("status", [OrderStatus.EXPIRED, OrderStatus.CANCELED,
                                    OrderStatus.PARTIALLY_FILLED])
def test_held_partial_fill_counts_as_entry(broker, status):
    b, c = broker
    c.orders = [
        order("entry_long", "AAA", d(1), d(0)),
        order("exit", "AAA", d(3), d(0)),
        # re-entry only partly filled; Alpaca leaves filled_at empty
        order("entry_long", "AAA", d(8), d(7), status=status, qty=20,
              filled_qty=7, filled_at=None),
    ]
    c.positions = [position("AAA", 7)]
    (h,) = b.held()
    assert (h.sig_date, h.opened, h.qty) == (d(7), d(8), 7)


def test_held_ignores_unfilled_and_foreign_orders(broker):
    b, c = broker
    c.orders = [
        order("entry_long", "AAA", d(1), d(0)),
        order("entry_long", "AAA", d(8), d(7), status=OrderStatus.EXPIRED,
              filled_qty=0),                                    # nothing filled
        order("entry_long", "AAA", d(9), d(8), cid="manual-123"),  # not ours
    ]
    c.positions = [position("AAA", 10)]
    (h,) = b.held()
    assert h.sig_date == d(0)


# ---- fee_inputs() --------------------------------------------------------

def test_fee_inputs_short_lots_open_close_and_partial(broker):
    b, c = broker
    c.equity = "5123.45"
    c.orders = [
        order("entry_short", "SSS", d(1), d(0), qty=10, price=50.0),
        order("exit", "SSS", d(5), d(0), side="buy", qty=10, filled_qty=4,
              status=OrderStatus.EXPIRED, filled_at=None),       # partial cover
        order("exit", "SSS", d(8), d(0), side="buy", qty=6),
        order("entry_long", "LLL", d(1), d(0), price=20.0),
        order("exit", "LLL", d(3), d(0), side="sell"),            # long: no lot
        order("entry_short", "TTT", d(9), d(8), qty=20, filled_qty=5, price=40.0,
              status=OrderStatus.CANCELED, filled_at=None),       # partial short
    ]
    equity, lots = b.fee_inputs()
    assert equity == pytest.approx(5123.45)
    assert sorted(lots, key=lambda x: (x[0], x[3] or date.max)) == [
        ("SSS", pytest.approx(200.0), d(1), d(5)),
        ("SSS", pytest.approx(300.0), d(1), d(8)),
        ("TTT", pytest.approx(200.0), d(9), None),
    ]


def test_fee_inputs_stop_all_closes_lot(broker):
    b, c = broker
    c.orders = [order("entry_short", "SSS", d(1), d(0), qty=10, price=50.0),
                order("stop_all", "SSS", d(4), d(0), side="buy", qty=10)]
    _, lots = b.fee_inputs()
    assert lots == [("SSS", pytest.approx(500.0), d(1), d(4))]


# ---- recently_exited() -----------------------------------------------------

def test_recently_exited_dates_fills_in_new_york(broker):
    b, c = broker
    prev = d(4)
    c.orders = [
        # 01:30Z on d(5) is 20:30 ET on d(4): the fill belongs to d(4)
        order("exit", "AAA", prev, d(0), filled_at=utc(2026, 11, 7, 1, 30)),
        # an exit filled the day before prev does not block
        order("exit", "BBB", d(3), d(0)),
        order("stop_all", "CCC", prev, d(0)),
        order("entry_long", "DDD", prev, d(3)),                  # entries don't count
        order("exit", "EEE", prev, d(0), status=OrderStatus.EXPIRED,
              filled_qty=3, filled_at=None),                     # partial exit counts
        order("exit", "FFF", prev, d(0), status=OrderStatus.EXPIRED,
              filled_qty=0),                                     # unfilled: no
    ]
    assert b.recently_exited(prev) == {"AAA", "CCC", "EEE"}
    assert "AAA" not in b.recently_exited(d(5))


# ---- orders_today() / stop history ----------------------------------------

def test_orders_today_prefix(broker):
    b, c = broker
    c.orders = [order("entry_long", "AAA", d(4), d(3)),
                order("entry_long", "BBB", d(3), d(2)),
                order("entry_long", "CCC", d(4), d(3), acct="joey_5k"),
                order("exit", "DDD", d(4), d(0), status=OrderStatus.REJECTED,
                      filled_qty=0)]
    got = b.orders_today(f"pt|{ACCT}|{d(4):%Y%m%d}")
    assert sorted(got) == sorted([client_order_id(ACCT, d(4), "entry_long", "AAA", d(3)),
                                  client_order_id(ACCT, d(4), "exit", "DDD", d(0))])


def test_stop_dates_any_stop_all_order_filled_or_not(broker):
    b, c = broker
    c.orders = [order("entry_long", "AAA", d(1), d(0)),
                order("stop_all", "AAA", d(6), d(0), status=OrderStatus.EXPIRED,
                      filled_qty=0),
                order("stop_all", "AAA", d(7), d(0))]
    assert b.stop_dates() == [d(6), d(7)]
    c.orders = c.orders[:1]
    assert b.stop_dates() == []


# ---- pagination ----------------------------------------------------------

def test_order_history_paginates_past_500(broker):
    b, c = broker
    c.orders = [order("entry_long", f"S{i:04d}", d(1), d(0)) for i in range(1203)]
    c.positions = [position("S0000", 1), position("S1202", 1)]
    h = {x.symbol: x for x in b.held()}
    assert h["S0000"].sig_date == d(0) and h["S1202"].sig_date == d(0)
    assert len(b._all_orders()) == 1203
    assert all(r.limit == 500 for r in c.requests)
    assert c.requests[0].until is None and c.requests[1].until is not None


# ---- submit() ------------------------------------------------------------

def test_submit_builds_cls_market_order_paper_only(broker):
    b, c = broker
    assert c.init["paper"] is True and not c.init["kw"]
    cid = client_order_id(ACCT, d(4), "entry_short", "XYZ", d(3))
    assert b.submit(OrderIntent("XYZ", "sell", 17, "entry_short", cid)) == "new-order-id"
    (req,) = c.submitted
    assert isinstance(req, MarketOrderRequest)
    assert req.time_in_force == TimeInForce.CLS
    assert req.side == OrderSide.SELL and req.qty == 17 and req.symbol == "XYZ"
    assert req.client_order_id == cid == "pt|rsibb_5k|20261106|entry_short|XYZ|20261105"
    b.submit(OrderIntent("XYZ", "buy", 3, "exit", cid))
    assert c.submitted[1].side == OrderSide.BUY
