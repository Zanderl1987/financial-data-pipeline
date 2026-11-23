import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paper.sim import simulate_account  # noqa: E402
from paper.strategies import StrategySpec  # noqa: E402

KW = dict(start="2020-01-01", end="2030-01-01")


def _frame(n=40, close=100.0, volume=1e6, freq="B"):
    idx = pd.date_range("2020-01-01", periods=n, freq=freq)
    c = np.full(n, close) if np.isscalar(close) else np.asarray(close, float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c,
                         "volume": np.full(n, float(volume))}, index=idx)


def _spec(long_at=None, short_at=None, k=2):
    """long_at / short_at: {symbol: [bar, ...]}; exit true k bars after signal bar."""
    long_at = long_at or {}
    short_at = short_at or {}

    def entries(df):
        sym = df.attrs["sym"]
        lo = pd.Series(False, index=df.index)
        sh = pd.Series(False, index=df.index)
        for b in long_at.get(sym, []):
            lo.iloc[b] = True
        for b in short_at.get(sym, []):
            sh.iloc[b] = True
        return {"long": lo, "short": sh}

    def held_exit(df):
        return lambda j, sig_i, entry_price, side: (j - sig_i) >= k

    return StrategySpec("fake", entries, held_exit)


def _frames(**kw):
    out = {}
    for sym, f in kw.items():
        f.attrs["sym"] = sym
        out[sym] = f
    return out


def test_fill_timing():
    f = _frame()
    res = simulate_account(_frames(A=f), _spec({"A": [10]}, k=3),
                           start_equity=5000, slots=5, **KW)
    t = res.trades.iloc[0]
    assert t.sig_date == f.index[10]
    assert t.entry_date == f.index[11]
    assert t.exit_sig_date == f.index[13]
    assert t.exit_date == f.index[14]
    assert len(res.trades) == 1


def test_slot_cap_and_dollar_volume_rank():
    fr = _frames(A=_frame(volume=1e6), B=_frame(volume=3e6), C=_frame(volume=2e6))
    res = simulate_account(fr, _spec({"A": [5], "B": [5], "C": [5]}),
                           start_equity=5000, slots=2, **KW)
    assert set(res.trades.symbol) == {"B", "C"}


def test_rank_ties_by_symbol():
    fr = _frames(B=_frame(), A=_frame(), C=_frame())
    res = simulate_account(fr, _spec({"A": [5], "B": [5], "C": [5]}),
                           start_equity=5000, slots=1, **KW)
    assert list(res.trades.symbol) == ["A"]


def test_freed_slot_reused_same_close():
    a, b = _frame(), _frame()
    res = simulate_account(_frames(A=a, B=b), _spec({"A": [5], "B": [7]}, k=2),
                           start_equity=5000, slots=1, **KW)
    ta = res.trades[res.trades.symbol == "A"].iloc[0]
    tb = res.trades[res.trades.symbol == "B"].iloc[0]
    assert ta.exit_date == a.index[8]
    assert tb.entry_date == b.index[8]


def test_no_reentry_on_exit_fill_bar():
    f = _frame()
    # 5 enters (entry 6); exit signal 7 fills 8; signals 7 and 8 are blocked, 9 is not
    res = simulate_account(_frames(A=f), _spec({"A": [5, 7, 8, 9]}, k=2),
                           start_equity=5000, slots=5, **KW)
    assert list(res.trades.sig_date) == [f.index[5], f.index[9]]


def test_shares_floor_and_skip_zero():
    fr = _frames(A=_frame(close=333.0), B=_frame(close=1500.0))
    res = simulate_account(fr, _spec({"A": [5], "B": [5]}),
                           start_equity=5000, slots=5, **KW)
    assert list(res.trades.symbol) == ["A"]
    assert res.trades.iloc[0].shares == 3
    assert res.trades.iloc[0].borrow_usd == 0.0
    assert res.open_positions.empty


def test_borrow_fee_short():
    f = _frame(n=60, close=330.0, freq="D")
    res = simulate_account(_frames(A=f), _spec(short_at={"A": [2]}, k=30),
                           start_equity=5000, slots=5, **KW)
    t = res.trades.iloc[0]
    assert t.side == "short"
    assert (t.exit_date - t.entry_date).days == 30
    assert abs(t.borrow_usd - 990 * 0.027 * 30 / 365) < 1e-9
    assert t.gross_pnl == 0.0
    assert abs(t.net_pnl - (-t.cost_usd - t.borrow_usd)) < 1e-9


