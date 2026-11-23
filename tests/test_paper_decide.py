from datetime import date, timedelta

import pandas as pd
import pytest

from paper.accounts import ACCOUNTS
from paper.decide import (Held, accrued_borrow_fee, client_order_id,
                          decide_orders, parse_client_order_id)
from paper.strategies import StrategySpec

ACC5 = next(a for a in ACCOUNTS if a.key == "joey_5k")
PREV = date(2026, 11, 20)
TODAY = date(2026, 11, 23)


def frame(close=50.0, volume=1000.0, n=10, end=PREV, long=False, short=False):
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    df = pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                       "volume": volume}, index=idx)
    df["sig_long"] = False
    df["sig_short"] = False
    df.loc[idx[-1], "sig_long"] = long
    df.loc[idx[-1], "sig_short"] = short
    return df


def _entries(df):
    return {"long": df["sig_long"], "short": df["sig_short"]}


def make_spec(exit_syms=None, exit_all=True):
    """exit_syms: set of frame ids (id(df)) that should trigger exits."""
    def held_exit(df):
        hit = exit_all if exit_syms is None else id(df) in exit_syms
        return lambda j, sig_i, ep, side: hit
    return StrategySpec("fake", _entries, held_exit)


def run(frames, held=(), eq=5000.0, spec=None, short=lambda s: True):
    return decide_orders(ACC5, spec or make_spec(), frames, list(held), TODAY, PREV,
                         eq, short)


def sig_back(df, back):
    return df.index[-1 - back].date()


def test_loss_limit_stops_all():
    f = {"A": frame(), "B": frame()}
    held = [Held("A", "long", 3, 50, PREV), Held("B", "short", 2, 50, None)]
    d = run(f, held, eq=3999.99)
    assert d.stopped and d.status == "stopped"
    assert [(o.symbol, o.side, o.qty, o.action) for o in d.orders] == [
        ("A", "sell", 3, "stop_all"), ("B", "buy", 2, "stop_all")]


def test_loss_limit_boundary():
    f = {"A": frame(long=True)}
    assert run(f, eq=4000.00).stopped
    d = run(f, eq=4000.01)
    assert not d.stopped and d.status == "ok"


def test_loss_limit_beats_stale():
    f = {"A": frame(end=PREV - timedelta(days=7))}
    d = run(f, [Held("A", "long", 1, 50, PREV)], eq=1.0)
    assert d.status == "stopped" and d.orders[0].action == "stop_all"


def test_exit_fires_and_frees_slot():
    f = {f"H{i}": frame() for i in range(5)}
    f["S1"] = frame(close=20, volume=100, long=True)
    f["S2"] = frame(close=20, volume=900, long=True)
    held = [Held(f"H{i}", "long", 10, 50, sig_back(f["H0"], 3)) for i in range(5)]
    d = run(f, held, spec=make_spec({id(f["H0"])}))
    assert [(o.action, o.symbol) for o in d.orders] == [("exit", "H0"),
                                                         ("entry_long", "S2")]
    assert d.orders[1].qty == 50


def test_no_exit_on_entry_bar():
    f = {"A": frame()}
    assert run(f, [Held("A", "long", 5, 50, sig_back(f["A"], 1))]).orders == []
    d = run(f, [Held("A", "long", 5, 50, sig_back(f["A"], 2))])
    assert [o.action for o in d.orders] == ["exit"]


def test_exit_uses_held_qty():
    f = {"A": frame()}
    d = run(f, [Held("A", "long", 2, 50, sig_back(f["A"], 3))])
    assert d.orders[0].qty == 2 and d.orders[0].side == "sell"


def test_untracked_position_flagged():
    f = {"A": frame(), "B": frame(long=True)}
    held = [Held("A", "long", 2, 50, None)] + [Held(f"X{i}", "long", 1, 50, None)
                                               for i in range(4)]
    d = run(f, held)
    assert "untracked:A" in d.notes
    assert d.orders == []  # 5 held fill all 5 slots


def test_stale_last_bar_refuses():
    f = {f"S{i}": frame(long=True) for i in range(9)}
    f["OLD"] = frame(end=PREV - timedelta(days=7))
    d = run(f)
    assert d.status == "stale_data" and d.orders == []


def test_stale_minor_drops_symbol():
    f = {f"S{i}": frame() for i in range(39)}
    f["OLD"] = frame(end=PREV - timedelta(days=7), long=True)
    d = run(f)
    assert d.status == "ok" and "stale:OLD" in d.notes and d.orders == []


def test_unshortable_skipped():
    f = {"A": frame(volume=5000, short=True), "B": frame(volume=10, short=True)}
    d = run(f, short=lambda s: s != "A")
    assert "skipped_unshortable:A" in d.notes
    assert [(o.symbol, o.action, o.side) for o in d.orders] == [
        ("B", "entry_short", "sell")]


def test_floor_and_zero_shares():
    f = {"A": frame(close=4.99, long=True), "B": frame(close=1200.0, long=True),
         "C": frame(close=50.0, long=True)}
    d = run(f)
    assert [o.symbol for o in d.orders] == ["C"]


def test_long_wins_and_tiebreak():
    f = {"B": frame(long=True, short=True), "A": frame(long=True)}
    d = run(f)
    assert [(o.symbol, o.action) for o in d.orders] == [("A", "entry_long"),
                                                         ("B", "entry_long")]


def test_client_order_id_roundtrip():
    s = client_order_id("joey_5k", TODAY, "exit", "AAPL", PREV)
    assert s == "pt|joey_5k|20261123|exit|AAPL|20261120"
    assert parse_client_order_id(s) == {"account_key": "joey_5k", "today": TODAY,
                                        "action": "exit", "symbol": "AAPL",
                                        "sig_date": PREV}
    assert parse_client_order_id("abc") is None


def test_accrued_borrow_fee():
    d0 = date(2026, 1, 1)
    got = accrued_borrow_fee([(1000.0, d0, None)], d0 + timedelta(days=73))
    assert got == pytest.approx(1000 * 0.027 * 73 / 365)
