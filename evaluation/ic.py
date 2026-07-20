"""
evaluation/ic.py -- level-IC evaluation of a continuous signal panel.
Generalizes tv_rating_eval.evaluate_signal: pooled + daily cross-sectional
IC and a cross-sectional quantile bucket spread, per horizon.
"""

import pandas as pd

from evaluation.data import HORIZONS
from evaluation import stats as ev_stats


def evaluate_ic(panel: pd.DataFrame, direction: int = 1, horizons=HORIZONS,
                min_names: int = 5, q: float = 0.2) -> dict:
    """
    direction=+1 evaluates `value` as higher-is-better; -1 evaluates -value
    (so a GOOD contrarian signal reports positive oriented IC); 0 evaluates
    raw values with no orientation.
    """
    work = panel.copy()
    vcol = "value"
    if direction == -1:
        work["_oriented_value"] = -work["value"]
        vcol = "_oriented_value"
    out = {}
    for h in horizons:
        fcol = f"fwd_{h}d"
        if fcol not in work.columns:
            continue
        res = {}
        sub = work.dropna(subset=[vcol, fcol])
        res.update(ev_stats.pooled_ic(sub[vcol], sub[fcol]))
        res.update(ev_stats.daily_ic(work, vcol, fcol, min_names=min_names))
        res.update(ev_stats.quantile_spread(work, vcol, fcol, q=q))
        res["oriented"] = int(direction)
        out[h] = res
    return out