def test_equity_marks_to_market():
    close = [100.0] * 11 + [110.0] * 29
    f = _frame(close=close)
    res = simulate_account(_frames(A=f), _spec({"A": [5]}, k=1000),
                           start_equity=5000, slots=5, **KW)
    assert res.trades.empty
    assert len(res.open_positions) == 1
    assert res.equity.iloc[0] == 5000
    assert res.equity.loc[f.index[6]] == 5000       # entered at 100, still 100
    assert abs(res.equity.iloc[-1] - 5100.0) < 1e-9  # 10 shares * +10
    assert len(res.equity) == len(f)


def test_unlimited_slots():
    fr = _frames(A=_frame(), B=_frame(), C=_frame())
    # A: 3 enters, 4 ignored (held), exit sig 5 -> fill 6; 10 is a second trade
    res = simulate_account(fr, _spec({"A": [3, 4, 10], "B": [3], "C": [3]}, k=2),
                           start_equity=5000, slots=None, **KW)
    assert len(res.trades) == 4
    assert (res.trades.symbol == "A").sum() == 2


def test_exit_never_evaluated_before_entry_bar_plus_one():
    f = _frame()
    res = simulate_account(_frames(A=f), _spec({"A": [5]}, k=0),   # always true
                           start_equity=5000, slots=5, **KW)
    t = res.trades.iloc[0]
    assert t.entry_date == f.index[6]
    assert t.exit_sig_date == f.index[7]
    assert t.exit_date == f.index[8]


def _held_counts(res, dates):
    out = []
    for d in dates:
        n = int(((res.trades.entry_date <= d) & (res.trades.exit_date > d)).sum())
        n += int((res.open_positions.entry_date <= d).sum())
        out.append(n)
    return out


def test_gap_does_not_overallocate_slots():
    full = pd.date_range("2020-01-01", periods=25, freq="B")
    xi = full.drop([pd.Timestamp("2020-01-13"), pd.Timestamp("2020-01-14")])
    x = _frame(n=len(xi))
    x.index = xi
    y = _frame(n=25)
    # X signal 2020-01-06 (idx 3), exit signal 2020-01-10 (idx 7), fills 01-15.
    # Y signals 2020-01-10 and would fill 01-13, before X's exit fills.
    res = simulate_account(_frames(X=x, Y=y), _spec({"X": [3], "Y": [7]}, k=4),
                           start_equity=5000, slots=1, **KW)
    assert max(_held_counts(res, full)) <= 1
    assert "X" in set(res.trades.symbol)


def test_costs_use_entry_and_exit_bar_impact():
    from evaluation.trades import _compute_adv_impact_arrays
    import event_backtest as eb
    rng = np.random.default_rng(7)
    n = 160
    close = 100.0 * np.exp(np.cumsum(0.015 * rng.standard_normal(n)))
    vol = rng.uniform(2e5, 2e6, n)
    f = _frame(n=n, close=close)
    f["volume"] = vol
    res = simulate_account(_frames(A=f), _spec({"A": [130]}, k=5),
                           start_equity=5000, slots=5, **KW)
    t = res.trades.iloc[0]
    assert t.entry_date == f.index[131] and t.exit_date == f.index[136]
    imp, _ = _compute_adv_impact_arrays(f["close"], f["volume"], 1000.0,
                                        eb.ADV_SQRT_LAW_K, n)
    assert imp[131] > 0 and imp[136] > 0 and imp[130] != imp[131]
    c = 1e-4
    want = (t.shares * t.entry_price * (c + imp[131])
            + t.shares * t.exit_price * (c + imp[136]))
    assert abs(t.cost_usd - want) < 1e-9


def test_equity_after_close_includes_realized_net_pnl():
    close = [100.0] * 6 + [110.0] * 34
    f = _frame(close=close)
    res = simulate_account(_frames(A=f), _spec({"A": [3]}, k=2),
                           start_equity=5000, slots=5, **KW)
    t = res.trades.iloc[0]
    assert t.exit_price == 110.0 and t.net_pnl > 0
    assert abs(res.equity.loc[t.exit_date] - (5000 + t.net_pnl)) < 1e-9
    assert abs(res.equity.iloc[-1] - (5000 + t.net_pnl)) < 1e-9
    assert res.equity.loc[f.index[4]] == 5000      # entry day, no move yet


def test_equity_open_short_subtracts_accrued_borrow():
    f = _frame(n=60, close=330.0, freq="D")
    res = simulate_account(_frames(A=f), _spec(short_at={"A": [2]}, k=1000),
                           start_equity=5000, slots=5, **KW)
    ed = res.open_positions.iloc[0].entry_date
    last = f.index[-1]
    want = 5000 - 990 * 0.027 * (last - ed).days / 365
    assert abs(res.equity.iloc[-1] - want) < 1e-9
    assert res.equity.loc[ed] == 5000
