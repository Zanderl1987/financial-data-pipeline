"""
evaluation/data.py -- price/return panel builder. The ONE place point-in-time
rules live:

  * apply_lag()          -- the only implementation of publication lag.
  * build_return_panel() -- entry at the first trading close STRICTLY AFTER
                            the (lagged) signal date; forward returns excess
                            vs the benchmark's matching path; non-positive
                            prices masked (degenerate pct_change guard).

No other evaluation module ever shifts dates. See
docs/superpowers/specs/2026-07-18-unified-eval-framework-design.md.
"""

import numpy as np
import pandas as pd

HORIZONS = (1, 3, 5, 10, 21)


def apply_lag(frame: pd.DataFrame, lag_days: int) -> pd.DataFrame:
    """Advance `date` by lag_days business days (0 = no-op). Returns a copy."""
    out = frame.copy()
    if lag_days:
        out["date"] = pd.to_datetime(out["date"]) + pd.offsets.BDay(int(lag_days))
    return out


def load_closes(symbols, start=None, end=None, benchmark="SPY",
                price_table=None) -> pd.DataFrame:
    """Wide close matrix for symbols (+ benchmark), longest-series invariant."""
    import event_backtest as eb          # local import: repo test convention
    syms = list(dict.fromkeys(list(symbols) + ([benchmark] if benchmark else [])))
    return eb.load_close_matrix(syms, start=start, end=end, price_table=price_table)


def build_return_panel(frame: pd.DataFrame, closes: pd.DataFrame,
                       horizons=HORIZONS, benchmark="SPY"):
    """
    Tidy panel: one row per input signal row, with entry_date and fwd_{h}d
    forward EXCESS returns. Returns (panel, dropped) where dropped maps
    symbol -> reason for every symbol that produced no rows.
    """
    dropped = {}
    min_len = max(horizons) + 2
    bench = None
    if benchmark and benchmark in closes.columns:
        bench = closes[benchmark].dropna()

    frames = []
    for sym, grp in frame.groupby("symbol", sort=False):
        if benchmark and sym == benchmark:
            dropped[sym] = "benchmark symbol excluded (excess vs itself is 0)"
            continue
        if sym not in closes.columns:
            dropped[sym] = "no price data"
            continue
        s = closes[sym].dropna()
        if len(s) < min_len:
            dropped[sym] = f"history too short ({len(s)} closes < {min_len})"
            continue

        c = s.to_numpy(dtype=float)
        n = len(s)
        b = bench.reindex(s.index).ffill().to_numpy(dtype=float) if bench is not None else None

        out = grp.reset_index(drop=True).copy()
        out["date"] = pd.to_datetime(out["date"])
        entry_loc = s.index.searchsorted(out["date"].to_numpy(), side="right")
        ok = entry_loc < n
        entry_dates = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns]")
        entry_dates[ok] = s.index.to_numpy()[entry_loc[ok]]
        out["entry_date"] = entry_dates

        for h in horizons:
            exit_loc = entry_loc + h
            ret = np.full(len(out), np.nan)
            m = ok & (exit_loc < n)
            if m.any():
                e0, e1 = c[entry_loc[m]], c[exit_loc[m]]
                good = np.isfinite(e0) & np.isfinite(e1) & (e0 > 0) & (e1 > 0)
                r = np.where(good, np.divide(e1, e0, where=e0 != 0) - 1.0, np.nan)
                if b is not None:
                    b0, b1 = b[entry_loc[m]], b[exit_loc[m]]
                    bgood = np.isfinite(b0) & np.isfinite(b1) & (b0 > 0) & (b1 > 0)
                    br = np.where(bgood, np.divide(b1, b0, where=b0 != 0) - 1.0, np.nan)
                    r = r - br
                ret[m] = r
            out[f"fwd_{h}d"] = ret
        frames.append(out)

    if not frames:
        return pd.DataFrame(), dropped
    return pd.concat(frames, ignore_index=True), dropped
