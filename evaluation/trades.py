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

from evaluation import execution as ev_execution

TRADE_COLS = ["symbol", "side", "entry_signal_date", "entry_date", "entry_price",
              "exit_signal_date", "exit_date", "exit_price", "days_held",
              "pnl_dollars", "pnl_pct", "exit_reason"]

#: Window used for the close-only volatility estimate behind
#: RiskControls.vol_stop_mult. Mirrors event_backtest.scenario()'s 14-day
#: `window_px.diff().abs().mean()` so the two engines measure the same thing.
VOL_WINDOW = 14


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


def _vol_stop_pct(close: pd.Series, entry_i: int, mult: float) -> "float | None":
    """
    Close-only volatility stop threshold, as a percent of entry price.

    Deliberately identical to event_backtest.scenario()'s computation: the mean
    absolute close-to-close change over the prior VOL_WINDOW bars, scaled by
    `mult`. That is NOT average true range -- there are no highs, lows, or prior
    close here -- which is why the config field is named vol_stop_mult and not
    atr_stop_mult. Returns None when there is insufficient history, matching
    scenario()'s `if loc0 >= 14` guard.
    """
    if entry_i < VOL_WINDOW:
        return None
    window = close.iloc[entry_i - VOL_WINDOW:entry_i]
    move = float(window.diff().abs().mean())
    px0 = float(close.iloc[entry_i])
    if not np.isfinite(move) or not np.isfinite(px0) or px0 <= 0:
        return None
    return (move * mult / px0) * 100.0


def _find_exit(close: pd.Series, exit_cond, entry_i: int, entry_price: float,
               side: str, risk, n: int):
    """
    First exit SIGNAL index at or after entry_i+1, and why it fired.

    Look-ahead safety: every trigger is evaluated on the close of day j and the
    caller executes at the close of j+1 -- identical treatment to a rule exit.
    A stop that filled at its own trigger day's close would be look-ahead, which
    is the bug class that has already bitten this repo twice.

    Precedence when several conditions fire on the SAME bar: rule, then stop,
    then target, then time. Only the label differs -- the exit bar is the same --
    but the label feeds exit_reason, so it must be deterministic.
    """
    sign = 1.0 if side == "long" else -1.0
    stop_pct = risk.stop_loss_pct
    if risk.vol_stop_mult is not None:
        vol_pct = _vol_stop_pct(close, entry_i, risk.vol_stop_mult)
        if vol_pct is not None:
            stop_pct = vol_pct
    peak = entry_price                      # for trailing stops

    last_j = n - 1
    if risk.max_holding_days is not None:
        # days_held is (exit_i - entry_i) == (j + 1 - entry_i), so the last
        # signal bar that respects the cap is entry_i + max_holding_days - 1.
        last_j = min(last_j, entry_i + risk.max_holding_days - 1)

    for j in range(entry_i + 1, last_j + 1):
        if exit_cond[j]:
            return j, "rule"
        px = close.iloc[j]
        if not np.isfinite(px) or px <= 0:
            continue
        if side == "long":
            peak = max(peak, px)
        else:
            peak = min(peak, px)
        ref = peak if risk.trailing else entry_price
        move = sign * (px / ref - 1.0)
        if stop_pct is not None and move <= -abs(stop_pct) / 100.0:
            return j, "stop"
        if risk.take_profit_pct is not None:
            gain = sign * (px / entry_price - 1.0)
            if gain >= abs(risk.take_profit_pct) / 100.0:
                return j, "target"
        if j == last_j and risk.max_holding_days is not None:
            return j, "time"
    return None, None


