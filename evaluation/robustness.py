"""
evaluation/robustness.py -- W2 robustness battery.

Five tests that ask questions evaluation/stats.py does not: whether a result
survives perturbation of the price level, whether the signal carries information
beyond the return moments, whether the drawdown is a property of the strategy or
of the trade order, how often an in-sample winner loses out-of-sample given the
number of configurations compared, and whether walk-forward results hold across
many purged partitions.

House rule, same as stats.py: a statistic whose assumptions fail returns None
plus a '*_reason' string, and nothing here divides by a zero/NaN sd.

Every re-simulation runs through the W1 engine with the CALLER'S ExecutionConfig.
A null that pays no costs while the strategy pays 10 bps is not a null.
"""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np
import pandas as pd

from evaluation.stats import _degenerate_sd

OHLC = ("open", "high", "low", "close")


# --------------------------------------------------------------- helpers


def _net_pnl(trades: pd.DataFrame) -> float:
    return 0.0 if trades.empty else float(trades["pnl_dollars"].sum())


def _usable(cache: dict) -> dict:
    """Symbols with a non-empty frame carrying a close column."""
    return {s: df for s, df in cache.items()
            if df is not None and not df.empty and "close" in df.columns}


def _sharpe(x: np.ndarray) -> float:
    """
    Annualization-free Sharpe used for ranking only.

    Returns 0.0 rather than +/-inf when the sd is zero OR float-noise around
    zero (a constant column's sd lands near 6e-19 in float64 -- see
    evaluation.stats.SD_FLOOR): a constant column carries no information to
    rank on, and letting it become +inf would make a degenerate column win
    every in-sample split in pbo().
    """
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return 0.0
    sd = x.std(ddof=1)
    if _degenerate_sd(sd):
        return 0.0
    return float(x.mean() / sd)


def _max_drawdown_pct(equity: np.ndarray) -> float:
    """Max peak-to-trough decline of an equity curve, as a positive percent."""
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1.0
    return float(-dd.min() * 100.0)


# --------------------------------------------------------------- 1. noise test


def _noisy_frame(df: pd.DataFrame, rng, sigma: float) -> pd.DataFrame:
    """
    One perturbed OHLC frame: a single lognormal factor per bar, applied to
    open/high/low/close TOGETHER.

    Applying one common factor is what keeps high >= max(open, close) and
    low <= min(open, close) true -- a positive scalar cannot reorder them.
    Perturbing the four independently would produce impossible bars. Volume is
    untouched.
    """
    shock = rng.lognormal(mean=0.0, sigma=sigma, size=len(df))
    out = df.copy()
    for col in OHLC:
        if col in out.columns:
            out[col] = out[col].to_numpy(dtype=float) * shock
    return out


def _tail_risk(arr: np.ndarray, alpha: float) -> "tuple[float, float]":
    """
    VaR/CVaR of a distribution of trial outcomes (net P&L across noise
    trials or price permutations) at confidence `alpha`.

    Deliberately NOT tearsheet.tail_risk_metrics: that function operates on
    a daily-return TIME SERIES (autocorrelated, one observation per trading
    day). This operates on a cross-trial distribution of independent
    re-simulations -- same VaR/CVaR mathematics (mean of the outcomes at or
    below the (1-alpha) percentile), different sampling unit, so it is kept
    as its own small helper rather than reusing that function on data it
    was not built for.
    """
    cut = float(np.percentile(arr, (1.0 - alpha) * 100.0))
    tail = arr[arr <= cut]
    cvar = float(tail.mean()) if len(tail) else cut
    return cut, cvar


