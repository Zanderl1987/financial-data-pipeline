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


def run(frames, held=(), eq=5000.0, spec=None, short=lambda s: True,
        recent=frozenset(), n_universe=None):
    return decide_orders(ACC5, spec or make_spec(), frames, list(held), TODAY, PREV,
                         eq, short, set(recent),
                         len(frames) if n_universe is None else n_universe)


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


def test_reentry_blocked_after_exit_fill():
    f = {"X": frame(long=True), "Y": frame(long=True, volume=1)}
    d = run(f, recent={"X"})
    assert [o.symbol for o in d.orders] == ["Y"] and "reentry_blocked:X" in d.notes
    assert [o.symbol for o in run(f).orders] == ["X", "Y"]


def test_fetch_coverage_refuses():
    f = {f"S{i}": frame(long=True) for i in range(8)}
    d = run(f, n_universe=10)
    assert d.status == "stale_data" and d.orders == []
    assert "fetch_coverage:8/10" in d.notes
    assert run(f, n_universe=8).status == "ok"
    assert run({**f, "S8": frame()}, n_universe=10).status == "ok"  # 9/10 passes


def test_held_symbol_not_a_candidate():
    f = {"A": frame(long=True), "B": frame(long=True, volume=1)}
    d = run(f, [Held("A", "long", 5, 50, sig_back(f["A"], 1))])
    assert [o.symbol for o in d.orders] == ["B"]


def test_unshortable_does_not_consume_slot():
    f = {"A": frame(volume=5000, short=True), "B": frame(volume=10, short=True)}
    held = [Held(f"X{i}", "long", 1, 50, None) for i in range(4)]  # free = 1
    d = run(f, held, short=lambda s: s != "A")
    assert [o.symbol for o in d.orders] == ["B"]


def test_no_exit_for_stale_held_symbol():
    f = {f"S{i}": frame() for i in range(19)}
    f["OLD"] = frame(end=PREV - timedelta(days=7))
    d = run(f, [Held("OLD", "long", 5, 50, sig_back(f["OLD"], 4))])
    assert d.status == "ok" and "stale:OLD" in d.notes
    assert not [o for o in d.orders if o.action == "exit"]


def test_held_without_frame_noted():
    f = {"A": frame()}
    d = run(f, [Held("GONE", "long", 5, 50, PREV)])
    assert "untracked:GONE" in d.notes and d.orders == []
    # sig_date not in the frame
    d = run(f, [Held("A", "long", 5, 50, date(2020, 1, 1))])
    assert "untracked:A" in d.notes and d.orders == []


def test_exactly_five_percent_stale_is_ok():
    f = {f"S{i}": frame() for i in range(19)}
    f["OLD"] = frame(end=PREV - timedelta(days=7))
    assert run(f).status == "ok"


# ---- I2: exits are sticky (a lost exit becomes a late exit, as in the sim) ----

def _spec_fires_on(bars_from_end):
    """held_exit true only on bars n-1-b for b in bars_from_end."""
    def held_exit(df):
        hits = {len(df) - 1 - b for b in bars_from_end}
        return lambda j, sig_i, ep, side: j in hits
    return StrategySpec("fake", _entries, held_exit)


def test_exit_fired_on_missed_bar_exits_today():
    f = {"A": frame()}
    held = [Held("A", "long", 5, 50, sig_back(f["A"], 4))]
    d = run(f, held, spec=_spec_fires_on({1}))       # fired on bar j-1 only
    assert [(o.action, o.symbol, o.qty) for o in d.orders] == [("exit", "A", 5)]


def test_catch_up_never_looks_before_entry_bar_plus_one():
    f = {"A": frame()}
    held = [Held("A", "long", 5, 50, sig_back(f["A"], 4))]
    # bar sig_i+1 (the entry fill bar) = 3 bars before the last bar
    assert run(f, held, spec=_spec_fires_on({3})).orders == []
    assert [o.action for o in run(f, held, spec=_spec_fires_on({2})).orders] == ["exit"]


