"""Expected-range helpers for the paper-trading forward test."""
from __future__ import annotations

import numpy as np
import pandas as pd


def normal_band(equity: pd.Series, start_equity: float, max_h: int = 63) -> pd.DataFrame:
    """p5/p50/p95 of (equity[t+h]-equity[t])/start_equity over all rolling windows."""
    v = equity.to_numpy(float)
    rows = {}
    for h in range(1, max_h + 1):
        d = (v[h:] - v[:-h]) / start_equity
        d = d[np.isfinite(d)]
        rows[h] = np.percentile(d, [5, 50, 95]) if len(d) else [np.nan] * 3
    return pd.DataFrame.from_dict(rows, orient="index", columns=["p5", "p50", "p95"])
