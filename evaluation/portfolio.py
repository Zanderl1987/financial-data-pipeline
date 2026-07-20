"""
evaluation/portfolio.py -- quantile-portfolio evaluation of a signal frame.
Thin wrapper over backtest.backtest (which already lags weights one day, so
weights set with info at t earn returns from t+1 -- PIT-safe by construction).
"""

import math

import pandas as pd


def evaluate_portfolio(frame: pd.DataFrame, direction: int = 1,
                       quantiles: int = 5, rebalance: str = "M",
                       long_short: bool = True, start=None, end=None,
                       price_table=None, cost_bps: float = 0.0):
    """frame: LAG-APPLIED signal frame (symbol, date, value). Returns BacktestResult."""
    import backtest as bt               # local import: repo test convention
    df = frame[["symbol", "date", "value"]].copy()
    if direction == -1:
        df["value"] = -df["value"]
    return bt.backtest(df, score="value", quantiles=quantiles,
                       rebalance=rebalance, long_short=long_short,
                       start=start, end=end, price_table=price_table,
                       cost_bps=cost_bps)


def _json_safe(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def summarize_portfolio(res) -> dict:
    """JSON-safe {metrics, params} from a BacktestResult."""
    return {"metrics": {k: _json_safe(v) for k, v in res.metrics.items()},
            "params": {k: _json_safe(v) for k, v in res.params.items()}}
