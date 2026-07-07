"""
test_exposure.py — verify the driver-exposure map (analytics/exposure.py).

No API keys or data files required.  Tests confirm:
  - the DRIVERS registry is well-formed and covers the documented defaults
  - compute_exposure() recovers a known beta from synthetic returns
  - the market control isolates the true driver beta when co-movement
    is entirely market-driven
  - insufficient/degenerate inputs return None instead of noise
"""

import sys
import os
import inspect

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from analytics import exposure as ex


class TestDriverRegistry:
    def test_drivers_well_formed(self):
        for name, spec in ex.DRIVERS.items():
            assert isinstance(name, str) and name == name.lower()
            table, symbol = spec
            assert table in ("futures", "cboe_volatility")
            assert isinstance(symbol, str) and symbol

    def test_defaults_are_registered(self):
        for d in ex.DEFAULT_DRIVERS:
            assert d in ex.DRIVERS
        assert ex.MARKET_DRIVER in ex.DRIVERS

    def test_exposure_map_params(self):
        sig = list(inspect.signature(ex.exposure_map).parameters.keys())
        for p in ("symbols", "drivers", "start", "end", "market", "min_obs"):
            assert p in sig

    def test_unknown_driver_raises(self):
        try:
            ex.load_driver_returns(["not_a_driver"])
        except KeyError:
            return
        raise AssertionError("expected KeyError for unknown driver")


def _dates(n):
    return pd.bdate_range("2020-01-02", periods=n)


class TestComputeExposure:
    def test_recovers_known_beta(self):
        rng = np.random.default_rng(3)
        idx = _dates(500)
        drv = pd.Series(rng.normal(0, 0.02, 500), index=idx)
        stock = 0.5 * drv + pd.Series(rng.normal(0, 0.002, 500), index=idx)
        res = ex.compute_exposure(stock, drv)
        assert res is not None
        assert abs(res["beta"] - 0.5) < 0.05
        assert res["t_stat"] > 10
        assert res["r2"] > 0.8
        assert res["n"] == 500

    def test_market_control_removes_spurious_exposure(self):
        # stock and driver both load on the market but NOT on each other:
        # raw beta is significant, beta_ex_mkt should be ~0
        rng = np.random.default_rng(11)
        idx = _dates(1000)
        mkt = pd.Series(rng.normal(0, 0.01, 1000), index=idx)
        drv = 0.8 * mkt + pd.Series(rng.normal(0, 0.005, 1000), index=idx)
        stock = 1.2 * mkt + pd.Series(rng.normal(0, 0.005, 1000), index=idx)
        res = ex.compute_exposure(stock, drv, market_ret=mkt)
        assert res["t_stat"] > 3                # raw looks exposed
        assert abs(res["t_ex_mkt"]) < 3         # control reveals it's the market

    def test_market_control_keeps_true_exposure(self):
        rng = np.random.default_rng(21)
        idx = _dates(1000)
        mkt = pd.Series(rng.normal(0, 0.01, 1000), index=idx)
        drv = pd.Series(rng.normal(0, 0.02, 1000), index=idx)
        stock = 1.0 * mkt + 0.3 * drv + pd.Series(rng.normal(0, 0.003, 1000), index=idx)
        res = ex.compute_exposure(stock, drv, market_ret=mkt)
        assert abs(res["beta_ex_mkt"] - 0.3) < 0.05
        assert res["t_ex_mkt"] > 10

    def test_insufficient_overlap_returns_none(self):
        idx = _dates(50)
        s = pd.Series(np.random.default_rng(1).normal(size=50), index=idx)
        assert ex.compute_exposure(s, s, min_obs=120) is None

    def test_zero_variance_driver_returns_none(self):
        idx = _dates(300)
        stock = pd.Series(np.random.default_rng(2).normal(size=300), index=idx)
        flat = pd.Series(0.0, index=idx)
        assert ex.compute_exposure(stock, flat) is None
