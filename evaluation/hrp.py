"""
evaluation/hrp.py -- Hierarchical Risk Parity weights (Lopez de Prado, 2016).

Correlation-aware position sizing across a SET of concurrently-held names,
the piece entry-time inverse-vol sizing (execution.py's Sizing.mode=
"inverse_vol") deliberately does not do -- that mode sizes each trade
against its own trailing volatility only, ignoring how it co-moves with
whatever else is currently held.

Hand-rolled rather than a skfolio/Riskfolio-Lib dependency: see
experiments/2026-09-03_skfolio-vs-riskfolio-vetting.md for the full
GO/NO-GO. Short version -- both packages force a real requirements.txt
cost (Riskfolio-Lib's scipy floor conflicts with this repo's pinned
scipy==1.10.1 outright and drags in a second full backtesting framework,
vectorbt, as a transitive dependency; skfolio is lighter but still forces
declaring scikit-learn plus a convex-solver stack for one estimator class
that HRP itself never needs a solver for) to reach an algorithm that is
four well-defined steps and needs nothing beyond scipy.cluster.hierarchy,
which ships in scipy's base install -- already a pinned dependency, no
new one added.

The four steps (hrp_weights() runs all of them):
  1. Correlation -> distance matrix (_distance_matrix).
  2. Hierarchical clustering on that distance (scipy.cluster.hierarchy.linkage).
  3. Quasi-diagonalization -- reorder leaves by the dendrogram so similar
     assets sit next to each other (_quasi_diag).
  4. Recursive bisection -- walk back down the tree, splitting weight at
     each fork inverse-proportional to the two child clusters' variance
     (_cluster_var, _recursive_bisection).

SCOPE, deliberately narrow today: this module computes weights for an
already-known, already-aligned returns panel. It does NOT (yet) plug into
Sizing.mode -- sizing a SET of concurrently-held positions from one HRP
call is a different shape than inverse_vol's per-trade-at-entry sizing,
and needs the trades.py _portfolio_pass admission-order rework that
TASKS.md already flags as its own design-doc-worthy project (touches the
exact engine the live campaign depends on). Two separate pieces of work
that happen to share a dependency decision, not one task.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform

VARIANCE_FLOOR = 1e-16


def _distance_matrix(corr: pd.DataFrame) -> np.ndarray:
    """
    Lopez de Prado's correlation-based distance: d_ij = sqrt(0.5*(1-corr_ij)).

    A PROPER metric (satisfies the triangle inequality, unlike the more
    obvious 1-corr_ij), which is what makes it valid input to hierarchical
    clustering. Ranges [0, 1]: 0 for perfectly correlated assets, 1 for
    perfectly anti-correlated ones.
    """
    d = np.sqrt(np.clip(0.5 * (1.0 - corr.to_numpy()), 0.0, None))
    np.fill_diagonal(d, 0.0)
    return d


def _quasi_diag(link: np.ndarray) -> list:
    """
    Recover the leaf order implied by a linkage tree: expand cluster ids
    from the root down into original leaf (asset) indices, so that assets
    merged early (most similar) end up adjacent in the returned order.

    `link` is a scipy linkage matrix: row i describes the (i + n)-th
    cluster, formed from cluster ids link[i, 0] and link[i, 1] (leaf
    indices are 0..n-1; cluster ids >= n refer to earlier rows).
    """
    link = link.astype(int)
    n = link.shape[0] + 1
    clusters = [link[-1, 0], link[-1, 1]]
    while max(clusters) >= n:
        idx = next(i for i, c in enumerate(clusters) if c >= n)
        c = clusters[idx]
        left, right = link[c - n, 0], link[c - n, 1]
        clusters = clusters[:idx] + [left, right] + clusters[idx + 1:]
    return clusters


def _cluster_var(cov: np.ndarray, members) -> float:
    """
    Variance of a cluster under LdP's own approximation: allocate an
    inverse-variance-weighted portfolio WITHIN the cluster (ignoring intra-
    cluster correlation, a deliberate simplification the original paper
    makes explicitly), then compute that portfolio's variance from the
    real (correlation-including) covariance sub-matrix.
    """
    sub = cov[np.ix_(members, members)]
    ivp = 1.0 / np.diag(sub)
    ivp /= ivp.sum()
    return float(ivp @ sub @ ivp)


def _recursive_bisection(cov: np.ndarray, sort_idx: list) -> pd.Series:
    """
    Walk back down the quasi-diagonalized order, splitting weight in half
    at each fork, then re-splitting each half inverse-proportional to its
    variance relative to its sibling -- so a lower-variance sub-cluster
    ends up with more weight than a naive 50/50 split would give it.
    Returns a Series indexed by the ORIGINAL integer asset positions
    (0..n-1), unsorted -- hrp_weights() maps these back to symbol names.
    """
    w = pd.Series(1.0, index=sort_idx)
    c_items = [sort_idx]
    while len(c_items) > 0:
        c_items = [i[j:k] for i in c_items
                  for j, k in ((0, len(i) // 2), (len(i) // 2, len(i)))
                  if len(i) > 1]
        for i in range(0, len(c_items), 2):
            c0, c1 = c_items[i], c_items[i + 1]
            var0, var1 = _cluster_var(cov, c0), _cluster_var(cov, c1)
            alpha = 1.0 - var0 / (var0 + var1)
            w[c0] *= alpha
            w[c1] *= (1.0 - alpha)
    return w


def hrp_weights(returns: pd.DataFrame, linkage_method: str = "single") -> pd.Series:
    """
    Hierarchical Risk Parity weights for a set of concurrently-held names.

    `returns` is a wide (date x symbol) frame -- columns entirely NaN are
    dropped first, then any remaining row with a NaN in any surviving
    column is dropped, since HRP needs one shared covariance matrix across
    every included symbol. This does NOT try to salvage partial-history
    symbols via their own overlapping window -- align history before
    calling this, the same discipline as everywhere else in evaluation/
    that a fabricated or silently-narrowed sample is worse than an
    explicit error.

    Raises ValueError if fewer than 2 symbols survive, or if any surviving
    symbol has near-zero variance (a constant return series makes cluster
    variance undefined, not just numerically unstable).

    Returns weights summing to 1.0, indexed by symbol, in the ORIGINAL
    column order of `returns` (not the quasi-diagonalized order used
    internally).
    """
    clean = returns.dropna(axis=1, how="all").dropna(axis=0, how="any")
    symbols = list(clean.columns)
    if len(symbols) < 2:
        raise ValueError("HRP needs at least 2 symbols with overlapping "
                         f"history after dropping NaNs, got {len(symbols)}")

    cov_df = clean.cov()
    cov = cov_df.to_numpy()
    variances = np.diag(cov)
    if np.any(variances <= VARIANCE_FLOOR):
        bad = [symbols[i] for i, v in enumerate(variances) if v <= VARIANCE_FLOOR]
        raise ValueError(f"HRP requires positive variance for every symbol; "
                         f"near-zero variance: {bad}")

    corr = clean.corr()
    dist = _distance_matrix(corr)
    condensed = squareform(dist, checks=False)
    link = linkage(condensed, method=linkage_method)
    sort_idx = _quasi_diag(link)

    w = _recursive_bisection(cov, sort_idx).sort_index()
    return pd.Series(w.to_numpy(), index=[symbols[i] for i in w.index],
                     name="hrp_weight")
