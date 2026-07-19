"""
evaluation/contracts.py -- typed input contracts for the unified evaluation
framework.

Validation happens HERE, loudly, at construction. Everything downstream
(data.py, the evaluators, runner.py) trusts a constructed contract and never
re-validates. See docs/superpowers/specs/2026-07-18-unified-eval-framework-design.md.
"""

import warnings
from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

MIN_DATES_WARN = 250


def _clean_dates(frame: pd.DataFrame, who: str) -> pd.DataFrame:
    out = frame.copy()
    dates = pd.to_datetime(out["date"])
    if getattr(dates.dt, "tz", None) is not None:
        raise ValueError(f"{who}: dates must be tz-naive")
    dates = dates.astype("datetime64[ns]")
    out["date"] = dates
    return out


@dataclass
class Signal:
    """
    Continuous score per (symbol, day).

    lag_days  : days after `date` the value became knowable (0 for
                price-derived signals; explicit and conservative for
                filed/published data). Applied ONLY by data.apply_lag().
    direction : +1 higher-is-better, -1 lower-is-better, 0 unknown --
                orients bucket definitions and expected IC sign in reports;
                0 reports raw signs with no orientation applied.
    """
    name: str
    frame: pd.DataFrame
    lag_days: int = 0
    direction: int = 1
    source: str = ""

    def __post_init__(self):
        who = f"Signal '{self.name}'"
        missing = {"symbol", "date", "value"} - set(self.frame.columns)
        if missing:
            raise ValueError(f"{who}: missing columns {sorted(missing)}")
        if self.direction not in (-1, 0, 1):
            raise ValueError(f"{who}: direction must be -1, 0, or +1")
        if self.lag_days < 0:
            raise ValueError(f"{who}: lag_days must be >= 0")
        f = _clean_dates(self.frame[["symbol", "date", "value"]], who)
        if f["value"].isna().any():
            raise ValueError(f"{who}: NaN values not allowed -- drop or fill upstream")
        if f.duplicated(["symbol", "date"]).any():
            raise ValueError(f"{who}: duplicate (symbol, date) rows -- the provider "
                             "must aggregate to one row per (symbol, date)")
        n_dates = f["date"].nunique()
        if n_dates < MIN_DATES_WARN:
            warnings.warn(f"{who}: only {n_dates} distinct dates (< {MIN_DATES_WARN}) "
                          "-- daily-IC t-stats will be unreliable")
        self.frame = f.sort_values(["date", "symbol"]).reset_index(drop=True)


@dataclass
class EventSet:
    """Discrete point-in-time occurrences, grouped by `label` for study."""
    name: str
    frame: pd.DataFrame
    lag_days: int = 0
    min_events: int = 5

    def __post_init__(self):
        who = f"EventSet '{self.name}'"
        missing = {"symbol", "date", "label"} - set(self.frame.columns)
        if missing:
            raise ValueError(f"{who}: missing columns {sorted(missing)}")
        if self.lag_days < 0:
            raise ValueError(f"{who}: lag_days must be >= 0")
        keep = ["symbol", "date", "label"] + (["magnitude"] if "magnitude" in self.frame.columns else [])
        f = _clean_dates(self.frame[keep], who)
        if f[["symbol", "date", "label"]].isna().any().any():
            raise ValueError(f"{who}: NaN in symbol/date/label not allowed")
        self.frame = f.sort_values(["date", "symbol"]).reset_index(drop=True)


@dataclass
class TradeRule:
    """
    A system producing discrete trades. entries/exits are callables
    (df) -> boolean Series over an OHLCV+signal frame; for side="long" or
    "short" they define that side's rule; for side="both" they define the
    LONG rule and short_entries/short_exits define the short rule.

    Rules see data up to and including day t; the ENGINE (trades.py)
    executes at the close of t+1. Entry timing is never trusted to the rule.
    """
    name: str
    entries: Callable
    exits: Callable
    side: str = "long"
    short_entries: Optional[Callable] = None
    short_exits: Optional[Callable] = None
    notional: float = 10_000.0

    def __post_init__(self):
        who = f"TradeRule '{self.name}'"
        if self.side not in ("long", "short", "both"):
            raise ValueError(f"{who}: side must be 'long', 'short', or 'both'")
        for label, fn in (("entries", self.entries), ("exits", self.exits)):
            if not callable(fn):
                raise ValueError(f"{who}: {label} must be callable")
        if self.side == "both":
            if not (callable(self.short_entries) and callable(self.short_exits)):
                raise ValueError(f"{who}: side='both' requires callable "
                                 "short_entries and short_exits")
        if self.notional <= 0:
            raise ValueError(f"{who}: notional must be positive")
