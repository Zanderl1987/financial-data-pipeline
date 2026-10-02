"""Event construction for the strike-introduction study."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import events as ev

IDX = pd.bdate_range("2026-01-01", periods=120)


def _close(jump_at=80, jump=0.12, n=120, seed=0):
    rng = np.random.default_rng(seed)
    spy = 100 * np.cumprod(1 + rng.normal(0, 0.005, n))
    a_ret = rng.normal(0, 0.01, n)
    a_ret[jump_at] += jump
    a = 50 * np.cumprod(1 + a_ret)
    return pd.DataFrame({"SPY": spy, "A": a}, index=IDX[:n])


def test_day_trigger_finds_the_jump_with_direction_and_excludes_event_day_from_sigma():
    e = ev.find_events(_close(), "day").set_index("event_date")
    assert IDX[80] in e.index
    assert e.loc[IDX[80], "direction"] == 1 and e.loc[IDX[80], "move_sigma"] > 2.5
    # sigma comes from the 60 days BEFORE the event: a jump of 12 sd-units of 1% noise
    assert e.loc[IDX[80], "move_sigma"] > 8


def test_run_trigger_cooldown_gives_one_event_for_one_run():
    c = _close(jump=0.0)
    r = c["A"].pct_change().fillna(0).to_numpy().copy()
    r[80:85] += 0.03                                  # five +3% excess days
    c["A"] = 50 * np.cumprod(1 + r)
    e = ev.find_events(c, "run")
    assert len(e[(e["event_date"] >= IDX[80]) & (e["event_date"] <= IDX[90])]) == 1


def test_hit_uses_move_side_over_e1_e2_and_entry_is_e3():
    events = pd.DataFrame({"symbol": ["A", "A"], "event_date": [IDX[80], IDX[100]],
                           "direction": [1, -1], "move_sigma": [3.0, -3.0]})
    dates = [d.strftime("%Y-%m-%d") for d in IDX]
    intros = pd.DataFrame({"symbol": "A", "date": dates, "valid": True, "above": 0,
                           "below": 0, "above_all": 0, "below_all": 0})
    intros.loc[82, "above"] = 2                      # e+2 of the up event
    intros.loc[101, "above"] = 5                     # opposite side for the down event
    out = ev.attach_intros(events, intros, IDX).set_index("event_date")
    assert out.loc[IDX[80], "hit"] and out.loc[IDX[80], "dir_intro"] == 2
    assert not out.loc[IDX[100], "hit"] and out.loc[IDX[100], "opp_intro"] == 5
    assert out.loc[IDX[80], "entry_date"] == IDX[83]


def test_event_with_invalid_window_day_is_dropped():
    events = pd.DataFrame({"symbol": ["A"], "event_date": [IDX[80]], "direction": [1],
                           "move_sigma": [3.0]})
    dates = [d.strftime("%Y-%m-%d") for d in IDX]
    intros = pd.DataFrame({"symbol": "A", "date": dates, "valid": True, "above": 0,
                           "below": 0, "above_all": 0, "below_all": 0})
    intros.loc[81, "valid"] = False
    assert ev.attach_intros(events, intros, IDX).empty


def test_forward_returns_are_signed_excess_and_nan_past_history():
    c = pd.DataFrame({"SPY": np.linspace(100, 110, 120), "A": np.linspace(50, 60, 120)},
                     index=IDX)
    events = pd.DataFrame({"symbol": ["A"], "event_date": [IDX[100]], "direction": [-1],
                           "entry_date": [IDX[103]]})
    out = ev.forward_returns(events, c, [1, 21])
    a = c["A"].iloc[104] / c["A"].iloc[103] - 1
    s = c["SPY"].iloc[104] / c["SPY"].iloc[103] - 1
    assert np.isclose(out.iloc[0]["ret_1"], -(a - s))
    assert np.isnan(out.iloc[0]["ret_21"])            # 103 + 21 > last index


def test_earnings_flag_within_one_trading_day():
    events = pd.DataFrame({"symbol": ["A", "A"], "event_date": [IDX[10], IDX[50]]})
    earn = pd.DataFrame({"symbol": ["A"], "date": [IDX[11]]})
    assert list(ev.earnings_flag(events, earn, IDX)) == [True, False]
