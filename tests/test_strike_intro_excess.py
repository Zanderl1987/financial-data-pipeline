"""LOSO Poisson excess model: recovers a planted mechanical rule, no self-prediction."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import excess_model as xm


def _synthetic_panel(n_sym=6, n=300, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(n_sym):
        for t in range(n):
            for side in ("up", "down"):
                head = rng.uniform(0.0, 0.5)
                lam = np.exp(0.5 - 6.0 * head)          # mechanics: close to the edge -> listings
                rows.append({"symbol": f"S{s}", "date": t, "side": side, "headroom": head,
                             "move_toward": rng.normal(), "sigma60": rng.uniform(0.01, 0.03),
                             "earnings": 0, "days_since_monthly": rng.integers(0, 20),
                             "log_price": np.log(100), "y": rng.poisson(lam)})
    return pd.DataFrame(rows)


def test_loso_recovers_headroom_effect_and_every_row_is_out_of_fold():
    panel = _synthetic_panel()
    out = xm.fit_loso(panel)
    assert out["expected"].notna().all()
    near = out[out["headroom"] < 0.05]["expected"].mean()
    far = out[out["headroom"] > 0.45]["expected"].mean()
    assert near > 5 * far


def test_constant_feature_is_dropped_not_singular():
    panel = _synthetic_panel(n_sym=3, n=100)          # earnings and log_price are constant
    out = xm.fit_loso(panel)
    assert out["expected"].notna().all()


def test_event_excess_subtracts_expected_on_the_move_side():
    idx = pd.bdate_range("2026-01-01", periods=10)
    panel = pd.DataFrame({"symbol": "A", "date": [d.strftime("%Y-%m-%d") for d in idx[1:3]] * 2,
                          "side": ["up", "up", "down", "down"], "expected": [0.4, 0.6, 2.0, 2.0]})
    events = pd.DataFrame({"symbol": ["A"], "event_date": [idx[0]], "direction": [1],
                           "dir_intro": [3]})
    assert np.isclose(xm.event_excess(events, panel, idx).iloc[0], 3 - 1.0)
