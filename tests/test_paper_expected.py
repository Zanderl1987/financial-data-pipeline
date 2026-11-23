import numpy as np
import pandas as pd

from paper.expected import normal_band


def test_band_linear_equity():
    eq = pd.Series(5000.0 + np.arange(300), index=pd.bdate_range("2020-01-01", periods=300))
    b = normal_band(eq, 5000.0)
    h = np.arange(1, 64)
    for col in ("p5", "p50", "p95"):
        assert np.allclose(b[col].to_numpy(), h / 5000.0)


def test_band_shape():
    rng = np.random.default_rng(0)
    eq = pd.Series(5000.0 + rng.normal(0, 20, 400).cumsum(),
                   index=pd.bdate_range("2020-01-01", periods=400))
    b = normal_band(eq, 5000.0)
    assert list(b.index) == list(range(1, 64))
    assert list(b.columns) == ["p5", "p50", "p95"]
    assert (b.p5 <= b.p50).all() and (b.p50 <= b.p95).all()
