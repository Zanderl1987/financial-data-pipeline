"""
evaluation/meta_label.py -- meta-labeling on top of the discrete trade
simulator (evaluation/trades.py).

Lopez de Prado's meta-labeling idea: keep a primary signal's entries/exits
exactly as they are, but train a SECOND, purely binary model on "would
acting on this particular signal have been profitable?" and use its
out-of-sample probability to filter or size trades -- deciding whether to
act, not what to predict. This is unusually cheap to bootstrap on THIS
repo's stack specifically: trades.py's stop/target/max_holding_days exits
already ARE triple-barrier labeling (the barrier that got hit is
`exit_reason`, the outcome is `pnl_pct`), so the labeled data this needs
falls straight out of a normal simulate() call -- there is no new
simulation infrastructure here, only a secondary model and a walk-forward
harness for it.

No sklearn: it's installed in this environment but not a declared
dependency (not in requirements.txt), so introducing a hard `import
sklearn` here would be an undeclared-dependency regression. scipy IS a
declared dependency (requirements.txt), so the secondary model is a small
L2-regularized logistic regression fit with scipy.optimize.minimize --
one well-understood convex problem, not a hand-rolled optimizer.

PIT discipline: walk_forward_meta_labels() only ever fits on trades whose
ENTRY SIGNAL DATE is strictly before the test block's earliest entry
signal date -- never on trades that overlap or follow it. This is the
one place in this module where getting it wrong would matter (a
non-causal fit here would make the whole exercise a way to launder
lookahead into a P&L number that looks validated), so it is checked by a
dedicated test, not just asserted in a docstring.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize

DEFAULT_FEATURE_WINDOWS = (5, 10, 21)


# ------------------------------------------------------------------ labels

def triple_barrier_labels(trades: pd.DataFrame) -> pd.Series:
    """
    Binary meta-label: 1 if the primary signal's trade was profitable
    (pnl_pct > 0), 0 otherwise. Index-aligned to `trades`. Deliberately
    ignores WHICH barrier was hit (stop/target/time) -- exit_reason is
    available as a feature if a caller wants it, but the label this module
    trains against is the thing that actually matters for "should I have
    acted on this signal": did it make money.
    """
    return (trades["pnl_pct"] > 0).astype(int).rename("meta_label")


# --------------------------------------------------------------- features

def _indicator_frames(cache: dict, indicator_cols) -> dict:
    """One analytics.technical.indicators() call per symbol, memoized here
    so build_features() doesn't recompute a symbol's whole indicator frame
    once per trade. A symbol whose cache frame lacks OHLC columns (some
    callers only ever populate "close") maps to None rather than raising --
    build_features() reports that as NaN features, same as any other
    missing-history case."""
    from analytics import technical as ev_technical

    out = {}
    for sym, df in cache.items():
        try:
            out[sym] = ev_technical.indicators(df)
        except (ValueError, KeyError):
            out[sym] = None
    return out


def build_features(trades: pd.DataFrame, cache: dict,
                   windows=DEFAULT_FEATURE_WINDOWS,
                   centered: bool = False,
                   indicator_cols: "list[str] | None" = None) -> pd.DataFrame:
    """
    Trailing return over each window in `windows` plus trailing volatility
    (the longest window) and distance from that window's SMA, computed
    from `cache[symbol]["close"]` as of each trade's entry_signal_date --
    the date the primary rule's signal fired, i.e. the actual decision
    point, one bar BEFORE this engine's next-close entry_date. Using
    entry_date instead would leak the fill price the meta-model is meant
    to be deciding whether to accept.

    centered=False (default, PIT-safe): every window is TRAILING -- ends
    at entry_signal_date, drawn only from bars up to and including the
    decision point. centered=True is the leaky switch
    leakage_probe.feature_centering_leakage() ablates: each window is
    instead CENTERED on entry_signal_date, i.e. half the window is drawn
    from bars that had not happened yet at decision time -- the classic
    `rolling(window, center=True)` bug. This exists only so that probe can
    measure how much it inflates the apparent meta-filter lift; never pass
    centered=True outside that probe.

    indicator_cols (opt-in, default None -- no behavior change unless set):
    names of extra columns from analytics.technical.indicators() (rsi14,
    macd, adx14, atr14, mom10, willr14, cci20, ...) to pull as of each
    trade's entry_signal_date, prefixed "ind_" in the output. Every one of
    that module's indicators is rolling/shift-based -- never centered,
    never look-ahead -- so this is a PIT-safe way to reuse the repo's real
    technical-analysis code instead of hand-rolling more of the trio above.
    Requires cache[symbol] to carry open/high/low/close; a symbol missing
    them (or a date before an indicator's window has enough history) gets
    NaN for every requested indicator column, same "never imputed"
    contract as the rest of this function.

    Rows for a trade whose symbol/date isn't found in `cache`, or that
    falls before the required window has enough history on both sides,
    get NaN features and must be dropped by the caller before fitting
    (never silently imputed -- a fabricated feature value is worse than a
    dropped row).
    """
    longest = max(windows)
    half = longest // 2
    ind_frames = _indicator_frames(cache, indicator_cols) if indicator_cols else {}
    rows = []
    for _, t in trades.iterrows():
        sym, sig_date = t["symbol"], t["entry_signal_date"]
        feat = {w: np.nan for w in windows}
        feat["volatility"] = np.nan
        feat["dist_from_sma"] = np.nan
        for col in (indicator_cols or ()):
            feat[f"ind_{col}"] = np.nan
        df = cache.get(sym)
        if df is not None and "close" in df.columns and sig_date in df.index:
            loc = df.index.get_loc(sig_date)
            close = df["close"]
            n = len(close)
            if centered:
                if loc - half >= 0 and loc + half < n:
                    for w in windows:
                        wh = w // 2
                        lo_i, hi_i = loc - wh, loc + wh
                        if lo_i >= 0 and hi_i < n:
                            feat[w] = float(close.iloc[hi_i] / close.iloc[lo_i] - 1.0)
                    window_px = close.iloc[loc - half: loc + half + 1]
                    rets = window_px.pct_change().dropna()
                    feat["volatility"] = float(rets.std(ddof=0))
                    sma = float(window_px.mean())
                    feat["dist_from_sma"] = float(close.iloc[loc] / sma - 1.0) if sma else np.nan
            elif loc >= longest:
                for w in windows:
                    feat[w] = float(close.iloc[loc] / close.iloc[loc - w] - 1.0)
                window_px = close.iloc[loc - longest: loc + 1]
                rets = window_px.pct_change().dropna()
                feat["volatility"] = float(rets.std(ddof=0))
                sma = float(window_px.mean())
                feat["dist_from_sma"] = float(close.iloc[loc] / sma - 1.0) if sma else np.nan
        if indicator_cols:
            ind_df = ind_frames.get(sym)
            if ind_df is not None and sig_date in ind_df.index:
                for col in indicator_cols:
                    if col in ind_df.columns:
                        v = ind_df.at[sig_date, col]
                        feat[f"ind_{col}"] = float(v) if pd.notna(v) else np.nan
        rows.append(feat)
    cols = ([*windows, "volatility", "dist_from_sma"]
            + [f"ind_{c}" for c in (indicator_cols or ())])
    out = pd.DataFrame(rows, index=trades.index,
                       columns=cols).rename(columns={w: f"ret_{w}d" for w in windows})
    return out


# ---------------------------------------------------------- logistic core

def _neg_log_likelihood(w, X, y, l2):
    z = X @ w
    # log(1+exp(z)) via logaddexp(0, z), numerically stable for large |z|
    ll = np.sum(y * z - np.logaddexp(0.0, z))
    return -ll + 0.5 * l2 * np.sum(w[1:] ** 2)   # bias term (w[0]) unregularized


def _grad(w, X, y, l2):
    p = 1.0 / (1.0 + np.exp(-X @ w))
    g = X.T @ (p - y)
    reg = l2 * w
    reg[0] = 0.0
    return g + reg


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0) -> np.ndarray:
    """L2-regularized logistic regression via scipy L-BFGS-B. X must already
    include a bias column of ones. Returns the fitted weight vector."""
    w0 = np.zeros(X.shape[1])
    res = minimize(_neg_log_likelihood, w0, args=(X, y, l2), jac=_grad,
                   method="L-BFGS-B")
    return res.x


def _add_bias(X: np.ndarray) -> np.ndarray:
    return np.column_stack([np.ones(len(X)), X])


def predict_proba(X: np.ndarray, weights: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-(_add_bias(X) @ weights)))


# --------------------------------------------------------------- backtest

def walk_forward_meta_labels(trades: pd.DataFrame, features: pd.DataFrame,
                             min_train: int = 50, refit_every: int = 20,
                             l2: float = 1.0) -> pd.DataFrame:
    """
    Out-of-sample meta-probability per trade, refit periodically as more
    history accrues. `trades`/`features` must share an index (build_features'
    output does). Rows with any NaN feature, or that fall in the initial
    min_train warmup, get meta_proba=NaN -- there is no trained model yet
    to score them with, and that is reported, not papered over with a
    default probability.

    Causality: sorted by entry_signal_date; the model used to score trade i
    is fit ONLY on trades whose entry_signal_date is strictly less than
    trade i's. Ties on entry_signal_date (two symbols signaling the same
    day) are treated as simultaneous and excluded from each other's
    training set too, not just from later trades'.
    """
    labels = triple_barrier_labels(trades)
    order = trades["entry_signal_date"].sort_values(kind="stable").index
    ok = features.loc[order].notna().all(axis=1)

    proba = pd.Series(np.nan, index=trades.index, name="meta_proba")
    sig_dates = trades["entry_signal_date"]

    valid_order = [i for i in order if ok.loc[i]]
    weights = None
    last_fit_n = 0
    for pos, i in enumerate(valid_order):
        if pos < min_train:
            continue
        cutoff = sig_dates.loc[i]
        train_idx = [j for j in valid_order[:pos] if sig_dates.loc[j] < cutoff]
        if len(train_idx) < min_train:
            continue
        if weights is None or len(train_idx) - last_fit_n >= refit_every:
            Xtr = _add_bias(features.loc[train_idx].to_numpy(dtype=float))
            ytr = labels.loc[train_idx].to_numpy(dtype=float)
            if ytr.min() == ytr.max():
                continue    # a single-class training window can't fit a classifier
            weights = fit_logistic(Xtr, ytr, l2=l2)
            last_fit_n = len(train_idx)
        if weights is not None:
            x = features.loc[[i]].to_numpy(dtype=float)
            proba.loc[i] = float(predict_proba(x, weights)[0])
    return trades.assign(meta_proba=proba)


def evaluate_meta_filter(trades_with_proba: pd.DataFrame,
                         threshold: float = 0.5) -> dict:
    """
    Compares trade_summary() on the full out-of-sample-scored set against
    the subset the meta-model would have kept at `threshold` -- the actual
    question meta-labeling exists to answer, not just "does the classifier
    have decent accuracy" in isolation.
    """
    from evaluation.trades import trade_summary

    scored = trades_with_proba.dropna(subset=["meta_proba"])
    if scored.empty:
        return {"meta_reason": "no out-of-sample-scored trades "
                               "(history shorter than min_train)"}
    kept = scored[scored["meta_proba"] >= threshold]
    return {"threshold": threshold,
           "n_scored": len(scored),
           "n_kept": len(kept),
           "kept_fraction": round(len(kept) / len(scored), 3),
           "unfiltered": trade_summary(scored),
           "filtered": trade_summary(kept)}