def simulate_symbol(index, close, long_entry, long_exit, short_entry, short_exit,
                    symbol: str, notional: float, *,
                    config=None) -> "list[dict]":
    """
    Low-level engine on flag arrays (Tier-2 permutation re-enters here).

    `config` is keyword-only and appended, never inserted: stats.permutation_trades
    calls this with 8 positional arguments and strategies/stage3.py binds
    `notional` by name through inspect.signature. Both must keep working.
    config=None means ExecutionConfig LEGACY, which reproduces the pre-Step-B
    behavior exactly -- no costs, no stops, no holding cap.
    """
    cfg = ev_execution.resolve(config)
    risk = cfg.risk
    rate = ev_execution.round_trip_rate(cfg.costs)
    has_risk = any(getattr(risk, f) is not None
                   for f in ("stop_loss_pct", "take_profit_pct",
                             "vol_stop_mult", "max_holding_days"))

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
        if has_risk:
            exit_sig_i, reason = _find_exit(close, exit_cond, entry_i,
                                            float(entry_price), side, risk, n)
        else:
            exit_sig_i, reason = None, None
            for j in range(entry_i + 1, n):
                if exit_cond[j]:
                    exit_sig_i, reason = j, "rule"
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
        pnl_dollars = round(notional * pct, 2)
        pnl_pct = round(100 * pct, 3)
        if rate:
            # Round, THEN deduct, THEN round -- the order strategies/stage3.py's
            # monkeypatch used. Deducting before the first rounding shifts
            # pnl_dollars by a cent per trade, which moves total_pnl_net and so
            # the campaign's pnl_p. See the W1 spec.
            pnl_dollars = round(pnl_dollars - notional * rate, 2)
            pnl_pct = round(pnl_pct - 100 * rate, 3)
        rows.append({
            "symbol": symbol, "side": side,
            "entry_signal_date": index[sig_i], "entry_date": index[entry_i],
            "entry_price": float(entry_price),
            "exit_signal_date": index[exit_sig_i], "exit_date": index[exit_i],
            "exit_price": float(exit_price), "days_held": int(exit_i - entry_i),
            "pnl_dollars": pnl_dollars,
            "pnl_pct": pnl_pct,
            "exit_reason": reason,
        })
        next_free = exit_i + 1
    return rows


def _portfolio_pass(rows: "list[dict]", cfg) -> "list[dict]":
    """
    Admit candidate trades in chronological entry order subject to a capital
    budget, a concurrency cap, and fractional sizing.

    APPROXIMATION, stated plainly because it is easy to over-read: this filters
    candidates that simulate_symbol already produced per symbol. The per-symbol
    "one position at a time, an unclosed position blocks later entries" rule
    resolves FIRST. A trade rejected here for lack of capital therefore does not
    free its symbol to take some different trade it would have taken in a true
    single-pass portfolio simulation. This captures the first-order effect of a
    capital constraint, not a full portfolio engine.
    """
    limits, sizing = cfg.limits, cfg.sizing
    capital = limits.capital
    if sizing.mode == "fixed_fraction" and capital is None:
        raise ValueError("sizing.mode='fixed_fraction' requires limits.capital")

    ordered = sorted(rows, key=lambda r: (r["entry_date"], r["symbol"]))
    equity = capital if capital is not None else 0.0
    open_positions = []          # (exit_date, committed, pnl_dollars)
    admitted = []

    for r in ordered:
        # Release everything that closed at or before this entry.
        still_open = []
        for exit_date, committed, pnl in open_positions:
            if exit_date <= r["entry_date"]:
                equity += pnl
            else:
                still_open.append((exit_date, committed, pnl))
        open_positions = still_open

        if limits.max_concurrent is not None and len(open_positions) >= limits.max_concurrent:
            continue

        if sizing.mode == "fixed_fraction":
            size = sizing.fraction * equity
            if size <= 0:
                continue
        else:
            size = sizing.notional

        if capital is not None:
            committed_now = sum(c for _, c, _ in open_positions)
            if committed_now + size > capital + 1e-9:
                continue

        row = dict(r)
        if sizing.mode == "fixed_fraction":
            # pnl_pct is already net of costs; re-denominate onto the new size.
            row["pnl_dollars"] = round(size * row["pnl_pct"] / 100.0, 2)
        admitted.append(row)
        open_positions.append((row["exit_date"], size, row["pnl_dollars"]))

    return admitted


def simulate(rule, cache: dict, notional: "float | None" = None,
             *, config=None) -> pd.DataFrame:
    """
    One realized-trade row per closed position across all cache symbols.

    With config=None (LEGACY) the portfolio pass is SKIPPED ENTIRELY rather than
    run with no-op limits, so there is no opportunity for float drift against
    pre-Step-B results.
    """
    cfg = ev_execution.resolve(config)
    notional = rule.notional if notional is None else notional
    rows = []
    for sym, df in cache.items():
        if df.empty or "close" not in df.columns:
            continue
        le, lx, se, sx = rule_flags(rule, df)
        rows.extend(simulate_symbol(df.index, df["close"], le, lx, se, sx,
                                    sym, notional, config=config))

    needs_portfolio = (cfg.limits.capital is not None
                       or cfg.limits.max_concurrent is not None
                       or cfg.sizing.mode != "fixed_notional")
    if needs_portfolio and rows:
        rows = _portfolio_pass(rows, cfg)

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
