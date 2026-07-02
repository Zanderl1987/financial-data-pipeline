"""
Signals library — cross-sectional factor scores over the feature matrix.

Each signal is defined so that **higher = more attractive** (a long-side tilt),
then standardized cross-sectionally (z-scored within each date across symbols)
so heterogeneous units — a P/E, a return, a volatility — combine on one scale.
The composite is a weighted blend of whichever signals have data.

This is the layer that answers "what looks good right now and why" and feeds
directly into backtest.py.

Factors
-------
- momentum       : 12-1 price momentum (trailing 12m return, skipping last month)
- value          : earnings yield (EPS / price) — cheap stocks score high
- quality        : return on assets blended with gross margin
- low_vol        : inverse trailing realized volatility (low-vol anomaly)
- growth         : YoY revenue growth (point-in-time)
- short_pressure : inverse days-to-cover — lightly shorted stocks score high
                   (flip the weight sign to hunt squeeze candidates instead)
- insider_flow   : trailing 90d net insider buying, scaled by shares outstanding
- sentiment      : trailing 21d mean Claude-scored news sentiment

Usage
-----
    from analytics import signal_panel, rank_symbols
    rank_symbols(["AAPL", "MSFT", "NVDA", "AMD"])        # latest-date ranking
    panel = signal_panel(start="2024-01-01")             # full (symbol,date) panel
    rank_symbols(weights={"momentum": 2, "value": 1})    # tilt the blend
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analytics.features import feature_matrix

# Default factor weights for the composite. Only factors actually present in the
# data contribute; weights are renormalized over the available subset per row.
DEFAULT_WEIGHTS = {
    "momentum":       1.0,
    "value":          1.0,
    "quality":        1.0,
    "low_vol":        1.0,
    "growth":         1.0,
    "short_pressure": 1.0,
    "insider_flow":   1.0,
    "sentiment":      1.0,
}

_SIGNAL_COLS = list(DEFAULT_WEIGHTS)


def _zscore(s: pd.Series) -> pd.Series:
    """Cross-sectional z-score; returns 0 where the group has no spread."""
    mu = s.mean()
    sd = s.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=s.index)
    return (s - mu) / sd


def _raw_signals(fm: pd.DataFrame) -> pd.DataFrame:
    """Derive raw (pre-standardization) factor columns from a feature matrix."""
    df = fm.copy()

    # momentum — prefer 12-1, fall back to 63-day return for short histories
    df["momentum"] = df["mom_12_1"]
    if "ret_63d" in df:
        df["momentum"] = df["momentum"].fillna(df["ret_63d"])

    # value — earnings yield (inverse P/E); undefined for non-positive price
    if "fund_eps" in df:
        df["value"] = (df["fund_eps"] / df["close"]).where(df["close"] > 0)

    # quality — ROA blended with gross margin (both standardized later)
    roa = gm = None
    if {"fund_net_income", "fund_total_assets"}.issubset(df.columns):
        roa = (df["fund_net_income"] / df["fund_total_assets"]).where(df["fund_total_assets"] > 0)
    if {"fund_gross_profit", "fund_revenue"}.issubset(df.columns):
        gm = (df["fund_gross_profit"] / df["fund_revenue"]).where(df["fund_revenue"] > 0)
    if roa is not None or gm is not None:
        parts = [p for p in (roa, gm) if p is not None]
        df["quality"] = sum(parts) / len(parts)

    # low_vol — inverse realized vol (higher score = calmer stock)
    if "vol_21d" in df:
        df["low_vol"] = -df["vol_21d"]

    # growth — point-in-time YoY revenue growth: each distinct reported revenue
    # level compared to the previously reported level for the same symbol.
    if "fund_revenue" in df:
        df = df.sort_values(["symbol", "date"])
        changed = df["fund_revenue"].ne(df.groupby("symbol")["fund_revenue"].shift())
        df["_lvl"] = changed.groupby(df["symbol"]).cumsum()
        lvl_rev = df.groupby(["symbol", "_lvl"])["fund_revenue"].first()
        prior = lvl_rev.groupby(level="symbol").shift().rename("_prior_rev")
        df = df.merge(prior, on=["symbol", "_lvl"], how="left")
        df["growth"] = ((df["fund_revenue"] - df["_prior_rev"]) / df["_prior_rev"].abs()
                        ).where(df["_prior_rev"] > 0)
        df = df.drop(columns=["_lvl", "_prior_rev"])

    # short_pressure — lightly shorted stocks score high. days_to_cover
    # (shares short / avg daily volume) is the cross-sectionally comparable
    # measure; raw shares_short is not, absent each symbol's float.
    if "si_days_to_cover" in df:
        df["short_pressure"] = -df["si_days_to_cover"]

    # insider_flow — trailing net insider buying. Scale by shares outstanding
    # when known so a 10k-share buy means more at a small-cap than a mega-cap;
    # without shares outstanding, raw net shares is the only consistent unit.
    if "insider_net_90d" in df:
        if "fund_shares" in df and df["fund_shares"].gt(0).any():
            df["insider_flow"] = (df["insider_net_90d"] / df["fund_shares"]
                                  ).where(df["fund_shares"] > 0)
        else:
            df["insider_flow"] = df["insider_net_90d"]

    # sentiment — trailing mean news score, already on a [-1, +1] scale
    if "news_score_21d" in df:
        df["sentiment"] = df["news_score_21d"]

    return df


def signal_panel(
    symbols: "list[str] | str | None" = None,
    start: "str | None" = None,
    end: "str | None" = None,
    weights: "dict[str, float] | None" = None,
    fm: "pd.DataFrame | None" = None,
) -> pd.DataFrame:
    """
    Build a (symbol, date) panel of z-scored factor signals + composite.

    Parameters
    ----------
    symbols, start, end : passed through to feature_matrix (ignored if fm given)
    weights : factor -> weight (default DEFAULT_WEIGHTS); only present factors count
    fm      : reuse a precomputed feature_matrix instead of rebuilding

    Returns DataFrame:
        symbol | date | momentum | value | quality | low_vol | growth |
        short_pressure | insider_flow | sentiment | composite
    (signal columns are cross-sectional z-scores; composite is their weighted mean)
    """
    if fm is None:
        fm = feature_matrix(symbols, start=start, end=end)
    if fm.empty:
        return pd.DataFrame()

    raw = _raw_signals(fm)
    present = [c for c in _SIGNAL_COLS if c in raw.columns and raw[c].notna().any()]
    if not present:
        return pd.DataFrame()

    # z-score each present signal cross-sectionally within every date
    z = raw[["symbol", "date"]].copy()
    for col in present:
        z[col] = raw.groupby("date")[col].transform(_zscore)

    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    weighted = pd.DataFrame(index=z.index)
    for col in present:
        weighted[col] = z[col] * w.get(col, 0.0)
    # renormalize by the weight of the signals that are non-null in each row
    weight_mask = z[present].notna().astype(float) * pd.Series(
        {c: w.get(c, 0.0) for c in present}
    )
    denom = weight_mask.sum(axis=1).replace(0, np.nan)
    z["composite"] = weighted.sum(axis=1) / denom

    z.attrs["factors"] = present
    z.attrs["price_table"] = fm.attrs.get("price_table")
    return z.sort_values(["date", "composite"], ascending=[True, False]).reset_index(drop=True)


def rank_symbols(
    symbols: "list[str] | str | None" = None,
    on: "str | None" = None,
    weights: "dict[str, float] | None" = None,
    ascending: bool = False,
    fm: "pd.DataFrame | None" = None,
) -> pd.DataFrame:
    """
    Rank symbols by composite score on a single date (latest by default).

    Parameters
    ----------
    on        : 'YYYY-MM-DD' date to rank on (default: most recent in the panel)
    ascending : True ranks worst-first
    weights   : factor weight overrides for the composite

    Returns the cross-section for that date, sorted by composite, with a `rank`.
    """
    panel = signal_panel(symbols, weights=weights, fm=fm)
    if panel.empty:
        return panel
    target = pd.to_datetime(on) if on else panel["date"].max()
    cross = panel[panel["date"] == target].copy()
    cross = cross.sort_values("composite", ascending=ascending)
    cross.insert(0, "rank", range(1, len(cross) + 1))
    return cross.reset_index(drop=True)


# Individual factor convenience wrappers ------------------------------------

def _single_factor(factor: str, symbols, start, end, fm):
    panel = signal_panel(symbols, start=start, end=end,
                         weights={k: (1.0 if k == factor else 0.0) for k in _SIGNAL_COLS},
                         fm=fm)
    if panel.empty or factor not in panel.columns:
        return pd.DataFrame()
    return panel[["symbol", "date", factor]]


def momentum(symbols=None, start=None, end=None, fm=None):
    """Cross-sectional 12-1 momentum z-scores."""
    return _single_factor("momentum", symbols, start, end, fm)


def value(symbols=None, start=None, end=None, fm=None):
    """Cross-sectional earnings-yield (value) z-scores."""
    return _single_factor("value", symbols, start, end, fm)


def quality(symbols=None, start=None, end=None, fm=None):
    """Cross-sectional quality (ROA + gross margin) z-scores."""
    return _single_factor("quality", symbols, start, end, fm)


def low_volatility(symbols=None, start=None, end=None, fm=None):
    """Cross-sectional low-volatility z-scores."""
    return _single_factor("low_vol", symbols, start, end, fm)


def short_pressure(symbols=None, start=None, end=None, fm=None):
    """Cross-sectional short-pressure z-scores (lightly shorted = high)."""
    return _single_factor("short_pressure", symbols, start, end, fm)


def insider_flow(symbols=None, start=None, end=None, fm=None):
    """Cross-sectional net-insider-buying z-scores."""
    return _single_factor("insider_flow", symbols, start, end, fm)


def sentiment(symbols=None, start=None, end=None, fm=None):
    """Cross-sectional news-sentiment z-scores."""
    return _single_factor("sentiment", symbols, start, end, fm)