def noise_test(rule, cache: dict, *, n_trials: int = 100, sigma_bps: float = 5.0,
               seed: int = 0, config=None, alpha: float = 0.95) -> dict:
    """
    Re-run the rule on price series perturbed by per-bar lognormal noise.

    The shock `exp(N(0, sigma))` is applied as ONE COMMON FACTOR PER BAR to
    open/high/low/close together. That jitters each bar's price LEVEL while
    leaving intrabar geometry (high >= max(open, close) >= min(open, close) >=
    low) exactly intact. Perturbing the four independently would produce
    impossible bars, and any rule reading high/low would then be reacting to
    malformed data rather than to noise. Volume is untouched.

    Signals are recomputed on the perturbed series -- this is an end-to-end
    re-run, not a re-pricing of fixed trades.

    A strategy whose net P&L flips sign under a few bps of jitter is fitted to
    accidents of the exact price path. `noise_cvar_pnl_dollars` (see
    _tail_risk) answers a question `noise_pct_profitable` cannot: not just
    how OFTEN noise flips the sign, but how BAD the P&L gets in the worst
    `1-alpha` share of trials when it does.
    """
    from evaluation import trades as tr

    usable = _usable(cache)
    if not usable:
        return {"noise_pct_profitable": None,
                "noise_reason": "no usable symbols in cache"}
    if not sigma_bps > 0:
        return {"noise_pct_profitable": None,
                "noise_reason": f"sigma_bps must be positive (got {sigma_bps})"}

    observed = _net_pnl(tr.simulate(rule, cache, config=config))
    sigma = float(sigma_bps) / 1e4
    rng = np.random.default_rng(seed)

    pnls = []
    for _ in range(n_trials):
        noisy = {sym: _noisy_frame(df, rng, sigma) for sym, df in usable.items()}
        pnls.append(_net_pnl(tr.simulate(rule, noisy, config=config)))

    arr = np.asarray(pnls, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < max(10, n_trials // 4):
        return {"noise_pct_profitable": None,
                "noise_reason": f"only {len(arr)} usable trials"}
    lo, hi = np.percentile(arr, [5.0, 95.0])
    var_cut, cvar = _tail_risk(arr, alpha)
    return {"observed_pnl_dollars": round(observed, 2),
            "noise_mean_pnl_dollars": round(float(arr.mean()), 2),
            "noise_median_pnl_dollars": round(float(np.median(arr)), 2),
            "noise_p5_pnl_dollars": round(float(lo), 2),
            "noise_p95_pnl_dollars": round(float(hi), 2),
            "noise_var_pnl_dollars": round(var_cut, 2),
            "noise_cvar_pnl_dollars": round(cvar, 2),
            "noise_pct_profitable": round(100.0 * float((arr > 0).mean()), 1),
            "sigma_bps": float(sigma_bps),
            "alpha": float(alpha),
            "n_trials": int(len(arr))}


# --------------------------------------------------------------- 2. price MCPT


def _shuffled_frame(df: pd.DataFrame, rng) -> "pd.DataFrame | None":
    """
    One synthetic OHLC frame: close-to-close log returns shuffled, path rebuilt
    from the same first price.

    Each bar's own ratios to its close (open/close, high/close, low/close)
    travel with the return that produced that bar, so intrabar geometry and gap
    structure stay attached to the bar rather than to the calendar slot.
    """
    close = df["close"].to_numpy(dtype=float)
    if len(close) < 3 or not np.all(np.isfinite(close)) or not np.all(close > 0):
        return None
    logret = np.diff(np.log(close))
    if not np.all(np.isfinite(logret)):
        return None

    perm = rng.permutation(len(logret))
    new_close = np.empty_like(close)
    new_close[0] = close[0]
    new_close[1:] = close[0] * np.exp(np.cumsum(logret[perm]))

    out = df.copy()
    out["close"] = new_close
    # source bar for each slot: slot 0 keeps its own, slot i>=1 inherits from
    # the bar that originally carried return perm[i-1] (i.e. bar perm[i-1]+1).
    src = np.empty(len(close), dtype=int)
    src[0] = 0
    src[1:] = perm + 1
    for col in ("open", "high", "low"):
        if col in df.columns:
            ratio = df[col].to_numpy(dtype=float) / close
            out[col] = new_close * ratio[src]
    return out


def price_mcpt(rule, cache: dict, *, n_perm: int = 200, seed: int = 0,
               config=None, alpha: float = 0.95) -> dict:
    """
    Monte Carlo permutation test on the PRICE SERIES, not on the trades.

    Shuffles each symbol's log returns, rebuilds a synthetic path, re-runs the
    rule end-to-end (signal generation included), and compares net P&L.
    One-sided empirical p with the +1 correction.

    LIMITATION, stated because this statistic is easy to over-read: shuffling
    returns destroys serial dependence as well as the signal's information. A
    trend-following rule scores well against this null partly BECAUSE the null
    has no trends, not only because the rule forecasts anything. This is
    evidence that the rule is not scoring on return moments alone; it is not by
    itself evidence of skill. Read it beside stats.permutation_trades, which
    nulls the signal while leaving the real price path -- autocorrelation
    included -- untouched. Either one alone is weak; the pair is informative.

    `price_mcpt_cvar_pnl_dollars` (see _tail_risk) is the mean null-world P&L
    in the worst `1-alpha` share of permutations -- how bad a plausible
    no-edge outcome looks in its own tail, not just whether the observed
    result beats the null on average.
    """
    from evaluation import trades as tr

    usable = _usable(cache)
    if not usable:
        return {"price_mcpt_p": None,
                "price_mcpt_reason": "no usable symbols in cache"}

    observed = _net_pnl(tr.simulate(rule, cache, config=config))
    rng = np.random.default_rng(seed)

    pnls = []
    ge = n_done = 0
    for _ in range(n_perm):
        synth = {}
        for sym, df in usable.items():
            frame = _shuffled_frame(df, rng)
            if frame is not None:
                synth[sym] = frame
        if not synth:
            continue
        n_done += 1
        pnl = _net_pnl(tr.simulate(rule, synth, config=config))
        pnls.append(pnl)
        if pnl >= observed:
            ge += 1

    if n_done < max(20, n_perm // 4):
        return {"price_mcpt_p": None,
                "price_mcpt_reason": f"only {n_done} usable permutations"}
    arr = np.asarray(pnls, dtype=float)
    arr = arr[np.isfinite(arr)]
    out = {"observed_pnl_dollars": round(observed, 2),
           "price_mcpt_p": round((1 + ge) / (n_done + 1), 4),
           "n_perm": int(n_done)}
    if len(arr) >= max(10, n_done // 4):
        var_cut, cvar = _tail_risk(arr, alpha)
        out["price_mcpt_var_pnl_dollars"] = round(var_cut, 2)
        out["price_mcpt_cvar_pnl_dollars"] = round(cvar, 2)
        out["alpha"] = float(alpha)
    return out


# --------------------------------------------------------------- 3. trade order


def trade_order_mc(trades: pd.DataFrame, *, n_trials: int = 1000, seed: int = 0,
                   starting_equity: float = 100_000.0) -> dict:
    """
    Shuffle the ORDER of realized trades and recompound the equity curve.

    Answers: how much of the observed drawdown is the strategy, and how much is
    the order the trades happened to arrive in?

    DRAWDOWN IS THE ONLY THING THIS TEST CAN TELL YOU, and that is not a
    limitation of the implementation -- it is arithmetic. Final equity is
    prod(1 + r_i), and multiplication commutes, so the final return is IDENTICAL
    under every permutation. (Summing dollar P&L is invariant for the same
    reason.) `final_return_pct` is therefore reported once, as a fact about the
    strategy, with no distribution around it: any percentile band on it would be
    a band of width zero dressed up as uncertainty. Path-dependent statistics --
    drawdown, time under water, the order in which capital was at risk -- are
    where permutation actually bites.

    `observed_mdd_percentile` is where the live sequence sits in the shuffled
    distribution. A low percentile means the real ordering was KIND -- plan
    around the median, not around what happened.
    """
    if trades is None or trades.empty:
        return {"mdd_median_pct": None, "order_reason": "no realized trades"}
    r = pd.Series(trades["pnl_pct"]).dropna().to_numpy(dtype=float) / 100.0
    if len(r) < 5:
        return {"mdd_median_pct": None,
                "order_reason": f"only {len(r)} trades (< 5)"}
    if np.any(r <= -1.0):
        return {"mdd_median_pct": None,
                "order_reason": "a trade lost 100% or more; compounding undefined"}

    def curve(seq: np.ndarray) -> np.ndarray:
        return starting_equity * np.cumprod(1.0 + seq)

    obs_curve = curve(r)
    obs_mdd = _max_drawdown_pct(np.concatenate([[starting_equity], obs_curve]))
    obs_final = float(obs_curve[-1] / starting_equity - 1.0) * 100.0

    rng = np.random.default_rng(seed)
    mdds = np.empty(n_trials)
    for i in range(n_trials):
        c = curve(rng.permutation(r))
        mdds[i] = _max_drawdown_pct(np.concatenate([[starting_equity], c]))

    return {"n_trades": int(len(r)),
            "final_return_pct": round(obs_final, 2),
            "observed_mdd_pct": round(obs_mdd, 2),
            "mdd_median_pct": round(float(np.median(mdds)), 2),
            "mdd_p5_pct": round(float(np.percentile(mdds, 5)), 2),
            "mdd_p95_pct": round(float(np.percentile(mdds, 95)), 2),
            "mdd_worst_pct": round(float(mdds.max()), 2),
            "observed_mdd_percentile": round(
                100.0 * float((mdds <= obs_mdd).mean()), 1),
            "n_trials": int(n_trials)}


# --------------------------------------------------------------- 4. PBO (CSCV)


def pbo(returns_matrix, *, n_splits: int = 8, metric=None) -> dict:
    """
    Probability of Backtest Overfitting via combinatorially-symmetric
    cross-validation (Bailey, Borwein, Lopez de Prado, Zhu).

    `returns_matrix` is (T periods, N configurations) -- the N things that were
    actually compared when the winner was picked. Rows are split into n_splits
    contiguous groups; for each of the C(S, S/2) ways to take half the groups as
    in-sample, the complement is out-of-sample. The in-sample best column's
    out-of-sample RANK (1 = worst, N = best) maps to a relative rank
    w = rank/(N+1) and a logit log(w/(1-w)).

    PBO is the fraction of combinations with logit <= 0 -- how often the
    in-sample winner lands in the bottom half out-of-sample. PBO near 0.5 means
    the selection procedure is a coin flip; near 0 means it generalizes.

    This measures the SELECTION, which is why it complements deflated_sharpe:
    that adjusts one Sharpe for how many trials existed, this asks whether
    picking by in-sample rank works at all.

    TWO CAVEATS, both measured on this implementation rather than assumed:

    1. A single PBO number is itself noisy. On pure-noise inputs (T=600, N=20,
       S=8) PBO across data realizations ranged 0.24 to 0.81 in testing -- the
       "no skill implies PBO ~ 0.5" statement is an expectation over datasets,
       not a property of any one dataset. Do not read 0.62 as meaningfully
       worse than 0.48.
    2. Because in-sample and out-of-sample are COMPLEMENTARY halves of one fixed
       dataset, each column's two halves are mechanically anticorrelated around
       its full-sample mean (pooled IS/OOS correlation measured at -0.23 on
       noise). CSCV is built to average that out across combinations, but it
       biases PBO upward on inputs with little real dispersion.

    What PBO reliably separates is genuine dominance from selection noise: a
    column with a real edge drives PBO to ~0 on every realization.
    """
    M = np.asarray(pd.DataFrame(returns_matrix).to_numpy(), dtype=float)
    if M.ndim != 2:
        return {"pbo": None, "pbo_reason": "returns_matrix must be 2-D"}
    T, N = M.shape
    if N < 2:
        return {"pbo": None,
                "pbo_reason": f"need at least 2 configurations (got {N})"}
    if n_splits < 4 or n_splits % 2 != 0:
        return {"pbo": None,
                "pbo_reason": f"n_splits must be even and >= 4 (got {n_splits})"}
    if T < n_splits * 2:
        return {"pbo": None,
                "pbo_reason": f"only {T} periods for {n_splits} splits (< {n_splits * 2})"}
    if not np.isfinite(M).all():
        return {"pbo": None, "pbo_reason": "returns_matrix contains non-finite values"}

    score = metric if metric is not None else _sharpe
    groups = np.array_split(np.arange(T), n_splits)
    half = n_splits // 2

    logits = []
    for combo in combinations(range(n_splits), half):
        is_idx = np.concatenate([groups[g] for g in combo])
        oos_idx = np.concatenate([groups[g] for g in range(n_splits)
                                  if g not in combo])
        is_perf = np.array([score(M[is_idx, j]) for j in range(N)])
        oos_perf = np.array([score(M[oos_idx, j]) for j in range(N)])
        if not np.isfinite(is_perf).any():
            continue
        best = int(np.nanargmax(is_perf))
        # average rank, 1 = worst .. N = best, so ties do not favor the winner
        order = pd.Series(oos_perf).rank(method="average").to_numpy()
        w = float(order[best]) / (N + 1.0)
        w = min(max(w, 1e-12), 1.0 - 1e-12)
        logits.append(math.log(w / (1.0 - w)))

    if not logits:
        return {"pbo": None, "pbo_reason": "no usable in-sample/out-of-sample splits"}
    lam = np.asarray(logits, dtype=float)
    return {"pbo": round(float((lam <= 0).mean()), 4),
            "median_logit": round(float(np.median(lam)), 4),
            "n_combinations": int(len(lam)),
            "n_configurations": int(N),
            "n_periods": int(T)}


# --------------------------------------------------------------- 5. CPCV


def cpcv_report(n_obs: int, *, n_groups: int = 6, k_test: int = 2,
                embargo_pct: float = 0.01, t1=None) -> dict:
    """
    Describe the CPCV configuration cpcv_splits() will produce, INCLUDING
    whether purging is exact.

    This exists so a caller cannot quietly receive embargo-only splits and
    believe they were purged: `purge` is "exact" only when t1 was supplied.
    """
    if n_groups < 2 or k_test < 1 or k_test >= n_groups:
        return {"n_splits": None,
                "cpcv_reason": f"need 1 <= k_test < n_groups (got k_test={k_test}, "
                               f"n_groups={n_groups})"}
    if n_obs < n_groups:
        return {"n_splits": None,
                "cpcv_reason": f"only {n_obs} observations for {n_groups} groups"}
    return {"n_splits": int(math.comb(n_groups, k_test)),
            "n_groups": int(n_groups), "k_test": int(k_test),
            "embargo_obs": int(n_obs * float(embargo_pct)),
            "purge": "exact" if t1 is not None else "embargo-only",
            "n_obs": int(n_obs)}


def cpcv_splits(n_obs: int, *, n_groups: int = 6, k_test: int = 2,
                embargo_pct: float = 0.01, t1=None):
    """
    Combinatorial purged cross-validation splits: every way of choosing k_test
    of n_groups contiguous groups as the test set.

    Two leakage controls, both needed for this to mean anything on financial
    data:

    - PURGE: with `t1` (per-observation label END index) supplied, drop any
      training observation whose label window [i, t1[i]] overlaps a test block.
      A training label that resolves inside the test window has seen the test
      period's outcome. Without t1 there is no label horizon to purge against
      and only the embargo applies -- cpcv_report() reports which you got.
    - EMBARGO: additionally drop the embargo_pct * n_obs training observations
      immediately AFTER each test block, covering serial correlation that
      outlives the label itself.

    Yields (train_idx, test_idx) as int arrays. Ordinary CV on price data
    without these two is leakage with extra steps.
    """
    if n_groups < 2 or k_test < 1 or k_test >= n_groups:
        raise ValueError(f"need 1 <= k_test < n_groups "
                         f"(got k_test={k_test}, n_groups={n_groups})")
    if n_obs < n_groups:
        raise ValueError(f"only {n_obs} observations for {n_groups} groups")

    idx = np.arange(n_obs)
    groups = np.array_split(idx, n_groups)
    embargo = int(n_obs * float(embargo_pct))
    t1_arr = None if t1 is None else np.asarray(t1, dtype=int)

    for combo in combinations(range(n_groups), k_test):
        test_idx = np.sort(np.concatenate([groups[g] for g in combo]))
        drop = np.zeros(n_obs, dtype=bool)
        drop[test_idx] = True

        # contiguous test blocks (adjacent chosen groups merge into one block)
        breaks = np.flatnonzero(np.diff(test_idx) > 1)
        starts = np.concatenate([[test_idx[0]], test_idx[breaks + 1]])
        ends = np.concatenate([test_idx[breaks], [test_idx[-1]]])

        for a, b in zip(starts, ends):
            if t1_arr is not None:
                # label window [i, t1[i]] overlaps [a, b]
                drop |= (t1_arr >= a) & (idx <= b)
            if embargo:
                drop[b + 1:b + 1 + embargo] = True

        train_idx = idx[~drop]
        yield train_idx, test_idx
