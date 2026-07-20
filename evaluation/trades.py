"""
evaluation/trades.py -- generic next-close trade-simulation engine.
Generalizes tv_rating_eval.simulate_trades to arbitrary TradeRule callables.

The ENGINE owns execution timing: a signal observed on day t executes at the
close of day t+1 (never trusted to the rule); one position per symbol at a
time; realized trades only -- a position with no qualifying exit before the
data ends is dropped and blocks later entries for that symbol.
"""

import numpy as np
import pandas as pd

TRADE_COLS = ["symbol", "side", "entry_signal_date", "entry_date", "entry_price",
              "exit_signal_date", "exit_date", "exit_price", "days_held",
              "pnl_dollars", "pnl_pct"]


def _bool_array(flags, n: int, who: str) -> np.ndarray:
    a = pd.Series(flags).fillna(False).to_numpy(dtype=bool)
    if len(a) != n:
        raise ValueError(f"{who}: rule returned {len(a)} flags for {n} rows")
    return a


def rule_flags(rule, df: pd.DataFrame):
    """(long_entry, long_exit, short_entry, short_exit) bool arrays for one frame."""
    n = len(df)
    z = np.zeros(n, dtype=bool)
    le, lx, se, sx = z, z, z, z
    if rule.side in ("long", "both"):
        le = _bool_array(rule.entries(df), n, f"{rule.name} entries")
        lx = _bool_array(rule.exits(df), n, f"{rule.name} exits")
    if rule.side == "short":
        se = _bool_array(rule.entries(df), n, f"{rule.name} entries")
        sx = _bool_array(rule.exits(df), n, f"{rule.name} exits")
    elif rule.side == "both":
        se = _bool_array(rule.short_entries(df), n, f"{rule.name} short_entries")
        sx = _bool_array(rule.short_exits(df), n, f"{rule.name} short_exits")
    return le, lx, se, sx


def simulate_symbol(index, close, long_entry, long_exit, short_entry, short_exit,
                    symbol: str, notional: float) -> "list[dict]":
    """Low-level engine on flag arrays (Tier-2 permutation re-enters here)."""
    close = pd.Series(np.asarray(close, dtype=float), index=index)
    n = len(close)
    rows = []
    entry_positions = sorted(
        [(i, "long") for i in np.flatnonzero(long_entry)] +
        [(i, "short") for i in np.flatnonzero(short_entry)]
    )
    next_free = 0
    for sig_i, side in entry_positions:
        if sig_i < next_free:
            continue                        # already in a position
        entry_i = sig_i + 1                 # ENGINE: next-close execution
        if entry_i >= n:
            continue
        entry_price = close.iloc[entry_i]
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue
        exit_cond = long_exit if side == "long" else short_exit
        exit_sig_i = None
        for j in range(entry_i + 1, n):
            if exit_cond[j]:
                exit_sig_i = j
                break
        if exit_sig_i is None:
            next_free = n                   # still open: blocks further entries
            continue
        exit_i = exit_sig_i + 1
        if exit_i >= n:
            next_free = n
            continue
        exit_price = close.iloc[exit_i]
        if not np.isfinite(exit_price) or exit_price <= 0:
            next_free = exit_i + 1
            continue
        pct = (exit_price / entry_price - 1.0) if side == "long" else \
              (1.0 - exit_price / entry_price)
        rows.append({
            "symbol": symbol, "side": side,
            "entry_signal_date": index[sig_i], "entry_date": index[entry_i],
            "entry_price": float(entry_price),
            "exit_signal_date": index[exit_sig_i], "exit_date": index[exit_i],
            "exit_price": float(exit_price), "days_held": int(exit_i - entry_i),
            "pnl_dollars": round(notional * pct, 2),
            "pnl_pct": round(100 * pct, 3),
        })
        next_free = exit_i + 1
    return rows


def simulate(rule, cache: dict, notional: "float | None" = None) -> pd.DataFrame:
    """One realized-trade row per closed position across all cache symbols."""
    notional = rule.notional if notional is None else notional
    rows = []
    for sym, df in cache.items():
        if df.empty or "close" not in df.columns:
            continue
        le, lx, se, sx = rule_flags(rule, df)
        rows.extend(simulate_symbol(df.index, df["close"], le, lx, se, sx,
                                    sym, notional))
    return pd.DataFrame(rows, columns=TRADE_COLS)


def trade_summary(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"n_trades": 0, "summary_reason": "no realized trades"}
    wins = trades["pnl_dollars"] > 0
    return {"n_trades": int(len(trades)),
            "n_long": int((trades["side"] == "long").sum()),
            "n_short": int((trades["side"] == "short").sum()),
            "total_pnl_dollars": round(float(trades["pnl_dollars"].sum()), 2),
            "win_rate_pct": round(100 * float(wins.mean()), 1),
            "avg_pnl_pct": round(float(trades["pnl_pct"].mean()), 3),
            "median_days_held": float(trades["days_held"].median()),
            "n_symbols": int(trades["symbol"].nunique())}
