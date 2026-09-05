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
from evaluation import execution as ev_execution
from evaluation.stats import _degenerate_sd

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


def _adv_participation_cost(weights: pd.DataFrame, aum: float, coeff,
                            adv_window: int, symbols, start, end,
                            price_table: "str | None",
                            returns: "pd.DataFrame | None" = None) -> pd.Series:
    """
    Per-symbol participation-based sqrt market-impact cost, as a fraction of
    `aum` per day: for each symbol traded on a rebalance day, cost_rate =
    coeff/1e4 * sqrt(dollar_traded / trailing_adv), charged on that symbol's
    own dollar amount traded, summed across symbols.

    The SAME model event_backtest.scenario()'s adv_impact_coeff already
    uses (a real function of a symbol's own liquidity) -- reused here, not
    reinvented -- applied to REBALANCE-DAY WEIGHT CHANGES instead of
    per-event notional. Deliberately a DIFFERENT parameter from this
    engine's existing adv_impact_coeff (a function of PORTFOLIO-level
    turnover with no per-symbol liquidity concept -- see evaluation/
    execution.py's docstring on why backtest.py's and event_backtest.py's
    two meanings of "sqrt_impact" are kept apart). Naming this
    adv_participation_coeff avoids repeating that exact ambiguity a third
    way.

    `aum` translates a fractional weight CHANGE into a dollar amount traded
    -- this engine has no other concept of portfolio dollar size (it works
    entirely in returns/weights), so aum is meaningful ONLY for this cost
    term, nowhere else in backtest().

    ADV is trailing `adv_window` days, shifted one day -- today's
    participation is measured against liquidity known BEFORE today's own
    (not-yet-observed) volume, the same look-ahead-safety convention
    event_backtest.load_dollar_volume()'s callers already use.

    coeff may ALSO be the string "sqrt_law" (same participation surface, but
    the flat scalar is replaced by the calibrated square-root-law form
    `event_backtest.ADV_SQRT_LAW_K * realized_daily_vol_bps * sqrt(p)`):
    realized daily vol per symbol per day comes from `returns`, measured over
    the trailing `adv_window` days SHIFTED one day (PIT -- today's own
    not-yet-realized return excluded), exactly like ADV itself. Requires
    `returns` (the engine already holds it; re-passing avoids re-deriving it
    from prices). The legacy numeric path is byte-identical when `returns`
    is unused/the scalar form is chosen.
    """
    import event_backtest as eb

    dollar_change = weights.diff().abs() * aum
    volumes = eb.load_dollar_volume_matrix(list(symbols), start=start, end=end,
                                           price_table=price_table)
    volumes = volumes.reindex(index=weights.index, columns=weights.columns)
    adv = volumes.rolling(adv_window, min_periods=adv_window).mean().shift(1)
    participation = (dollar_change / adv).replace([np.inf, -np.inf], np.nan)
    if coeff == "sqrt_law":
        if returns is None:
            raise ValueError("sqrt_law mode requires the returns matrix "
                             "(returns=...) to measure per-symbol realized "
                             "daily volatility")
        realized_vol = returns.rolling(
            adv_window, min_periods=adv_window).std().shift(1)
        cost_rate = (eb.ADV_SQRT_LAW_K * realized_vol
                     * np.sqrt(participation.clip(lower=0)))
    else:
        cost_rate = (coeff / 1e4) * np.sqrt(participation.clip(lower=0))
    cost_dollars = (cost_rate * dollar_change).fillna(0.0).sum(axis=1)
    return cost_dollars / aum


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
    spread_bps: float = 0.0,
    borrow_fee_bps: float = 0.0,
    slippage_model: "str | None" = None,
    adv_impact_coeff: float = 0.1,
    adv_participation_coeff: "float | str | None" = None,
    aum: float = 1_000_000.0,
    adv_window: int = 20,
    vol_target: "float | None" = None,
    max_weight: "float | None" = None,
    max_drawdown_stop: "float | None" = None,
) -> BacktestResult:
    """
    Backtest a cross-sectional signal with advanced execution costs, risk controls,
    and performance metrics.

    adv_participation_coeff (opt-in, default None -- no behavior change unless
    set): a real per-symbol ADV market-impact cost, on top of (not instead of)
    the existing portfolio-turnover-based adv_impact_coeff/slippage_model. See
    _adv_participation_cost()'s docstring for the model and why it's a
    separate parameter. `aum`/`adv_window` are only meaningful when this is set.
    Set it to the string "sqrt_law" to replace the flat scalar with the
    calibration-backed square-root-law form `ADV_SQRT_LAW_K * realized_daily_
    vol_bps * sqrt(p)` (per-symbol realized vol measured PIT from `R`); any
    other string raises.
    """
    if not {"symbol", "date", score}.issubset(signal.columns):
        raise ValueError(f"signal must have columns symbol, date, '{score}'")

    if adv_participation_coeff is not None and adv_participation_coeff != "sqrt_law" \
            and not isinstance(adv_participation_coeff, (int, float)):
        raise ValueError(f"adv_participation_coeff must be a number, "
                         f"'sqrt_law', or None; got {adv_participation_coeff!r}")

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

    # Position sizing cap constraint (max_weight)
    if max_weight is not None and max_weight > 0:
        target = target.clip(lower=-abs(max_weight), upper=abs(max_weight))

    # Daily weights: hold each rebalance's target until the next; lag one day so
    # weights set using info at date t earn returns from t+1.
    weights = (target.reindex(R.index).ffill().fillna(0.0)).shift(1).fillna(0.0)

    # Dynamic Volatility Targeting
    if vol_target is not None and vol_target > 0:
        raw_gross = (weights * R).sum(axis=1)
        # shift(1): today's scale must come from vol estimated through
        # yesterday's close, not today's own (not-yet-realized) return.
        rolling_vol = (raw_gross.rolling(window=21, min_periods=5).std() * np.sqrt(_ANN)).shift(1)
        scale = (vol_target / rolling_vol).replace([np.inf, -np.inf], 1.0).fillna(1.0).clip(upper=2.0)
        weights = weights.mul(scale, axis=0)

    # Execute daily gross return
    gross = (weights * R).sum(axis=1)

    # Transaction & execution costs. The arithmetic lives in
    # evaluation/execution.py so this engine and event_backtest.py share one
    # definition of a cost rate; see that module's docstring for why the two
    # meanings of "sqrt_impact" are kept apart rather than merged.
    turnover = weights.diff().abs().sum(axis=1).fillna(0.0)
    short_exposure = weights.clip(upper=0.0).abs().sum(axis=1)
    cost_model = ev_execution.costs_from_legacy_kwargs(
        cost_bps=cost_bps, spread_bps=spread_bps, borrow_fee_bps=borrow_fee_bps,
        slippage_model=slippage_model, impact_coeff=adv_impact_coeff,
    )
    costs = ev_execution.daily_cost(cost_model, turnover, short_exposure, ann=_ANN)
    if adv_participation_coeff is not None and (
            adv_participation_coeff == "sqrt_law"
            or (isinstance(adv_participation_coeff, (int, float))
                and adv_participation_coeff > 0)):
        costs = costs + _adv_participation_cost(
            weights, aum, adv_participation_coeff, adv_window, symbols,
            start, end, pt, returns=R)

    net = gross - costs

    # Portfolio Drawdown Circuit Breaker
    if max_drawdown_stop is not None and max_drawdown_stop > 0:
        equity_tmp = (1.0 + net).cumprod()
        peak_tmp = equity_tmp.cummax()
        dd_tmp = equity_tmp / peak_tmp - 1.0
        stopped_out = dd_tmp < -abs(max_drawdown_stop)
        if stopped_out.any():
            # The breach is only knowable at the close of the day it happens,
            # so that day's already-realized return must stand -- flattening
            # starts the following trading day, not stop_idx itself (zeroing
            # stop_idx would retroactively erase a loss with foresight).
            stop_pos = net.index.get_loc(stopped_out.idxmax())
            if stop_pos + 1 < len(net):
                net.iloc[stop_pos + 1:] = 0.0
                weights.iloc[stop_pos + 1:] = 0.0

    equity = (1.0 + net).cumprod()

    # Benchmark: equal-weight buy-and-hold of the same universe.
    bench_ret = R.mean(axis=1).fillna(0.0)
    benchmark = (1.0 + bench_ret).cumprod()

    metrics = _compute_metrics(net, equity, benchmark, bench_ret, weights, turnover, long_short, start, end)

    params = {
        "price_table": pt, "score": score, "quantiles": quantiles,
        "rebalance": rebalance, "long_short": long_short, "cost_bps": cost_bps,
        "spread_bps": spread_bps, "borrow_fee_bps": borrow_fee_bps,
        "slippage_model": slippage_model or "none",
        "adv_participation_coeff": adv_participation_coeff,
        "aum": aum if adv_participation_coeff else None,
        "adv_window": adv_window if adv_participation_coeff else None,
        "vol_target": vol_target, "max_weight": max_weight,
        "max_drawdown_stop": max_drawdown_stop,
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
    sharpe = mean_ann / vol if not _degenerate_sd(vol) else float("nan")
    return {"total_return_pct": round(100 * total, 2),
            "cagr_pct": round(100 * cagr, 2),
            "ann_vol_pct": round(100 * vol, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(100 * _max_drawdown(equity), 2)}


def _compute_metrics(net, equity, benchmark, bench_ret, weights, turnover, long_short,
                     start=None, end=None) -> dict:
    from evaluation import stats as ev_stats

    m = _ann_metrics(net, equity)
    b = _ann_metrics(bench_ret, benchmark)
    m["benchmark_cagr_pct"] = b.get("cagr_pct")
    m["benchmark_sharpe"] = b.get("sharpe")
    m["excess_cagr_pct"] = (round(m["cagr_pct"] - b["cagr_pct"], 2)
                            if m.get("cagr_pct") is not None and b.get("cagr_pct") is not None
                            else None)
    m["hit_rate_pct"] = round(100 * float((net > 0).mean()), 1) if len(net) else None
    m["avg_turnover"] = round(float(turnover[turnover > 0].mean()), 3) if (turnover > 0).any() else 0.0

    # Risk & Ratio Extensions
    sortino_res = ev_stats.sortino_ratio(net)
    m["sortino"] = sortino_res.get("sortino")

    calmar_res = ev_stats.calmar_ratio(m.get("cagr_pct"), m.get("max_drawdown_pct"))
    m["calmar"] = calmar_res.get("calmar")

    omega_res = ev_stats.omega_ratio(net)
    m["omega"] = omega_res.get("omega")

    var_res = ev_stats.value_at_risk(net)
    m["var_95_pct"] = var_res.get("var_95_pct")

    cvar_res = ev_stats.conditional_var(net)
    m["cvar_95_pct"] = cvar_res.get("cvar_95_pct")

    gtp_res = ev_stats.gain_to_pain_ratio(net)
    m["gain_to_pain"] = gtp_res.get("gain_to_pain")

    ff_res = ev_stats.fama_french_factor_attribution(net, start=start, end=end)
    m["ff_alpha_ann"] = ff_res.get("ff_alpha_ann")
    m["ff_r_squared"] = ff_res.get("ff_r_squared")

    active = weights.iloc[-1]
    m["n_long"] = int((active > 0).sum())
    m["n_short"] = int((active < 0).sum())
    return m

