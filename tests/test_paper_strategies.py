import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paper.strategies import STRATEGIES  # noqa: E402
from strategies.ports import (capitulation_stretch_reversion as cap,  # noqa: E402
                              rsi_bb_inside_strategy as rsibb)
from strategies.ports.base import atr_wilder  # noqa: E402


def _frame(seed, n=600, start=100.0, vol=0.02):
    rng = np.random.default_rng(seed)
    c = start * np.exp(np.cumsum(rng.normal(0, vol, n)))
    o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.003, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n)))
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                         "volume": 1e6}, index=idx)


def test_six_accounts():
    from paper.accounts import ACCOUNTS
    assert {a.key for a in ACCOUNTS} == {"joey_5k", "cap_5k", "rsibb_5k",
                                         "joey_10k", "cap_10k", "rsibb_10k"}
    assert all((a.start_equity, a.slots) in {(5000.0, 5), (10000.0, 10)} for a in ACCOUNTS)
    assert {a.login for a in ACCOUNTS if a.start_equity == 5000.0} == {"A"}
    assert {a.login for a in ACCOUNTS if a.start_equity == 10000.0} == {"B"}
    a = ACCOUNTS[0]
    assert a.env_key == f"APCA_KEY_{a.key.upper()}"
    assert a.env_secret == f"APCA_SECRET_{a.key.upper()}"


def test_entries_match_port_and_floor():
    df = _frame(5, n=1500, start=6.0, vol=0.04)  # wanders through sub-$5 territory
    assert (df.close < 5).any() and (df.close >= 5).any()
    out = STRATEGIES["rsi_bb_inside_strategy"].entries(df)
    port = rsibb.compute(df)
    assert (out["long"] == (port["entries"] & (df.close >= 5))).all()
    assert (out["short"] == (port["short_entries"] & (df.close >= 5))).all()
    assert out["long"].any() and out["short"].any()
    assert (port["entries"] & ~out["long"]).any()  # floor actually removed some
    assert not STRATEGIES["joey_stochrsi_atr"].entries(df)["short"].any()
    assert not STRATEGIES["capitulation_stretch_reversion"].entries(df)["short"].any()


def test_joey_atr_locked_at_own_signal_bar():
    df = _frame(2)
    atr = atr_wilder(df.high, df.low, df.close, 14).to_numpy()
    s0, s1, j = 50, 53, 55
    assert abs(atr[s0] - atr[s1]) > 1e-9
    p = float(df.close.iloc[s0 + 1])
    # put bar j's high between the two ATR-based targets, low safely above both stops
    df2 = df.copy()
    lo_r, hi_r = sorted([atr[s0], atr[s1]])
    df2.iloc[j, df2.columns.get_loc("high")] = p + 1.5 * (lo_r + hi_r) / 2
    df2.iloc[j, df2.columns.get_loc("low")] = p
    atr2 = atr_wilder(df2.high, df2.low, df2.close, 14).to_numpy()
    fn = STRATEGIES["joey_stochrsi_atr"].held_exit(df2)
    got = {}
    for sig in (s0, s1):
        r = 1.5 * atr2[sig]
        expected = bool(df2.high.iloc[j] >= p + r or df2.low.iloc[j] <= p - r)
        assert fn(j, sig, p, "long") == expected
        got[sig] = expected
    assert got[s0] != got[s1]


def test_capitulation_time_stop():
    n = 60
    idx = pd.bdate_range("2021-01-01", periods=n)
    c = np.linspace(100, 99.4, n)  # falling, tiny range: no stop, close stays below EMA
    df = pd.DataFrame({"open": c, "high": c + 0.1, "low": c - 0.1, "close": c,
                       "volume": 1e6}, index=idx)
    fn = STRATEGIES["capitulation_stretch_reversion"].held_exit(df)
    sig = 30
    assert fn(sig + 9, sig, c[sig + 1], "long") is False
    assert fn(sig + 10, sig, c[sig + 1], "long") is True


def test_held_exit_equals_port_replay_when_no_overlap():
    checked = 0
    for seed in (3, 4, 5):
        df = _frame(seed, n=900)
        n = len(df)
        port = cap.compute(df)
        fn = STRATEGIES["capitulation_stretch_reversion"].held_exit(df)
        exits_port = set(np.flatnonzero(port["exits"].to_numpy()))
        mine, next_free = set(), 0
        for s in np.flatnonzero(port["entries"].to_numpy()):
            if s < next_free or s + 1 >= n:
                continue
            hit = next((j for j in range(s + 2, n)
                        if fn(j, s, df.close.iloc[s + 1], "long")), None)
            if hit is None:
                break
            mine.add(hit)
            next_free = hit + 2
        assert mine == exits_port
        checked += len(mine)
        rp = rsibb.compute(df)
        f2 = STRATEGIES["rsi_bb_inside_strategy"].held_exit(df)
        for side in ("long", "short"):
            for j in range(n):
                assert f2(j, 0, 1.0, side) == bool(rp["exits"].iloc[j])
    assert checked > 0
