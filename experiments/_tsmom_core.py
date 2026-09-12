"""Shared core for the TSMOM experiment scripts (see 2026-09-12_tsmom-futures.py).

MOP-faithful construction:
- signals at month-ends: sign of the 12-month log return skipping the most
  recent month (skip_days=31 approximation), optionally no skip
- positions effective from t0+1 (shifted 1 day, no lookahead)
- per-instrument ex-ante vol target (None = no vol scaling -> |position|=1)
- portfolio return = equal-weight average of the vol-scaled positions

Roll handling (in-house data caveat):
The `futures` table is raw continuous front-month closes with NO
back-adjustment; contract rolls show up as one-day open gaps. `clean_returns()`
drops each instrument's return on detected roll days (overnight gap > 4x the
symbol's median gap AND > 1.5%), which removes the spurious roll jumps while
keeping all other days.
"""
import datetime as dt
import math

import numpy as np
import pandas as pd

FUT = pd.read_parquet("storage/curated/futures/futures.parquet")

VOL_TARGET = 0.40   # MOP per-instrument ex-ante annualized vol
LOOKBACK = 252      # trading days in a year (vol window)
ANN = 252


def load_close_wide() -> pd.DataFrame:
    df = FUT[["date", "symbol", "close"]].copy()
    df = df[df["close"].apply(np.isfinite) & (df["close"] > 0)]
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    wide = df.pivot_table(index="date", columns="symbol", values="close")
    return wide.sort_index()


def load_open_wide() -> pd.DataFrame:
    df = FUT[["date", "symbol", "open"]].copy()
    df = df[df["open"].apply(np.isfinite) & (df["open"] > 0)]
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return df.pivot_table(index="date", columns="symbol", values="open").sort_index()


def month_end_anchors(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.resample("ME").last().dropna().values)


def asof_idx(days: np.ndarray, target: pd.Timestamp) -> int:
    return int(np.searchsorted(days, target, side="right") - 1)


def ann_sharpe(ret: pd.Series) -> float:
    r = ret.dropna()
    if len(r) < 3 or float(r.std(ddof=1)) == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1) * math.sqrt(ANN))


def ann_vol(ret: pd.Series) -> float:
    r = ret.dropna()
    if len(r) < 3:
        return float("nan")
    return float(r.std(ddof=1) * math.sqrt(ANN))


def max_drawdown(equity: pd.Series) -> float:
    e = equity.dropna()
    if e.empty:
        return float("nan")
    return float((e / e.cummax() - 1.0).min())


def roll_mask(gap_mult: float = 4.0, gap_abs: float = 0.015) -> pd.DataFrame:
    """True where an instrument shows a roll/listing gap (overnight open jump)."""
    close = load_close_wide()
    open_ = load_open_wide().reindex(close.index)
    gap = (open_ / close.shift(1) - 1.0).abs()
    med = gap.median()
    return (gap > gap_mult * med) & (gap > gap_abs)


def clean_returns(mask_rolls: bool = True) -> pd.DataFrame:
    """Daily returns with roll days set to NaN (excised from the portfolio)."""
    close = load_close_wide()
    ret = close.pct_change()
    if mask_rolls:
        ret = ret[~roll_mask()]
    return ret


def build_positions(close: pd.DataFrame,
                    vol_target: "float | None" = VOL_TARGET,
                    skip_days: int = 31,
                    vol_floor: float = 0.10) -> pd.DataFrame:
    """Month-end sign-of-momentum positions, vol-scaled to a per-instrument
    target. `vol_floor` caps effective leverage (0.40/floor) to blunt the
    flat-price data-glitch windows that collapse rolling vol (e.g. ZT=F 2003)."""
    days = np.asarray(close.index.to_pydatetime())
    ret = close.pct_change()
    vol = ret.rolling(LOOKBACK).std(ddof=1) * math.sqrt(ANN)
    if vol_floor:
        vol = vol.clip(lower=vol_floor)

    anchors = month_end_anchors(close.index)
    pos = pd.DataFrame(0.0, index=anchors, columns=close.columns)
    look_d = dt.timedelta(days=skip_days + 366)
    skip_d = dt.timedelta(days=skip_days)
    for t0 in anchors:
        ie = asof_idx(days, t0 - skip_d)
        ist = asof_idx(days, t0 - look_d)
        if ist < 0 or ie < 0:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            mom = np.log(close.iloc[ie] / close.iloc[ist])
        v = vol.loc[t0]
        for sym in close.columns:
            m, vv = mom.get(sym, 0.0), v.get(sym)
            if m == 0 or not np.isfinite(m) or not np.isfinite(vv) or vv <= 0:
                continue
            scale = VOL_TARGET / vv if vol_target is not None else 1.0
            pos.at[t0, sym] = math.copysign(scale, m)
    return pos


def portfolio_returns(pos: pd.DataFrame, close: pd.DataFrame,
                      ret: "pd.DataFrame | None" = None,
                      ret_name: str = "gross") -> pd.DataFrame:
    """Daily portfolio returns from positions (PIT: held shifted 1 day)."""
    if ret is None:
        ret = close.pct_change()
    held = pos.reindex(close.index, method="ffill").shift(1).fillna(0.0)
    n_active = (held.abs() > 0).sum(axis=1).replace(0, np.nan)
    gross = (held * ret).sum(axis=1) / n_active
    turnover = held.diff().abs().sum(axis=1)

    out = pd.DataFrame({ret_name: gross, "active": n_active, "turnover": turnover})
    for bps in (1.0, 2.5, 5.0, 10.0):
        out[f"net_{bps:g}bps"] = gross - turnover * (bps / 1e4) / n_active
    return out