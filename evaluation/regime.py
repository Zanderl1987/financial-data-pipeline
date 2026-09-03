"""
evaluation/regime.py -- Statistical Jump Model regime detection + reporting.

Detects a small number of persistent market regimes (e.g. "calm"/"stressed")
from a daily return series and lets any downstream evaluation (tearsheet,
event_backtest, the campaign registry) be reported per-regime instead of
pooled across the whole sample.

Why this exists instead of stats.regime_conditioning(): that function is a
fixed, hand-picked bull/bear + high/low-vol split (benchmark close vs its
200-day SMA; realized vol vs its own median) built for Tier-1 IC panel
conditioning at signal date. It's cheap and PIT-safe by construction, and
this module does not replace it there. What it lacks is persistence: an SMA
crossing whipsaws every time price oscillates around the average, so "bull"
and "bear" windows can be a handful of days long. A Statistical Jump Model
(Shu & Mulvey 2024, https://arxiv.org/abs/2402.05272) fits the SAME kind of
k-means-style clustering but adds a penalty for switching state on
consecutive days, solved via dynamic programming jointly with the cluster
centroids -- so raising jump_penalty buys longer, more interpretable regimes
at the cost of some fit quality, instead of an unpenalized clustering that
chatters day to day. Fully hand-rolled in numpy (no new dependency), same
call the W5 optimizer made about scipy's differential_evolution over adding
the `cma` package.

PIT CAVEAT, stated plainly: fit_jump_model() standardizes and clusters the
WHOLE feature series at once, in-sample. This is a segmentation/diagnostic
tool for reporting on results you already have (a live-tearsheet
per-regime breakdown, a completed campaign's per-regime Sharpe), not a
live regime-prediction signal -- do not feed fitted labels back into a
trading rule without a walk-forward refit, or every trade downstream of it
inherits look-ahead bias.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# ------------------------------------------------------------------ core fit

def _standardize(X: np.ndarray) -> np.ndarray:
    mu = X.mean(axis=0)
    sd = X.std(axis=0, ddof=0)
    sd = np.where(sd > 1e-12, sd, 1.0)   # a constant column stays constant, not NaN/inf
    return (X - mu) / sd


def _viterbi_assign(X: np.ndarray, centroids: np.ndarray,
                    jump_penalty: float) -> "tuple[np.ndarray, float]":
    """
    Optimal state path minimizing sum of squared distances to the assigned
    centroid plus jump_penalty for every day the state changes from the
    previous day. Standard Viterbi dynamic program, O(T*K^2).
    """
    T, K = len(X), len(centroids)
    dist = ((X[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)  # (T, K)
    cost = np.empty((T, K))
    back = np.zeros((T, K), dtype=int)
    cost[0] = dist[0]
    for t in range(1, T):
        prev = cost[t - 1][:, None] + jump_penalty * (
            1.0 - np.eye(K)[:, :])          # prev_state x this_state penalty
        # prev has shape (K_prev, K_this); take the best prev state per this-state
        best_prev = prev.argmin(axis=0)
        cost[t] = prev[best_prev, np.arange(K)] + dist[t]
        back[t] = best_prev
    labels = np.empty(T, dtype=int)
    labels[-1] = int(cost[-1].argmin())
    for t in range(T - 2, -1, -1):
        labels[t] = back[t + 1, labels[t + 1]]
    total_cost = float(cost[-1].min())
    return labels, total_cost


def fit_jump_model(X: np.ndarray, k: int = 2, jump_penalty: float = 5.0,
                   n_init: int = 10, max_iter: int = 50,
                   seed: "int | None" = 0) -> dict:
    """
    Fit a Statistical Jump Model to an already-standardized feature matrix
    X (T rows x D columns). Coordinate descent: alternate between the
    optimal state path given fixed centroids (Viterbi) and the optimal
    centroids given a fixed path (per-state mean, k-means style), keeping
    the best of n_init random restarts. Returns
    {labels, centroids, cost, n_iter, converged}.

    An empty state after reassignment (no days land in it) is reseeded from
    a random data point, the standard k-means empty-cluster fix -- without
    it a bad restart can silently collapse to fewer than k regimes.
    """
    X = np.asarray(X, dtype=float)
    T = len(X)
    if T < k * 2:
        raise ValueError(f"need at least {k * 2} rows for k={k} states, got {T}")
    rng = np.random.default_rng(seed)

    best = None
    for _ in range(n_init):
        centroids = X[rng.choice(T, size=k, replace=False)].copy()
        labels = None
        converged = False
        n_iter = 0
        for it in range(max_iter):
            n_iter = it + 1
            new_labels, cost = _viterbi_assign(X, centroids, jump_penalty)
            new_centroids = centroids.copy()
            for j in range(k):
                members = X[new_labels == j]
                if len(members):
                    new_centroids[j] = members.mean(axis=0)
                else:
                    new_centroids[j] = X[rng.integers(T)]
            moved = not np.allclose(new_centroids, centroids, atol=1e-10)
            same_labels = labels is not None and np.array_equal(labels, new_labels)
            centroids, labels = new_centroids, new_labels
            if same_labels and not moved:
                converged = True
                break
        _, final_cost = _viterbi_assign(X, centroids, jump_penalty)
        if best is None or final_cost < best["cost"]:
            best = {"labels": labels, "centroids": centroids, "cost": final_cost,
                    "n_iter": n_iter, "converged": converged}
    return best


# ------------------------------------------------------------- return bridge

def regime_features(returns, vol_window: int = 21) -> pd.DataFrame:
    """
    Trailing mean return, volatility, and downside deviation, each over
    vol_window trading days -- deliberately the same three ingredients a
    reader would use by hand to eyeball "is this a calm or stressed
    stretch": level, spread, and asymmetry of recent returns. Every value
    at date t uses only returns up to and including t (trailing window,
    no centered/future-leaking rolling stats).
    """
    s = pd.Series(returns).dropna().astype(float)
    mean_ret = s.rolling(vol_window).mean()
    vol = s.rolling(vol_window).std(ddof=0)
    downside = s.clip(upper=0.0).rolling(vol_window).apply(
        lambda w: float(np.sqrt(np.mean(w ** 2))), raw=True)
    out = pd.DataFrame({"mean_return": mean_ret, "volatility": vol,
                        "downside_dev": downside}).dropna()
    return out


def label_regimes(returns, k: int = 2, jump_penalty: float = 5.0,
                  vol_window: int = 21, n_init: int = 10,
                  seed: "int | None" = 0) -> dict:
    """
    End-to-end: returns -> features -> fitted regimes, ranked and named by
    annualized mean return (low to high) so "regime 0" always means the
    worst-performing cluster regardless of how k-means happened to index
    it. Returns {"labels": per-date Series, "n_switches": int,
    "regime_stats": {name: {ann_return_pct, ann_vol_pct, n_days}}} or a
    "*_reason" dict when there isn't enough history.
    """
    feats = regime_features(returns, vol_window=vol_window)
    if len(feats) < k * 2 * vol_window:
        return {"labels": None,
                "regime_reason": f"only {len(feats)} feature days "
                                 f"(< {k * 2 * vol_window} needed for k={k})"}
    Xz = _standardize(feats.to_numpy())
    fit = fit_jump_model(Xz, k=k, jump_penalty=jump_penalty,
                         n_init=n_init, seed=seed)
    raw_labels = pd.Series(fit["labels"], index=feats.index, name="regime_raw")

    # Rank clusters by realized mean return in the underlying return series
    # (not the standardized feature) so the label is directly interpretable.
    s = pd.Series(returns).reindex(feats.index).astype(float)
    order = (s.groupby(raw_labels).mean().sort_values().index.tolist())
    rank = {old: new for new, old in enumerate(order)}
    labels = raw_labels.map(rank).rename("regime")

    stats = {}
    for j in range(k):
        sub = s[labels == j]
        n = len(sub)
        stats[int(j)] = {
            "n_days": int(n),
            "ann_return_pct": round(float(sub.mean() * TRADING_DAYS * 100), 2) if n else None,
            "ann_vol_pct": round(float(sub.std(ddof=1) * math.sqrt(TRADING_DAYS) * 100), 2)
                          if n > 1 else None,
        }
    n_switches = int((labels.diff().fillna(0) != 0).sum())
    return {"labels": labels, "n_switches": n_switches, "regime_stats": stats,
           "params": {"k": k, "jump_penalty": jump_penalty,
                      "vol_window": vol_window, "converged": fit["converged"]}}


def walk_forward_regimes(returns, k: int = 2, jump_penalty: float = 5.0,
                         vol_window: int = 21, min_train: int = 756,
                         refit_every: int = 63, n_init: int = 5,
                         seed: "int | None" = 0) -> dict:
    """
    PIT-safe regime labels via periodic refitting on an EXPANDING window.

    label_regimes() fits fit_jump_model() on the WHOLE sample at once --
    its own docstring says plainly that makes it "a segmentation/
    diagnostic tool for reporting on results you already have... not a
    live regime-prediction signal." This is the walk-forward variant that
    caveat calls for: refit periodically as history accrues, and use only
    the NEWLY-added date range from each refit's own labels.

    Mechanics: starting once `min_train` feature-days exist, refit
    fit_jump_model() every `refit_every` trading days on features[:cutoff]
    -- an EXPANDING window, never fixed-length or rolling, matching
    meta_label.walk_forward_meta_labels' own choice for the same reason
    (more history characterizes a rare regime like a genuine crash better
    than a short rolling window would). Each fit's Viterbi decode jointly
    optimizes over its WHOLE training window, so early dates within one
    fit's window ARE influenced by later dates in that SAME window -- but
    only the labels for the newest slice of dates (right at that window's
    own trailing edge) are ever kept in the final output; everything
    earlier was already labeled by a PRIOR, earlier-cutoff fit and is not
    touched again. A date's final label therefore only ever depends on
    data up to and including the refit that produced it -- the same
    causality discipline meta_label.walk_forward_meta_labels uses, applied
    to a clustering model instead of a classifier.

    ONE EXCEPTION, stated plainly rather than hidden: the very FIRST
    `min_train`-day window has no prior fit to walk forward from, so its
    labels come from an in-sample fit over that whole window -- the same
    PIT caveat label_regimes() carries, but confined to the warmup period
    only. `warmup_end`/`walk_forward_start` in the return dict mark that
    boundary explicitly so a caller who needs a strictly causal series can
    slice it out.

    Cluster ranking (which raw cluster index means "worst"/"best") is
    recomputed at EVERY refit from ONLY that fit's own training-window
    returns, never from data past its cutoff -- so "regime 0" stays "the
    worst-performing cluster as of what was knowable at that refit," not a
    fixed global ranking only knowable in hindsight.

    n_init defaults lower than label_regimes' (5 vs 10): a walk-forward run
    does roughly len(feats) / refit_every fits, so the per-fit random-
    restart speed/quality tradeoff matters more here than for a single
    one-off fit.

    Returns {"labels", "n_switches", "regime_stats" (computed post-hoc over
    the assembled series -- descriptive only, never fed back into a fit),
    "n_refits", "n_converged", "warmup_end", "walk_forward_start", "params"}
    or a "*_reason" dict when there isn't enough history for even one fit.
    """
    feats = regime_features(returns, vol_window=vol_window)
    if len(feats) < min_train:
        return {"labels": None,
                "regime_reason": f"only {len(feats)} feature days "
                                 f"(< min_train={min_train})"}

    s = pd.Series(returns).reindex(feats.index).astype(float)
    X = feats.to_numpy()

    label_chunks = []
    n_refits = n_converged = 0
    prev_cutoff = 0
    cutoff = min_train
    while prev_cutoff < len(feats):
        cutoff = min(cutoff, len(feats))
        Xz = _standardize(X[:cutoff])
        fit = fit_jump_model(Xz, k=k, jump_penalty=jump_penalty,
                             n_init=n_init, seed=seed)
        n_refits += 1
        n_converged += int(fit["converged"])

        # Rank clusters using ONLY returns up to THIS fit's own cutoff --
        # the same PIT boundary the fit itself was trained on.
        raw_full = pd.Series(fit["labels"], index=feats.index[:cutoff])
        order = (s.iloc[:cutoff].groupby(raw_full).mean()
                 .sort_values().index.tolist())
        rank = {old: new for new, old in enumerate(order)}

        new_slice = raw_full.iloc[prev_cutoff:cutoff]
        label_chunks.append(new_slice.map(rank))

        prev_cutoff = cutoff
        cutoff = prev_cutoff + refit_every

    labels = pd.concat(label_chunks).rename("regime")

    stats = {}
    for j in range(k):
        sub = s.reindex(labels.index)[labels == j]
        n = len(sub)
        stats[int(j)] = {
            "n_days": int(n),
            "ann_return_pct": round(float(sub.mean() * TRADING_DAYS * 100), 2) if n else None,
            "ann_vol_pct": round(float(sub.std(ddof=1) * math.sqrt(TRADING_DAYS) * 100), 2)
                          if n > 1 else None,
        }
    n_switches = int((labels.diff().fillna(0) != 0).sum())
    return {"labels": labels, "n_switches": n_switches, "regime_stats": stats,
           "n_refits": n_refits, "n_converged": n_converged,
           "warmup_end": feats.index[min_train - 1],
           "walk_forward_start": (feats.index[min_train]
                                  if min_train < len(feats) else None),
           "params": {"k": k, "jump_penalty": jump_penalty,
                      "vol_window": vol_window, "min_train": min_train,
                      "refit_every": refit_every, "n_init": n_init}}


def regime_report(returns, labels: pd.Series) -> dict:
    """
    Per-regime headline_metrics() from tearsheet.py -- Sharpe/Sortino/
    drawdown broken out by regime label instead of pooled, so a strategy
    that only works in one regime doesn't hide behind a blended number.
    Reuses tearsheet's computation rather than reimplementing it, matching
    this repo's compute/render split.
    """
    from evaluation import tearsheet as ts

    s = pd.Series(returns).reindex(labels.index).dropna()
    out = {}
    for regime in sorted(labels.dropna().unique()):
        sub = s[labels.reindex(s.index) == regime]
        out[int(regime)] = ts.headline_metrics(sub)
    return out
