"""Battery calibration: detects a planted effect, stays quiet on noise, survives tiny groups."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strike_intro import battery as bt


def test_planted_effect_is_detected_by_every_test():
    rng = np.random.default_rng(0)
    a, b = rng.normal(0.02, 0.05, 400), rng.normal(0.0, 0.05, 400)
    r = bt.compare_groups(a, b, n_perm=2000, n_boot=500)
    assert r["diff"] > 0.01 and r["ci_lo"] > 0
    for k in ("p_welch", "p_perm", "p_boot", "p_mw"):
        assert r[k] < 0.01, k


def test_null_p_values_are_roughly_uniform():
    rng = np.random.default_rng(1)
    ps = [bt.compare_groups(rng.normal(0, 1, 60), rng.normal(0, 1, 60),
                            n_perm=300, n_boot=100, seed=i)["p_perm"] for i in range(200)]
    ps = np.array(ps)
    assert 0.02 < (ps < 0.05).mean() < 0.10
    assert 0.40 < np.median(ps) < 0.60


def test_tiny_group_gives_nan_p_values_not_zero():
    r = bt.compare_groups(np.array([0.1, 0.2, 0.3]), np.random.default_rng(2).normal(0, 1, 50))
    assert r["n_a"] == 3 and np.isnan(r["p_perm"]) and np.isnan(r["p_welch"])


def test_permutation_respects_strata():
    # Group a is all in stratum 0, which has a high mean for both groups: shuffling
    # within strata must not call that a group effect.
    rng = np.random.default_rng(3)
    a = rng.normal(1.0, 0.1, 100)
    b = np.r_[rng.normal(1.0, 0.1, 100), rng.normal(0.0, 0.1, 100)]
    r = bt.compare_groups(a, b, strata_a=np.zeros(100), strata_b=np.r_[np.zeros(100), np.ones(100)],
                          n_perm=2000, n_boot=200)
    assert r["p_perm"] > 0.05


def test_mde_shrinks_with_n():
    assert bt.mde(1000, 1000, 0.05) < bt.mde(100, 100, 0.05)


def test_clustered_ols_drops_constant_column():
    rng = np.random.default_rng(4)
    df = pd.DataFrame({"y": rng.normal(size=200), "hit": rng.integers(0, 2, 200),
                       "earn": 0, "entry_date": np.repeat(np.arange(40), 5),
                       "symbol": np.tile(list("ABCDE"), 40)})
    out = bt.clustered_ols(df, "y", ["hit", "earn"])
    assert set(out["term"]) == {"const", "hit"}
