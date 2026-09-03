"""
evaluation/trades.py -- generic next-close trade-simulation engine.
Generalizes tv_rating_eval.simulate_trades to arbitrary TradeRule callables.

The ENGINE owns execution timing: a signal observed on day t executes at the
close of day t+1 (never trusted to the rule); one position per symbol at a
time; realized trades only -- a position with no qualifying exit before the
data ends is dropped and blocks later entries for that symbol.
"""

import heapq

import numpy as np
import pandas as pd

from evaluation import execution as ev_execution
from evaluation import hrp as ev_hrp

TRADE_COLS = ["symbol", "side", "entry_signal_date", "entry_date", "entry_price",
              "exit_signal_date", "exit_date", "exit_price", "days_held",
              "pnl_dollars", "pnl_pct", "exit_reason", "entry_vol_pct"]

#: Minimum SHARED trading days across a candidate's HRP cohort before a
#: correlation estimate is trusted -- same order as tearsheet.tail_risk_
#: metrics' 20-day floor and roughly VOL_WINDOW's own scale, not a
#: precisely-derived number. Below this, mode="hrp" rejects the candidate
#: (see _hrp_size) rather than fit a correlation matrix on noise.
MIN_HRP_OVERLAP_DAYS = 20

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


def _next_candidate(entry_positions, cursor: int, close: pd.Series,
                    long_exit, short_exit, symbol: str, notional: float,
                    cfg, n: int) -> "dict | None":
    """
    First eligible candidate trade for one symbol, searching entry signals
    starting at `cursor` (an index into the signal array, not a date). The
    resumable single-trade-at-a-time core both `simulate_symbol` (eager,
    whole-list) and `_simulate_single_pass` (lazy, portfolio-aware) build on
    -- one implementation of the entry/exit math, two different callers
    deciding when to resume it.

    Returns None when this symbol has no further candidate EVER from this
    point on: entry signals exhausted, or an entry found with no valid
    completing exit before the data ends. The latter matches
    simulate_symbol's original next_free=n case: an unresolved (never
    closes) position is treated as permanently occupying the symbol,
    identical in the single-pass engine as in the eager one -- this is a
    deliberate, narrow scope decision (see _simulate_single_pass's
    docstring), not an oversight.

    Returns {"sig_i", "exit_i", "row"} on success. `sig_i`/`exit_i` are the
    cursors a caller resumes from: `exit_i + 1` if this candidate is taken,
    `sig_i + 1` if it is rejected (the fix this function exists to enable --
    see _simulate_single_pass).
    """
    risk = cfg.risk
    rate = ev_execution.round_trip_rate(cfg.costs)
    has_risk = any(getattr(risk, f) is not None
                   for f in ("stop_loss_pct", "take_profit_pct",
                             "vol_stop_mult", "max_holding_days"))

    for sig_i, side in entry_positions:
        if sig_i < cursor:
            continue
        entry_i = sig_i + 1                 # ENGINE: next-close execution
        if entry_i >= n:
            return None
        entry_price = close.iloc[entry_i]
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue                        # bad price: try the next signal
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
            return None                     # still open: blocks the symbol
        exit_i = exit_sig_i + 1
        if exit_i >= n:
            return None
        exit_price = close.iloc[exit_i]
        if not np.isfinite(exit_price) or exit_price <= 0:
            return None
        pct = (exit_price / entry_price - 1.0) if side == "long" else \
              (1.0 - exit_price / entry_price)
        pnl_dollars = round(notional * pct, 2)
        pnl_pct = round(100 * pct, 3)
        total_rate = rate
        if side == "short" and cfg.costs.borrow_fee_bps > 0:
            # Annualized borrow fee accrued over the trade's actual holding
            # period, same rate the weight-matrix engine charges daily via
            # execution.daily_cost() -- but round_trip_rate() is a flat
            # per-trade constant with no notion of holding period, so this
            # engine (variable holding time per trade) needs its own accrual
            # rather than reusing that helper.
            total_rate += (cfg.costs.borrow_fee_bps / 1e4
                           * (exit_i - entry_i) / ev_execution.TRADING_DAYS)
        if total_rate:
            # Round, THEN deduct, THEN round -- the order strategies/stage3.py's
            # monkeypatch used. Deducting before the first rounding shifts
            # pnl_dollars by a cent per trade, which moves total_pnl_net and so
            # the campaign's pnl_p. See the W1 spec.
            pnl_dollars = round(pnl_dollars - notional * total_rate, 2)
            pnl_pct = round(pnl_pct - 100 * total_rate, 3)
        row = {
            "symbol": symbol, "side": side,
            "entry_signal_date": close.index[sig_i], "entry_date": close.index[entry_i],
            "entry_price": float(entry_price),
            "exit_signal_date": close.index[exit_sig_i], "exit_date": close.index[exit_i],
            "exit_price": float(exit_price), "days_held": int(exit_i - entry_i),
            "pnl_dollars": pnl_dollars,
            "pnl_pct": pnl_pct,
            "exit_reason": reason,
            # Always computed (independent of whether vol_stop_mult risk
            # control is enabled) so mode="inverse_vol" sizing has a trailing
            # vol estimate to size against; reuses the exact same 14-day
            # mean-absolute-close-change calculation _find_exit uses for a
            # vol stop, mult=1.0 -> a plain (unscaled) trailing vol reading.
            "entry_vol_pct": _vol_stop_pct(close, entry_i, 1.0),
        }
        return {"sig_i": sig_i, "exit_i": exit_i, "row": row}
    return None


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

    Short trades additionally accrue cfg.costs.borrow_fee_bps over their actual
    holding period (see the borrow-fee comment in _next_candidate) -- previously
    only backtest.py's weight-matrix engine charged this; this engine had a
    real gap where a short strategy's cost model silently excluded borrow fees.

    Every candidate found is unconditionally taken (there is no portfolio
    constraint at this level) -- built on the same _next_candidate core the
    portfolio-aware single-pass engine (_simulate_single_pass) uses, just
    driven by an unconditional "always admit, resume at exit_i + 1" loop
    instead of an admission decision. See docs/superpowers/specs/2026-09-03-
    single-pass-portfolio-engine-design.md.
    """
    cfg = ev_execution.resolve(config)
    close = pd.Series(np.asarray(close, dtype=float), index=index)
    n = len(close)
    entry_positions = sorted(
        [(i, "long") for i in np.flatnonzero(long_entry)] +
        [(i, "short") for i in np.flatnonzero(short_entry)]
    )
    rows = []
    cursor = 0
    while True:
        cand = _next_candidate(entry_positions, cursor, close, long_exit,
                               short_exit, symbol, notional, cfg, n)
        if cand is None:
            break
        rows.append(cand["row"])
        cursor = cand["exit_i"] + 1
    return rows


def _size_candidate(row: dict, sizing, equity: float) -> "float | None":
    """
    Notional to commit to one candidate under `sizing`, given current
    portfolio `equity`. Returns None when the candidate cannot be sized at
    all (no reliable trailing-vol estimate for inverse_vol, or a
    non-positive size) -- a rejection, not a fabricated guess. Shared by
    both the legacy filter-only _portfolio_pass and the single-pass engine
    so sizing math has exactly one implementation.

    mode='hrp' is NOT handled here -- it needs the open-position cohort and
    a trailing returns panel, neither of which this function has access
    to. See _hrp_size, called directly from _simulate_single_pass instead.
    """
    if sizing.mode == "hrp":
        raise ValueError("mode='hrp' requires the open-position cohort; "
                         "call _hrp_size via _simulate_single_pass, not "
                         "this function (see evaluation/hrp.py and the "
                         "2026-09-03 single-pass-portfolio-engine spec)")
    if sizing.mode == "fixed_fraction":
        size = sizing.fraction * equity
    elif sizing.mode == "inverse_vol":
        vol = row.get("entry_vol_pct")
        # No reliable trailing-vol estimate (insufficient history before
        # this trade's entry) -> reject rather than guess a size; a
        # fabricated vol reading would silently over- or under-size a
        # real position.
        if vol is None or not np.isfinite(vol) or vol <= 0:
            return None
        size = sizing.fraction * equity * (sizing.vol_target_pct / vol)
    else:
        size = sizing.notional
    return size if size > 0 else None


def _hrp_size(row: dict, sizing, symbols: dict, open_symbols: set,
             equity: float) -> "float | None":
    """
    HRP-weighted size for one candidate (mode='hrp'), given the OTHER
    currently-open symbols (see evaluation/hrp.py). Recomputes
    hrp.hrp_weights() over {open symbols} u {candidate symbol} from each
    member's trailing sizing.hrp_lookback daily returns ending STRICTLY
    BEFORE the candidate's entry_date -- the same PIT boundary
    entry_vol_pct already uses for inverse_vol (data available as of the
    fill, never the future).

    Entry-time-only, same philosophy as mode='inverse_vol' (see Sizing's
    docstring): already-open positions are NOT re-sized when a new one
    joins the cohort.

    Returns None (reject, not a fabricated size) when the cohort can't
    support a real correlation estimate: fewer than MIN_HRP_OVERLAP_DAYS
    trading days of SHARED history across every member, or hrp.hrp_weights
    itself raises (e.g. near-zero variance in some member). The n=1 case
    (no other open positions) is not ambiguous, though -- HRP is undefined
    for a single asset, but a one-name book has nothing to diversify
    against yet, so it gets the whole fraction * equity budget, same as
    fixed_fraction would.
    """
    sym = row["symbol"]
    cohort = sorted(open_symbols | {sym})
    if len(cohort) < 2:
        size = sizing.fraction * equity
        return size if size > 0 else None

    entry_date = row["entry_date"]
    returns = {}
    for s in cohort:
        close = symbols[s]["close"]
        window = close[close.index < entry_date].tail(sizing.hrp_lookback + 1)
        r = window.pct_change().dropna()
        if not r.empty:
            returns[s] = r
    if len(returns) < 2:
        return None

    panel = pd.DataFrame(returns)
    if len(panel.dropna()) < MIN_HRP_OVERLAP_DAYS:
        return None
    try:
        weights = ev_hrp.hrp_weights(panel)
    except ValueError:
        return None
    if sym not in weights.index:
        return None

    size = sizing.fraction * equity * float(weights.loc[sym])
    return size if size > 0 else None


def _admit_row(row: dict, size: float, sizing) -> dict:
    """Finalize an admitted candidate: re-denominate pnl_dollars onto the
    committed size for fractional/inverse-vol/hrp sizing (pnl_pct is
    already net of costs, unaffected). fixed_notional rows pass through
    unchanged."""
    out = dict(row)
    if sizing.mode in ("fixed_fraction", "inverse_vol", "hrp"):
        out["pnl_dollars"] = round(size * out["pnl_pct"] / 100.0, 2)
    return out


def _portfolio_pass(rows: "list[dict]", cfg) -> "list[dict]":
    """
    SUPERSEDED by _simulate_single_pass -- retained only as a historical
    reference and regression marker (see
    tests/test_execution_step_b.py::TestSinglePassPortfolio's comparison
    against this function's known-buggy output), not called by simulate()
    or permutation_trades() any more.

    Admits candidate trades in chronological entry order subject to a
    capital budget, a concurrency cap, and fractional/inverse-vol sizing.

    APPROXIMATION, stated plainly because it was easy to over-read: this
    filters candidates that simulate_symbol already produced per symbol,
    eagerly, assuming every one of them would be taken. A trade rejected
    here for lack of capital does NOT free its symbol to take some
    different trade it would have taken in a true single-pass portfolio
    simulation, because simulate_symbol had already committed that symbol's
    candidate list before this function ever ran. See
    docs/superpowers/specs/2026-09-03-single-pass-portfolio-engine-design.md
    for the full bug mechanism and the fix.
    """
    limits, sizing = cfg.limits, cfg.sizing
    capital = limits.capital
    if sizing.mode in ("fixed_fraction", "inverse_vol") and capital is None:
        raise ValueError(f"sizing.mode={sizing.mode!r} requires limits.capital")

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

        size = _size_candidate(r, sizing, equity)
        if size is None:
            continue

        if capital is not None:
            committed_now = sum(c for _, c, _ in open_positions)
            if committed_now + size > capital + 1e-9:
                continue

        row = _admit_row(r, size, sizing)
        admitted.append(row)
        open_positions.append((row["exit_date"], size, row["pnl_dollars"]))

    return admitted


def _push_next(heap: list, sym: str, state: dict, cursor: int,
              notional: float, cfg) -> None:
    """Generate one more candidate for `sym` starting at `cursor` and push
    it onto the shared heap, keyed (entry_date, symbol) -- symbol breaks
    ties because at most one candidate per symbol is ever heap-resident at
    once, so the tuple comparison never needs to order the candidate dicts
    themselves. Pushes nothing when the symbol has no further candidate."""
    cand = _next_candidate(state["entry_positions"], cursor, state["close"],
                           state["long_exit"], state["short_exit"], sym,
                           notional, cfg, state["n"])
    if cand is not None:
        heapq.heappush(heap, (cand["row"]["entry_date"], sym, cand))


def _simulate_single_pass(symbol_flags: dict, notional: float, cfg) -> "list[dict]":
    """
    True single-pass portfolio simulation: candidates are generated ONE AT A
    TIME per symbol (via _next_candidate) and merged across symbols in
    chronological (entry_date, symbol) order through a min-heap, with
    admission (concurrency, sizing, capital) decided at the moment each
    candidate is generated -- not after a whole per-symbol candidate list
    has already been committed to.

    This closes the admission-order limitation _portfolio_pass's own
    docstring names: a REJECTED candidate resumes its symbol's search right
    after the rejected candidate's own entry SIGNAL (`sig_i + 1`), not its
    exit -- so a different entry that fired before the rejected trade's
    would-be exit gets a real chance to be generated and admitted. An
    ADMITTED candidate resumes at `exit_i + 1`, same as today.

    Scope, deliberately narrow (see docs/superpowers/specs/2026-09-03-
    single-pass-portfolio-engine-design.md): a candidate whose entry has NO
    valid completing exit (position never closes before the data ends) is
    still treated as terminal for that symbol regardless of whether it
    would have been admitted or rejected -- identical to simulate_symbol's
    existing behavior for the unconstrained case. Modeling a perpetually-
    open position's capital consumption is a separate question this spec
    does not take on.

    `symbol_flags[sym] = (index, close, long_entry, long_exit, short_entry,
    short_exit)` -- pre-built per symbol so this is reusable by both
    simulate() (flags from the rule) and stats.permutation_trades()
    (flags from a permutation), the null must face the same admission-order
    fix the observed run does or the p-value comparison is invalid.

    `sizing.mode='hrp'` is the reason `open_positions` tracks each open
    trade's symbol (not just exit_date/committed/pnl_dollars): _hrp_size
    needs the live set of currently-open symbols to know what cohort to
    compute correlation-aware weights over at each admission decision. See
    evaluation/hrp.py and Sizing's own docstring for the full design.
    """
    limits, sizing = cfg.limits, cfg.sizing
    capital = limits.capital
    if sizing.mode in ("fixed_fraction", "inverse_vol", "hrp") and capital is None:
        raise ValueError(f"sizing.mode={sizing.mode!r} requires limits.capital")

    symbols = {}
    heap = []
    for sym, (index, close_raw, long_entry, long_exit, short_entry, short_exit) \
            in symbol_flags.items():
        close = pd.Series(np.asarray(close_raw, dtype=float), index=index)
        n = len(close)
        entry_positions = sorted(
            [(i, "long") for i in np.flatnonzero(long_entry)] +
            [(i, "short") for i in np.flatnonzero(short_entry)]
        )
        symbols[sym] = {"close": close, "long_exit": long_exit,
                        "short_exit": short_exit,
                        "entry_positions": entry_positions, "n": n}
        _push_next(heap, sym, symbols[sym], 0, notional, cfg)

    equity = capital if capital is not None else 0.0
    open_positions = []          # (exit_date, committed, pnl_dollars, symbol)
    admitted = []

    while heap:
        _, sym, cand = heapq.heappop(heap)
        row = cand["row"]

        still_open = []
        for exit_date, committed, pnl, osym in open_positions:
            if exit_date <= row["entry_date"]:
                equity += pnl
            else:
                still_open.append((exit_date, committed, pnl, osym))
        open_positions = still_open

        admit = not (limits.max_concurrent is not None
                    and len(open_positions) >= limits.max_concurrent)
        if admit:
            if sizing.mode == "hrp":
                open_symbols = {osym for _, _, _, osym in open_positions}
                size = _hrp_size(row, sizing, symbols, open_symbols, equity)
            else:
                size = _size_candidate(row, sizing, equity)
        else:
            size = None
        admit = admit and size is not None
        if admit and capital is not None:
            committed_now = sum(c for _, c, _, _ in open_positions)
            admit = committed_now + size <= capital + 1e-9

        if admit:
            out_row = _admit_row(row, size, sizing)
            admitted.append(out_row)
            open_positions.append((out_row["exit_date"], size,
                                   out_row["pnl_dollars"], sym))
            next_cursor = cand["exit_i"] + 1
        else:
            next_cursor = cand["sig_i"] + 1     # THE fix: resume at entry, not exit

        _push_next(heap, sym, symbols[sym], next_cursor, notional, cfg)

    return admitted


def simulate(rule, cache: dict, notional: "float | None" = None,
             *, config=None) -> pd.DataFrame:
    """
    One realized-trade row per closed position across all cache symbols.

    With config=None (LEGACY) the portfolio machinery is SKIPPED ENTIRELY
    rather than run with no-op limits, so there is no opportunity for float
    drift against pre-Step-B results -- and this includes every unconstrained
    caller regardless of config, since needs_portfolio below is False unless
    a caller explicitly opts into PortfolioLimits or non-fixed_notional
    Sizing (the live TV catalog campaign never does; see the design spec).
    """
    cfg = ev_execution.resolve(config)
    notional = rule.notional if notional is None else notional

    needs_portfolio = (cfg.limits.capital is not None
                       or cfg.limits.max_concurrent is not None
                       or cfg.sizing.mode != "fixed_notional")

    if needs_portfolio:
        symbol_flags = {}
        for sym, df in cache.items():
            if df.empty or "close" not in df.columns:
                continue
            le, lx, se, sx = rule_flags(rule, df)
            symbol_flags[sym] = (df.index, df["close"], le, lx, se, sx)
        rows = _simulate_single_pass(symbol_flags, notional, cfg) if symbol_flags else []
    else:
        rows = []
        for sym, df in cache.items():
            if df.empty or "close" not in df.columns:
                continue
            le, lx, se, sx = rule_flags(rule, df)
            rows.extend(simulate_symbol(df.index, df["close"], le, lx, se, sx,
                                        sym, notional, config=config))

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
