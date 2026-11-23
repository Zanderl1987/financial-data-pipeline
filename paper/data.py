"""Daily bar fetch for the cloud paper-trading job.

Plain split-adjusted OHLCV from yf.download(auto_adjust=False), matching the
backtest's yfinance_universe_prices. Any bar dated >= today (America/New_York)
is dropped: during market hours yfinance returns today's unfinished bar, and
computing signals from it would be look-ahead.
"""
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

_COLS = ["open", "high", "low", "close", "volume"]


def _ny_today() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def _normalize(df: pd.DataFrame, today: date):
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    if "close" not in df.columns:
        return None
    for c in _COLS:
        if c not in df.columns:
            df[c] = float("nan")
    df = df[_COLS].apply(pd.to_numeric, errors="coerce")
    idx = pd.DatetimeIndex(pd.to_datetime(df.index))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df.index.name = "date"
    df = df.dropna(how="all")
    # a duplicated date makes get_indexer raise downstream: keep the last, sort
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df.index < pd.Timestamp(today)]
    return None if df.empty else df


def fetch_daily_bars(symbols, start: date, end: date, today: date | None = None,
                     batch: int = 200) -> dict:
    today = today or _ny_today()
    symbols = list(symbols)
    out = {}
    for i in range(0, len(symbols), batch):
        syms = symbols[i:i + batch]
        if i:
            time.sleep(1)
        raw = yf.download(syms, start=start, end=end, auto_adjust=False,
                          group_by="ticker", threads=True, progress=False)
        if raw is None or raw.empty:
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            top = set(raw.columns.get_level_values(0))
            for s in syms:
                if s in top:
                    df = _normalize(raw[s], today)
                    if df is not None:
                        out[s] = df
        elif len(syms) == 1:
            df = _normalize(raw, today)
            if df is not None:
                out[syms[0]] = df
    return out
