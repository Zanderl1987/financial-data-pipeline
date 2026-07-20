"""
evaluation/events.py -- event-study evaluation of an EventSet frame, one
study per label. Wraps event_backtest.event_study; entry_lag=1 keeps the
engine's next-close entry rule (day 0 is the first trading close AFTER the
lag-applied event date).
"""

import numpy as np
import pandas as pd


def _json_safe(v):
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return f if np.isfinite(f) else None
    return v


def evaluate_events(frame: pd.DataFrame, min_events: int = 5,
                    benchmark: str = "SPY", window=(0, 21),
                    entry_lag: int = 1, price_table=None) -> dict:
    """frame: LAG-APPLIED event frame (symbol, date, label[, magnitude])."""
    import event_backtest as eb         # local import: repo test convention
    out = {"labels": {}, "skipped": {}}
    for label, grp in frame.groupby("label"):
        if len(grp) < min_events:
            out["skipped"][str(label)] = int(len(grp))
            continue
        try:
            res = eb.event_study(grp[["symbol", "date"]], window=window,
                                 benchmark=benchmark, entry_lag=entry_lag,
                                 price_table=price_table)
        except RuntimeError as exc:
            out["skipped"][str(label)] = str(exc)
            continue
        out["labels"][str(label)] = {
            "n_events": int(res.n_events),
            "horizons": {str(h): {k: _json_safe(v) for k, v in row.items()}
                         for h, row in res.horizons.iterrows()},
            "mean_car_pct": {str(int(d)): round(100 * float(v), 3)
                             for d, v in res.mean_car.items()
                             if np.isfinite(v)},
        }
    return out