def _replay_vs_sim(skip_days=(), max_hold=7):
    """Run decide_orders day by day (fills at that day's close) and the sim
    over the same frames; return (replay trades, sim trades, frames)."""
    import numpy as np
    from paper.sim import simulate_account
    rng = np.random.default_rng(7)
    n, syms = 90, [f"S{i}" for i in range(9)]
    idx = pd.bdate_range("2026-01-05", periods=n)
    frames = {}
    for s in syms:
        c = rng.uniform(10, 60, n)
        frames[s] = pd.DataFrame({
            "open": c, "high": c, "low": c, "close": c,
            "volume": rng.uniform(1e5, 1e6, n),
            "sig_long": rng.random(n) < 0.08, "sig_short": rng.random(n) < 0.05,
            "xf": rng.random(n) < 0.15}, index=idx)

    def held_exit(df):
        xf = df["xf"].to_numpy(bool)
        return lambda j, sig_i, ep, side: bool(xf[j]) or (j - sig_i) >= max_hold
    spec = StrategySpec("fake", _entries, held_exit)

    sim = simulate_account(frames, spec, start_equity=5000.0, slots=ACC5.slots,
                           start=str(idx[0].date()), end=str(idx[-1].date()))
    sim_tr = sorted((r.symbol, r.sig_date.date(), r.entry_date.date(),
                     r.exit_date.date()) for r in sim.trades.itertuples())

    held, recent, trades = [], set(), []
    for k in range(1, n):
        if k in skip_days:
            recent = set()
            continue
        today, prev = idx[k].date(), idx[k - 1].date()
        vis = {s: df.iloc[:k] for s, df in frames.items()}
        dec = decide_orders(ACC5, spec, vis, list(held), today, prev, 5000.0,
                            lambda s: True, recent, len(vis))
        assert dec.status == "ok"
        recent = set()
        for o in dec.orders:
            px = float(frames[o.symbol]["close"].iloc[k])
            if o.action == "exit":
                h = next(h for h in held if h.symbol == o.symbol)
                held.remove(h)
                trades.append((h.symbol, h.sig_date, h.opened, today))
                recent.add(o.symbol)
            else:
                held.append(Held(o.symbol, "long" if o.side == "buy" else "short",
                                 o.qty, px, prev, today))
    return sorted(trades), sim_tr, frames


def test_daily_replay_matches_sim_on_normal_path():
    for max_hold in (7, 30):
        replay, sim, _ = _replay_vs_sim(max_hold=max_hold)
        assert len(sim) > 30          # the scenario really exercises the rules
        assert replay == sim


def test_missed_day_exit_fills_one_day_late_not_lost():
    """rsi_bb-style one-bar exit: the exit bar is followed by a bar on which
    the condition is false. Skipping the run that would have exited must give
    an exit on the next run, not a position held until the next signal."""
    replay, _, frames = _replay_vs_sim(max_hold=30)
    idx = pd.bdate_range("2026-01-05", periods=90)
    pos = {d: i for i, d in enumerate(idx.date)}
    for sym, sig, opened, exited in replay:
        k = pos[exited]                     # exit filled at bar k, fired on k-1
        if k + 1 < 90 and not frames[sym]["xf"].iloc[k] and k - pos[sig] < 30:
            break
    else:
        raise AssertionError("no one-bar exit in the scenario")
    late, _, _ = _replay_vs_sim(skip_days={k}, max_hold=30)
    got = next(t for t in late if t[0] == sym and t[1] == sig)
    assert got[3] == idx[k + 1].date()


# ---- I3: the loss-limit stop is latched -----------------------------------

def test_latched_stop_closes_held_and_never_enters():
    f = {"A": frame(), "B": frame(long=True), "C": frame(long=True)}
    held = [Held("A", "long", 3, 50, sig_back(f["A"], 9))]
    d = decide_orders(ACC5, make_spec(exit_all=False), f, held, TODAY, PREV,
                      4800.0, lambda s: True, set(), 3, stop_latched=True)
    assert d.stopped and d.status == "stopped"
    assert [(o.symbol, o.action, o.side, o.qty) for o in d.orders] == [
        ("A", "stop_all", "sell", 3)]
    assert "stop:latched" in d.notes


def test_latched_stop_with_nothing_held_places_nothing():
    f = {"B": frame(long=True)}
    d = decide_orders(ACC5, make_spec(), f, [], TODAY, PREV, 9999.0,
                      lambda s: True, set(), 1, stop_latched=True)
    assert d.stopped and d.status == "stopped" and d.orders == []


def test_not_latched_trades_normally():
    f = {"B": frame(long=True)}
    d = decide_orders(ACC5, make_spec(), f, [], TODAY, PREV, 4800.0,
                      lambda s: True, set(), 1, stop_latched=False)
    assert not d.stopped and [o.action for o in d.orders] == ["entry_long"]
