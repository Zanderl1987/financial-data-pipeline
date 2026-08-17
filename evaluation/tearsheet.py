"""
evaluation/tearsheet.py -- performance-analytics layer (W3).

Pure computation: pandas/numpy/scipy in, dicts and DataFrames out. NO plotting
import lives here. generate_tearsheet.py renders; this module never draws, which
is what lets W4's interactive callbacks call exactly the same functions the
static HTML does.

The repo could already say whether a result is significant. This says what
holding it would have felt like: when the returns arrived, how long the bad
stretches lasted, and what the strategy adds over its benchmark.

House rule from stats.py carries over: a statistic whose assumptions fail
returns None plus a '*_reason' string, and nothing divides by a zero sd.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

#: Relative floor below which a standard deviation is treated as zero.
#:
#: `sd > 0` is NOT a sufficient guard, which is the trap this constant exists
#: for. A constant series of 0.001 has an arithmetically-zero sd, but in float64
#: it comes out around 6e-19 rather than exactly 0.0 -- positive, finite, and
#: enough to produce a Sharpe of 2.4e16. That passes every `> 0` check in the
#: repo and would render as a plausible-looking huge number rather than as the
#: degenerate input it is. Caught by tests/test_tearsheet.py.
SD_FLOOR = 1e-12


def _degenerate_sd(sd: float, scale: float = 1.0) -> bool:
    """True when `sd` is zero, non-finite, or float-noise around zero."""
    return not (np.isfinite(sd) and sd > SD_FLOOR * max(1.0, abs(scale)))


# --------------------------------------------------------------- helpers


def _clean(returns) -> pd.Series:
    """Daily return Series with a sorted DatetimeIndex and no NaNs."""
    s = pd.Series(returns).dropna()
    if s.empty:
        return s
    s.index = pd.to_datetime(s.index)
    return s.sort_index().astype(float)


def to_equity(returns, starting: float = 1.0) -> pd.Series:
    """Compounded equity curve from daily returns."""
    s = _clean(returns)
    if s.empty:
        return s
    return starting * (1.0 + s).cumprod()


def _ann_vol(s: pd.Series) -> float:
    return float(s.std(ddof=1) * math.sqrt(TRADING_DAYS))


# --------------------------------------------------------------- trade bridge


def daily_returns_from_trades(trades: pd.DataFrame, *,
                              starting_equity: float = 100_000.0) -> dict:
    """
    Daily return series from realized trades, on a REALIZED basis.

    Each trade's P&L lands on its exit date; equity is
    starting_equity + cumsum(realized P&L); the series is reindexed to a
    business-day calendar with 0.0 on days nothing closed.

    THIS IS NOT MARK-TO-MARKET, and the difference is not cosmetic. A position
    sitting 40% underwater contributes nothing to this curve until the day it
    closes, so the drawdown computed from it is a LOWER BOUND on the drawdown
    actually experienced. The series is also spiky in a way that makes its
    Sharpe non-comparable to a mark-to-market Sharpe out of backtest.py.

    The returned dict carries basis="realized" for exactly that reason: a
    downstream consumer should not be able to put these two side by side
    without the difference being visible. A true mark-to-market curve needs
    per-day position valuation the trade engine does not retain today.
    """
    if trades is None or trades.empty:
        return {"returns": None, "basis": "realized",
                "returns_reason": "no realized trades"}
    need = {"exit_date", "pnl_dollars"}
    missing = need - set(trades.columns)
    if missing:
        return {"returns": None, "basis": "realized",
                "returns_reason": f"trades missing columns {sorted(missing)}"}

    df = trades[["exit_date", "pnl_dollars"]].dropna()
    if df.empty:
        return {"returns": None, "basis": "realized",
                "returns_reason": "no trades with an exit date"}

    by_day = (df.assign(exit_date=pd.to_datetime(df["exit_date"]))
                .groupby("exit_date")["pnl_dollars"].sum().sort_index())
    calendar = pd.bdate_range(by_day.index.min(), by_day.index.max())
    pnl = by_day.reindex(calendar).fillna(0.0)

    equity = starting_equity + pnl.cumsum()
    if (equity <= 0).any():
        return {"returns": None, "basis": "realized",
                "returns_reason": "equity hit zero; returns undefined"}

    prev = equity.shift(1).fillna(starting_equity)
    returns = (equity / prev) - 1.0
    return {"returns": returns, "basis": "realized",
            "n_trades": int(len(df)),
            "n_days": int(len(returns)),
            "starting_equity": float(starting_equity),
            "final_equity": round(float(equity.iloc[-1]), 2)}


# --------------------------------------------------------------- monthly table


def monthly_returns_table(returns) -> dict:
    """
    Compounded monthly returns pivoted year x month, plus a YTD column.

    Percent units. Partial first/last months are included as-is, and n_months
    reports how many months are real -- a three-month backtest should not be
    able to look like a year of evidence just because it spans a year boundary.
    """
    s = _clean(returns)
    if s.empty:
        return {"table": None, "monthly_reason": "no returns"}

    monthly = (1.0 + s).resample("ME").prod() - 1.0
    if monthly.empty:
        return {"table": None, "monthly_reason": "no complete months"}

    frame = pd.DataFrame({"year": monthly.index.year,
                          "month": monthly.index.month,
                          "ret": monthly.to_numpy() * 100.0})
    table = frame.pivot(index="year", columns="month", values="ret")
    table = table.reindex(columns=range(1, 13))
    table.columns = MONTH_NAMES
    ytd = frame.groupby("year")["ret"].apply(
        lambda r: (np.prod(1.0 + r.to_numpy() / 100.0) - 1.0) * 100.0)
    table["YTD"] = ytd
    table = table.round(2)

    vals = monthly.to_numpy()
    return {"table": table,
            "n_months": int(len(monthly)),
            "best_month_pct": round(float(vals.max()) * 100.0, 2),
            "worst_month_pct": round(float(vals.min()) * 100.0, 2),
            "pct_positive_months": round(100.0 * float((vals > 0).mean()), 1)}


# --------------------------------------------------------------- rolling


def rolling_metrics(returns, window: int = 63) -> dict:
    """
    Annualized rolling Sharpe, Sortino and volatility over a trailing window
    (63 ~ one quarter).

    Windows with a degenerate standard deviation yield NaN, never inf or a huge
    float-noise artifact -- see SD_FLOOR. An inf (or 2e16) Sharpe on a flat
    stretch would dominate every chart it appears in.
    """
    s = _clean(returns)
    if window < 5:
        return {"frame": None, "rolling_reason": f"window must be >= 5 (got {window})"}
    if len(s) < 2 * window:
        return {"frame": None,
                "rolling_reason": f"only {len(s)} days (< {2 * window})"}

    ann = math.sqrt(TRADING_DAYS)
    roll = s.rolling(window)
    mean = roll.mean()
    sd = roll.std(ddof=1)
    vol = sd * ann
    sharpe = (mean / sd.where(sd > SD_FLOOR) * ann)

    def _downside(x: np.ndarray) -> float:
        neg = x[x < 0.0]
        if len(neg) == 0:
            return np.nan
        return float(np.sqrt(np.mean(neg ** 2)))

    dd = s.rolling(window).apply(_downside, raw=True)
    sortino = (mean / dd.where(dd > SD_FLOOR) * ann)

    frame = pd.DataFrame({"rolling_sharpe": sharpe,
                          "rolling_sortino": sortino,
                          "rolling_vol_pct": vol * 100.0}).dropna(how="all")
    return {"frame": frame, "window": int(window),
            "n_windows": int(frame["rolling_sharpe"].notna().sum())}


# --------------------------------------------------------------- drawdown


def drawdown_series(returns) -> pd.Series:
    """Underwater series (percent, <= 0) from daily returns."""
    eq = to_equity(returns)
    if eq.empty:
        return eq
    return (eq / eq.cummax() - 1.0) * 100.0


def drawdown_periods(returns, top_n: int = 5) -> dict:
    """
    One row per drawdown episode, deepest first.

    An episode still underwater at the end of the sample is reported as
    recovered = False, with recovery_date NaT and days_to_recovery pd.NA. It is
    NOT closed off at the last bar: doing so would report a recovery that never
    happened, and it would do so precisely in the sample where the drawdown
    matters most -- the one that is still going.

    COLUMN TYPES ARE PINNED, deliberately. Left to pandas' inference, an
    all-unrecovered table keeps Python None in an object column while a mixed
    table coerces to NaT/NaN -- so a downstream `is None` check works on the
    synthetic case and silently renders "NaT" on real data. `recovered` (a
    plain bool) is the flag to branch on; the date/int columns are always
    datetime64 and Int64 so `pd.isna` works uniformly.
    """
    eq = to_equity(returns)
    if eq.empty:
        return {"table": None, "dd_reason": "no returns"}

    under = eq < eq.cummax() - 1e-15
    if not under.any():
        return {"table": pd.DataFrame(columns=[
            "peak_date", "valley_date", "recovery_date", "depth_pct",
            "days_to_valley", "days_to_recovery", "total_days", "recovered"]),
            "n_periods": 0, "max_drawdown_pct": 0.0}

    peaks = eq.cummax()
    rows = []
    i, n = 0, len(eq)
    idx = eq.index
    while i < n:
        if not under.iloc[i]:
            i += 1
            continue
        start = i                                  # first day below the peak
        peak_i = start - 1 if start > 0 else 0     # the peak it fell from
        while i < n and under.iloc[i]:
            i += 1
        end = i                                    # first recovered day, or n
        seg = eq.iloc[start:end]
        valley_pos = int(np.argmin(seg.to_numpy()))
        valley_i = start + valley_pos
        depth = float(seg.iloc[valley_pos] / peaks.iloc[start] - 1.0) * 100.0
        recovered = end < n
        rows.append({
            "peak_date": idx[peak_i],
            "valley_date": idx[valley_i],
            "recovery_date": idx[end] if recovered else None,
            "depth_pct": round(depth, 2),
            "days_to_valley": int((idx[valley_i] - idx[peak_i]).days),
            "days_to_recovery": (int((idx[end] - idx[valley_i]).days)
                                 if recovered else None),
            "total_days": (int((idx[end] - idx[peak_i]).days)
                           if recovered else int((idx[-1] - idx[peak_i]).days)),
            "recovered": bool(recovered),
        })

    table = (pd.DataFrame(rows).sort_values("depth_pct")
             .head(top_n).reset_index(drop=True))
    table = table.astype({"recovery_date": "datetime64[ns]",
                          "days_to_recovery": "Int64",
                          "days_to_valley": "Int64",
                          "total_days": "Int64",
                          "recovered": "bool"})
    return {"table": table, "n_periods": len(rows),
            "max_drawdown_pct": round(float(table["depth_pct"].min()), 2),
            "n_unrecovered": int((~pd.DataFrame(rows)["recovered"]).sum())}


# --------------------------------------------------------------- benchmark


def benchmark_stats(returns, bench_returns, rf: float = 0.0) -> dict:
    """
    Strategy vs benchmark: annualized alpha, beta, R^2, correlation, tracking
    error, information ratio, and up/down capture.

    Alignment is an INNER JOIN on date and n_overlap is reported. A benchmark
    covering half the period should be visible as covering half the period, not
    silently forward-filled into looking complete.
    """
    s = _clean(returns)
    b = _clean(bench_returns)
    if s.empty or b.empty:
        return {"beta": None, "bench_reason": "empty strategy or benchmark series"}

    joined = pd.concat([s.rename("s"), b.rename("b")], axis=1,
                       join="inner").dropna()
    n = len(joined)
    if n < 30:
        return {"beta": None,
                "bench_reason": f"only {n} overlapping days (< 30)"}

    x = joined["b"].to_numpy() - rf
    y = joined["s"].to_numpy() - rf
    var_b = float(np.var(x, ddof=1))
    if _degenerate_sd(math.sqrt(var_b) if var_b > 0 else 0.0, float(x.mean())):
        return {"beta": None, "n_overlap": n,
                "bench_reason": "zero benchmark variance"}

    beta = float(np.cov(y, x, ddof=1)[0, 1] / var_b)
    alpha_daily = float(y.mean() - beta * x.mean())
    resid = y - (alpha_daily + beta * x)
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - float(np.sum(resid ** 2)) / ss_tot if ss_tot > 0 else None

    diff = joined["s"] - joined["b"]
    te = _ann_vol(diff)
    ir = float(diff.mean() * TRADING_DAYS / te) if te > 0 else None

    up = joined[joined["b"] > 0]
    down = joined[joined["b"] < 0]
    up_cap = (round(100.0 * float(up["s"].mean() / up["b"].mean()), 1)
              if len(up) > 0 and up["b"].mean() != 0 else None)
    down_cap = (round(100.0 * float(down["s"].mean() / down["b"].mean()), 1)
                if len(down) > 0 and down["b"].mean() != 0 else None)

    sd_s, sd_b = joined["s"].std(ddof=1), joined["b"].std(ddof=1)
    corr = (round(float(joined["s"].corr(joined["b"])), 4)
            if sd_s > 0 and sd_b > 0 else None)

    return {"beta": round(beta, 3),
            "alpha_ann_pct": round(alpha_daily * TRADING_DAYS * 100.0, 2),
            "r_squared": None if r2 is None else round(r2, 4),
            "correlation": corr,
            "tracking_error_pct": round(te * 100.0, 2),
            "information_ratio": None if ir is None else round(ir, 2),
            "up_capture_pct": up_cap,
            "down_capture_pct": down_cap,
            "n_overlap": int(n)}


# --------------------------------------------------------------- headline


def headline_metrics(returns) -> dict:
    """CAGR, vol, Sharpe, Sortino, max drawdown, Calmar, hit rate."""
    s = _clean(returns)
    if len(s) < 5:
        return {"sharpe": None,
                "headline_reason": f"only {len(s)} days (< 5)"}
    eq = to_equity(s)
    total = float(eq.iloc[-1] - 1.0)
    years = len(s) / TRADING_DAYS
    cagr = (float(eq.iloc[-1] ** (1.0 / years) - 1.0)
            if eq.iloc[-1] > 0 and years > 0 else None)
    vol = _ann_vol(s)
    mean = float(s.mean())
    degenerate = _degenerate_sd(vol, mean)
    sharpe = None if degenerate else float(mean * TRADING_DAYS / vol)
    mdd = float((eq / eq.cummax() - 1.0).min()) * 100.0

    neg = s[s < 0].to_numpy()
    dvol = (float(np.sqrt(np.mean(neg ** 2)) * math.sqrt(TRADING_DAYS))
            if len(neg) else None)
    sortino = (None if dvol is None or _degenerate_sd(dvol, mean)
               else float(mean * TRADING_DAYS / dvol))
    calmar = (round(float(cagr * 100.0 / abs(mdd)), 2)
              if cagr is not None and mdd < 0 else None)

    return {"total_return_pct": round(total * 100.0, 2),
            "cagr_pct": None if cagr is None else round(cagr * 100.0, 2),
            "ann_vol_pct": round(vol * 100.0, 2),
            "sharpe": None if sharpe is None else round(sharpe, 2),
            "sortino": None if sortino is None else round(sortino, 2),
            "max_drawdown_pct": round(mdd, 2),
            "calmar": calmar,
            "hit_rate_pct": round(100.0 * float((s > 0).mean()), 1),
            "n_days": int(len(s)),
            **({"headline_reason": "zero return variance"} if degenerate else {})}


def tearsheet(returns, bench_returns=None, *, window: int = 63,
              top_n_drawdowns: int = 5) -> dict:
    """
    Assemble the full tearsheet. Benchmark sections are omitted with a reason
    when no benchmark is supplied, rather than silently absent.
    """
    s = _clean(returns)
    out = {"headline": headline_metrics(s),
           "monthly": monthly_returns_table(s),
           "rolling": rolling_metrics(s, window=window),
           "drawdowns": drawdown_periods(s, top_n=top_n_drawdowns),
           "underwater": drawdown_series(s)}
    out["benchmark"] = (benchmark_stats(s, bench_returns)
                        if bench_returns is not None
                        else {"beta": None,
                              "bench_reason": "no benchmark supplied"})
    return out
