"""Report renders every section from a synthetic results bundle."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import report


def _bundle():
    rng = np.random.default_rng(0)
    n = 80
    ev = pd.DataFrame({"trigger": ["day"] * n, "symbol": "A",
                       "event_date": pd.bdate_range("2025-01-01", periods=n),
                       "direction": rng.choice([-1, 1], n), "hit": rng.random(n) > 0.5,
                       "excess": rng.normal(size=n), "earnings": rng.random(n) > 0.8,
                       **{f"ret_{h}": rng.normal(0, 0.02, n) for h in [1, 3, 5, 10, 21, 63, 126]},
                       "absret_21": rng.uniform(0, 0.1, n), "rv_ratio": rng.uniform(0.5, 2, n)})
    car = pd.DataFrame(rng.normal(0, 0.01, (n, 132)).cumsum(axis=1), columns=range(-5, 127))
    tests = pd.DataFrame([{"test_id": "day|ret_21|all", "trigger": "day", "measure": "ret",
                           "horizon": 21, "subgroup": "all", "n_a": 40, "n_b": 40, "diff": 0.01,
                           "ci_lo": -0.01, "ci_hi": 0.03, "p_welch": 0.2, "p_perm": 0.21,
                           "p_boot": 0.25, "p_mw": 0.3, "p_adj": np.nan, "cohen_d": 0.2,
                           "primary": True}])
    reg = pd.DataFrame({"term": ["const", "hit"], "coef": [0.0, 0.01], "se": [0.01, 0.01],
                        "p": [0.9, 0.3]})
    eq = pd.Series(np.cumsum(rng.normal(0, 0.01, 50)), index=pd.bdate_range("2025-02-01", periods=50))
    ranges = pd.DataFrame({"symbol": "A", "date": pd.bdate_range("2025-01-01", periods=30),
                           "close": 100.0, "near_min": 80.0, "near_max": 120.0,
                           "above": 0, "below": 0})
    meta = {"universe": 100, "complete": 1, "date_range": "2025-01..2025-06", "mde_21": 0.012}
    return {"events": ev, "car": car, "tests": tests, "regression": reg, "equity": eq,
            "ranges": ranges, "meta": meta}


def test_report_contains_every_section(tmp_path):
    path = report.build_report(_bundle(), str(tmp_path / "r.html"))
    html = open(path, encoding="utf-8").read()
    for title in ["Return paths after the event", "HIT minus MISS by horizon",
                  "Forward return by EXCESS tercile", "Volatility check",
                  "Event scatter", "Strike-range explorer", "All tests",
                  "Trading view", "Primary test"]:
        assert title in html, title
    assert "cdn.jsdelivr.net" in html
