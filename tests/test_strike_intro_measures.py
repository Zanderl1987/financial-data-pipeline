"""Per-expiration introduction panel (spec amendment 2026-10-02)."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import measures


def _ch(rows):
    return pd.DataFrame(rows, columns=["symbol", "date", "contract_ticker", "strike",
                                       "expiration_date", "change"])


def _summ(dates, status=None, replaced=None):
    n = len(dates)
    return pd.DataFrame({"symbol": ["A"] * n, "date": dates,
                         "status": status or ["ok"] * n,
                         "replaced_frac": replaced or [0.0] * n})


BASE = [
    ("A", "2026-09-01", "c100", 100.0, "2026-10-16", "initial"),
    ("A", "2026-09-01", "c110", 110.0, "2026-10-16", "initial"),
    ("A", "2026-09-01", "l050", 50.0, "2027-01-15", "initial"),
    ("A", "2026-09-01", "l300", 300.0, "2027-01-15", "initial"),
]


def test_extension_of_an_existing_expiration_counts_per_expiration_not_whole_chain():
    ch = _ch(BASE + [("A", "2026-09-02", "c120", 120.0, "2026-10-16", "added")])
    out = measures.daily_intros(ch, _summ(["2026-09-01", "2026-09-02"])).set_index("date")
    assert out.loc["2026-09-01", "valid"] == False          # no prior day
    assert out.loc["2026-09-02", "above"] == 1 and out.loc["2026-09-02", "below"] == 0
    assert out.loc["2026-09-02", "above_all"] == 0          # LEAPS pin the chain max (300)


def test_brand_new_expiration_never_counts():
    ch = _ch(BASE + [("A", "2026-09-02", "n500", 500.0, "2026-11-20", "added"),
                     ("A", "2026-09-02", "n010", 10.0, "2026-11-20", "added")])
    out = measures.daily_intros(ch, _summ(["2026-09-01", "2026-09-02"])).set_index("date")
    assert (out.loc["2026-09-02", "above"], out.loc["2026-09-02", "below"]) == (0, 0)
    assert (out.loc["2026-09-02", "above_all"], out.loc["2026-09-02", "below_all"]) == (1, 1)


def test_below_extension_and_inside_strikes():
    ch = _ch(BASE + [("A", "2026-09-02", "c090", 90.0, "2026-10-16", "added"),
                     ("A", "2026-09-02", "c105", 105.0, "2026-10-16", "added")])
    out = measures.daily_intros(ch, _summ(["2026-09-01", "2026-09-02"])).set_index("date")
    assert (out.loc["2026-09-02", "above"], out.loc["2026-09-02", "below"]) == (0, 1)


def test_missing_suspect_and_split_days_are_invalid_and_do_not_move_the_range():
    ch = _ch(BASE + [("A", "2026-09-04", "c120", 120.0, "2026-10-16", "added"),
                     ("A", "2026-09-08", "c130", 130.0, "2026-10-16", "added")])
    s = _summ(["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08"],
              status=["ok", "missing", "suspect", "ok", "ok"],
              replaced=[0.0, 0.0, 0.0, 0.95, 0.0])
    out = measures.daily_intros(ch, s).set_index("date")
    assert list(out["valid"]) == [False, False, False, False, True]
    assert np.isnan(out.loc["2026-09-02", "above"])
    # the split day's listing still updates the range, so 09-08 is measured from 120
    assert out.loc["2026-09-08", "above"] == 1


def test_near_expiration_extremes_after_the_day():
    ch = _ch(BASE + [("A", "2026-09-02", "c120", 120.0, "2026-10-16", "added"),
                     ("A", "2026-09-02", "c100", 100.0, "2026-10-16", "removed")])
    out = measures.daily_intros(ch, _summ(["2026-09-01", "2026-09-02"])).set_index("date")
    assert (out.loc["2026-09-02", "near_min"], out.loc["2026-09-02", "near_max"]) == (110.0, 120.0)
