"""
Backtesting engine — turn a cross-sectional signal into a performance record.

Closes the loop: signals.py produces (symbol, date, score); this module ranks
those scores into quantile portfolios, rebalances on a schedule, and reports an
equity curve plus risk/return metrics, benchmarked against equal-weight
buy-and-hold.

Look-ahead safety
-----------------
The signal known on rebalance date *t* sets the weights that earn the returns
of *t+1* onward (weights are shifted one day before being multiplied into
returns). A score computed from data as of *t* never earns the return of *t*.

Vectorized
----------
Portfolio returns are a single matrix product: a daily weight matrix
(forward-filled from rebalance dates, then lagged one day) times the daily
return matrix. No per-day Python loop over the holding period.

Usage
-----
    import backtest as bt
    from analytics import signal_panel

    sig = signal_panel(["AAPL", "MSFT", "NVDA", "AMD", "INTC"], start="2020-01-01")
    res = bt.backtest(sig, rebalance="M", quantiles=5, long_short=True)
    print(res.summary())
    res.equity            # equity curve (pandas Series)
    res.metrics           # dict of performance stats
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query as q
from analytics.features import _pick_price_table  # reuse price-source detection

_ANN = 252  # trading days per year


@dataclass
class BacktestResult:
    """Container for a backtest run. Inspect .metrics or call .summary()."""
    returns: pd.Series           # daily strategy returns
    equity: pd.Series            # cumulative growth of $1
    benchmark: pd.Series         # benchmark equity curve (equal-weight buy/hold)
    weights: pd.DataFrame        # daily weight matrix (date x symbol)
    metrics: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)

    def summary(self) -> pd.DataFrame:
        """One-column summary table of parameters + headline metrics."""
        rows = {**self.params, **self.metrics}
        return pd.DataFrame.from_dict(rows, orient="index", columns=["value"])

    def __repr__(self) -> str:
        m = self.metrics
        return (f"<BacktestResult CAGR={m.get('cagr_pct')}% "
                f"Sharpe={m.get('sharpe')} MaxDD={m.get('max_drawdown_pct')}% "
                f"vs bench CAGR={m.get('benchmark_cagr_pct')}%>")


def _returns_matrix(price_table, symbols, start, end) -> pd.DataFrame:
    """Wide daily-return matrix: rows=date, cols=symbol."""
    cols = q.schema(price_table)["column_name"].tolist()
    close_col = "adj_close" if "adj_close" in cols else "close"
    px = q.load(price_table, symbol=list(symbols), start=start, end=end,
                columns=["symbol", "date", close_col])
    if px.empty:
        return pd.DataFrame()
    px["date"] = pd.to_datetime(px["date"])
    wide = (px.drop_duplicates(["symbol", "date"])
              .pivot(index="date", columns="symbol", values=close_col)
              .sort_index())
    return wide.pct_change()


def _rebalance_dates(index: pd.DatetimeIndex, rebalance: str) -> pd.DatetimeIndex:
    """Last available trading day within each calendar period (D/W/M/Q)."""
    if rebalance.upper() in ("D", "DAILY"):
        return index
    freq = {"W": "W", "M": "ME", "Q": "QE"}.get(rebalance.upper(), "ME")
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.resample(freq).last().dropna().values)


def _target_weights(scores_wide: pd.DataFrame, rebal_dates, quantiles, long_short) -> pd.DataFrame:
    """
    Quantile weights at each rebalance date.

    Top 1/quantiles of symbols by score go long (equal weight summing to +1);
    if long_short, the bottom 1/quantiles go short (summing to -1).
    """
    out = pd.DataFrame(0.0, index=rebal_dates, columns=scores_wide.columns)
    for rd in rebal_dates:
        # most recent scores known on or before the rebalance date
        prior = scores_wide.loc[:rd]
        if prior.empty:
            continue
        s = prior.iloc[-1].dropna()
        n = len(s)
        if n < 2:
            continue
        k = max(1, int(round(n / quantiles)))
        ranked = s.sort_values(ascending=False)
        longs = ranked.index[:k]
        out.loc[rd, longs] = 1.0 / k
        if long_short:
            shorts = ranked.index[-k:]
            out.loc[rd, shorts] = out.loc[rd, shorts] - 1.0 / k
    return out


def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def backtest(
    signal: pd.DataFrame,
    score: str = "composite",
    price_table: "str | None" = None,
    quantiles: int = 5,
    rebalance: str = "M",
    long_short: bool = True,
    start: "str | None" = None,
    end: "str | None" = None,
    cost_bps: float = 0.0,
) -> BacktestResult:
    """
    Backtest a cross-sectional signal.

    Parameters
    ----------
    signal      : DataFrame with columns [symbol, date, <score>]
                  (e.g. the output of analytics.signal_panel)
    score       : the score column to rank on (default 'composite')
    price_table : price source override (default: auto-detect)
    quantiles   : number of buckets; top bucket goes long (default 5 = quintiles)
    rebalance   : 'D','W','M','Q' rebalance frequency (default monthly)
    long_short  : long top / short bottom bucket (True) or long-only (False)
    start, end  : restrict the backtest window
    cost_bps    : round-trip transaction cost in basis points applied to turnover

    Returns a BacktestResult.
    """
    if not {"symbol", "date", score}.issubset(signal.columns):
        raise ValueError(f"signal must have columns symbol, date, '{score}'")

    sig = signal[["symbol", "date", score]].copy()
    sig["date"] = pd.to_datetime(sig["date"])
    if start:
        sig = sig[sig["date"] >= pd.Timestamp(start)]
    if end:
        sig = sig[sig["date"] <= pd.Timestamp(end)]
    symbols = sig["symbol"].unique()

    pt = price_table or _pick_price_table(None, symbols=symbols)
    if pt is None:
        raise RuntimeError("No price table with data available for backtesting.")

    R = _returns_matrix(pt, symbols, start, end)
    if R.empty:
        raise RuntimeError(f"No price data in '{pt}' for the requested symbols/window.")

    scores_wide = (sig.drop_duplicates(["date", "symbol"])
                      .pivot(index="date", columns="symbol", values=score)
                      .reindex(columns=R.columns)
                      .sort_index())

    rebal_dates = _rebalance_dates(R.index, rebalance)
    target = _target_weights(scores_wide, rebal_dates, quantiles, long_short)

    # Daily weights: hold each rebalance's target until the next; lag one day so
    # weights set using info at date t earn returns from t+1.
    weights = (target.reindex(R.index).ffill().fillna(0.0)).shift(1).fillna(0.0)

    gross = (weights * R).sum(axis=1)

    # Transaction costs: turnover (sum of absolute weight changes) * cost.
    turnover = weights.diff().abs().sum(axis=1).fillna(0.0)
    costs = turnover * (cost_bps / 1e4)
    net = gross - costs

    equity = (1.0 + net).cumprod()

    # Benchmark: equal-weight buy-and-hold of the same universe.
    bench_ret = R.mean(axis=1).fillna(0.0)
    benchmark = (1.0 + bench_ret).cumprod()

    metrics = _compute_metrics(net, equity, benchmark, bench_ret, weights, turnover, long_short)

    params = {
        "price_table": pt, "score": score, "quantiles": quantiles,
        "rebalance": rebalance, "long_short": long_short, "cost_bps": cost_bps,
        "n_symbols": len(symbols), "n_days": len(net),
        "start": str(R.index.min().date()), "end": str(R.index.max().date()),
    }
    return BacktestResult(returns=net, equity=equity, benchmark=benchmark,
                          weights=weights, metrics=metrics, params=params)


def _ann_metrics(ret: pd.Series, equity: pd.Series) -> dict:
    n = len(ret)
    if n == 0:
        return {}
    total = float(equity.iloc[-1] - 1.0)
    cagr = float(equity.iloc[-1] ** (_ANN / n) - 1.0) if equity.iloc[-1] > 0 else float("nan")
    vol = float(ret.std(ddof=0) * np.sqrt(_ANN))
    mean_ann = float(ret.mean() * _ANN)
    sharpe = mean_ann / vol if vol > 0 else float("nan")
    return {"total_return_pct": round(100 * total, 2),
            "cagr_pct": round(100 * cagr, 2),
            "ann_vol_pct": round(100 * vol, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(100 * _max_drawdown(equity), 2)}


def _compute_metrics(net, equity, benchmark, bench_ret, weights, turnover, long_short) -> dict:
    m = _ann_metrics(net, equity)
    b = _ann_metrics(bench_ret, benchmark)
    m["benchmark_cagr_pct"] = b.get("cagr_pct")
    m["benchmark_sharpe"] = b.get("sharpe")
    m["excess_cagr_pct"] = (round(m["cagr_pct"] - b["cagr_pct"], 2)
                            if m.get("cagr_pct") is not None and b.get("cagr_pct") is not None
                            else None)
    m["hit_rate_pct"] = round(100 * float((net > 0).mean()), 1) if len(net) else None
    m["avg_turnover"] = round(float(turnover[turnover > 0].mean()), 3) if (turnover > 0).any() else 0.0
    active = weights.iloc[-1]
    m["n_long"] = int((active > 0).sum())
    m["n_short"] = int((active < 0).sum())
    return m
