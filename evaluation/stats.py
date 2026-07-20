"""
evaluation/stats.py -- the entire significance battery, all three tiers.

Tier 1 (parametric)     -- this section (Task 3).
Tier 2 (resampling)     -- appended by Task 6.
Tier 3 (research-grade) -- appended by Task 7.

House rule: a statistic whose assumptions fail returns None plus a
'*_reason' string. NEVER divide by a zero/NaN sd (bug class hit twice
in the TV-rating build).
"""

import math

import numpy as np
import pandas as pd
from scipy import stats as sps

# --------------------------------------------------------------- Tier 1


def t_to_p(t: float) -> float:
    """Two-sided p from a t-statistic via the normal approximation."""
    return float(2.0 * (1.0 - sps.norm.cdf(abs(t))))


def pooled_ic(values, fwd) -> dict:
    x = pd.Series(values).reset_index(drop=True)
    y = pd.Series(fwd).reset_index(drop=True)
    m = x.notna() & y.notna()
    n = int(m.sum())
    if n < 10:
        return {"pooled_ic": None, "pooled_p": None, "n": n,
                "pooled_reason": f"fewer than 10 pairs (n={n})"}
    if x[m].nunique() < 2 or y[m].nunique() < 2:
        return {"pooled_ic": None, "pooled_p": None, "n": n,
                "pooled_reason": "no variance in values or returns"}
    rho, p = sps.spearmanr(x[m], y[m])
    return {"pooled_ic": round(float(rho), 4), "pooled_p": round(float(p), 4), "n": n}


def daily_ic(panel: pd.DataFrame, value_col: str, fwd_col: str,
             min_names: int = 5) -> dict:
    sub = panel.dropna(subset=[value_col, fwd_col])
    ics = []
    for _, day in sub.groupby("date"):
        if day["symbol"].nunique() >= min_names and day[value_col].nunique() > 1:
            r, _ = sps.spearmanr(day[value_col], day[fwd_col])
            if np.isfinite(r):
                ics.append(r)
    if len(ics) < 5:
        return {"mean_daily_ic": None, "ic_se": None, "ic_t_stat": None,
                "ic_days": len(ics),
                "daily_reason": f"only {len(ics)} usable days (< 5)"}
    a = np.asarray(ics)
    sd = a.std(ddof=1)
    out = {"mean_daily_ic": round(float(a.mean()), 4), "ic_days": int(len(a)),
           "ic_pct_positive": round(100 * float((a > 0).mean()), 1)}
    if sd > 0:
        se = sd / math.sqrt(len(a))
        out["ic_se"] = round(float(se), 5)
        out["ic_t_stat"] = round(float(a.mean() / se), 2)
    else:
        out["ic_se"] = None
        out["ic_t_stat"] = None
        out["daily_reason"] = "zero cross-day variance in daily ICs"
    return out


def quantile_spread(panel: pd.DataFrame, value_col: str, fwd_col: str,
                    q: float = 0.2, min_side: int = 6) -> dict:
    """
    Pooled top-q vs bottom-q cross-sectional bucket spread (per-date buckets,
    pooled returns, Welch t). Cross-sectional quantiles rather than absolute
    thresholds so arbitrary signal scales work.
    """
    sub = panel.dropna(subset=[value_col, fwd_col])
    tops, bots = [], []
    for _, day in sub.groupby("date"):
        if len(day) < 2 or day[value_col].nunique() < 2:
            continue
        k = max(1, int(round(len(day) * q)))
        r = day.sort_values(value_col)
        tops.append(r[fwd_col].tail(k))
        bots.append(r[fwd_col].head(k))
    top = pd.concat(tops) if tops else pd.Series(dtype=float)
    bot = pd.concat(bots) if bots else pd.Series(dtype=float)
    if len(top) <= min_side or len(bot) <= min_side:
        return {"spread_pct": None, "spread_t": None, "spread_p": None,
                "top_n": int(len(top)), "bottom_n": int(len(bot)),
                "spread_reason": f"bucket too small (top={len(top)}, bottom={len(bot)})"}
    sd_t, sd_b = top.std(ddof=1), bot.std(ddof=1)
    out = {"top_n": int(len(top)), "bottom_n": int(len(bot)),
           "top_mean_pct": round(100 * float(top.mean()), 3),
           "bottom_mean_pct": round(100 * float(bot.mean()), 3),
           "spread_pct": round(100 * float(top.mean() - bot.mean()), 3)}
    if (sd_t > 0 or sd_b > 0) and np.isfinite(sd_t) and np.isfinite(sd_b):
        t, p = sps.ttest_ind(top, bot, equal_var=False)
        out["spread_t"] = round(float(t), 2)
        out["spread_p"] = round(float(p), 4)
    else:
        out["spread_t"] = None
        out["spread_p"] = None
        out["spread_reason"] = "zero variance in both buckets"
    return out


# --------------------------------------------------------------- Tier 2


def block_bootstrap_spread(panel: pd.DataFrame, value_col: str, fwd_col: str,
                           q: float = 0.2, n_boot: int = 1000, seed: int = 0,
                           min_days: int = 20) -> dict:
    """
    Bootstrap whole DATES (cross-sections) with replacement -> percentile CI
    on the top-q minus bottom-q spread. Resampling dates, not rows, preserves
    cross-sectional correlation (the block that matters for daily panels).
    """
    sub = panel.dropna(subset=[value_col, fwd_col])
    per_day = []
    for _, day in sub.groupby("date"):
        if len(day) < 2 or day[value_col].nunique() < 2:
            continue
        k = max(1, int(round(len(day) * q)))
        r = day.sort_values(value_col)
        per_day.append((float(r[fwd_col].tail(k).mean()),
                        float(r[fwd_col].head(k).mean())))
    if len(per_day) < min_days:
        return {"spread_ci_lo_pct": None, "spread_ci_hi_pct": None,
                "boot_reason": f"only {len(per_day)} usable days (< {min_days})"}
    arr = np.asarray(per_day)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(n_boot, len(arr)))
    boot = arr[idx].mean(axis=1)                       # (n_boot, 2)
    spreads = boot[:, 0] - boot[:, 1]
    lo, hi = np.percentile(spreads, [2.5, 97.5])
    return {"spread_boot_mean_pct": round(100 * float(spreads.mean()), 3),
            "spread_ci_lo_pct": round(100 * float(lo), 3),
            "spread_ci_hi_pct": round(100 * float(hi), 3),
            "n_boot": int(n_boot), "boot_days": int(len(arr))}


def bootstrap_sharpe(returns, block_len: int = 21, n_boot: int = 1000,
                     seed: int = 0) -> dict:
    """Moving-block bootstrap CI on the annualized Sharpe of daily returns."""
    r = pd.Series(returns).dropna().to_numpy(dtype=float)
    n = len(r)
    if n < 3 * block_len:
        return {"sharpe": None, "sharpe_ci_lo": None, "sharpe_ci_hi": None,
                "sharpe_reason": f"only {n} days (< {3 * block_len})"}
    sd = r.std(ddof=0)
    if not sd > 0:
        return {"sharpe": None, "sharpe_ci_lo": None, "sharpe_ci_hi": None,
                "sharpe_reason": "zero return variance"}
    ann = math.sqrt(252.0)
    obs = float(r.mean() / sd * ann)
    rng = np.random.default_rng(seed)
    n_blocks = int(math.ceil(n / block_len))
    sharpes = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n - block_len + 1, size=n_blocks)
        sample = np.concatenate([r[s:s + block_len] for s in starts])[:n]
        ssd = sample.std(ddof=0)
        sharpes[i] = sample.mean() / ssd * ann if ssd > 0 else np.nan
    sharpes = sharpes[np.isfinite(sharpes)]
    if len(sharpes) < n_boot // 2:
        return {"sharpe": round(obs, 2), "sharpe_ci_lo": None,
                "sharpe_ci_hi": None,
                "sharpe_reason": "bootstrap degenerate (zero-variance samples)"}
    lo, hi = np.percentile(sharpes, [2.5, 97.5])
    return {"sharpe": round(obs, 2), "sharpe_ci_lo": round(float(lo), 2),
            "sharpe_ci_hi": round(float(hi), 2), "n_boot": int(len(sharpes))}


def permutation_trades(rule, cache: dict, n_perm: int = 200,
                       seed: int = 0) -> dict:
    """
    Permutation null for a trade system: within each symbol, relocate the
    same NUMBER of entry signals to uniformly random days (exit rule kept
    as-is), re-simulate through the same engine, and compare total P&L and
    win rate. One-sided empirical p-values with the +1 correction.
    """
    from evaluation import trades as tr        # local import (no cycles)
    obs = tr.simulate(rule, cache)
    if obs.empty:
        return {"pnl_p": None, "win_rate_p": None,
                "perm_reason": "no realized trades"}
    obs_pnl = float(obs["pnl_dollars"].sum())
    obs_wr = float((obs["pnl_dollars"] > 0).mean())
    rng = np.random.default_rng(seed)
    flags = {}
    for sym, df in cache.items():
        if df.empty or "close" not in df.columns:
            continue
        flags[sym] = (df.index, df["close"].to_numpy(dtype=float),
                      tr.rule_flags(rule, df))
    pnl_ge = wr_ge = n_done = 0
    for _ in range(n_perm):
        rows = []
        for sym, (index, close, (le, lx, se, sx)) in flags.items():
            n = len(index)
            ple = np.zeros(n, dtype=bool)
            k = int(le.sum())
            if k:
                ple[rng.choice(n, size=k, replace=False)] = True
            pse = np.zeros(n, dtype=bool)
            k = int(se.sum())
            if k:
                pse[rng.choice(n, size=k, replace=False)] = True
            rows.extend(tr.simulate_symbol(index, close, ple, lx, pse, sx,
                                           sym, rule.notional))
        perm = pd.DataFrame(rows, columns=tr.TRADE_COLS)
        if perm.empty:
            continue
        n_done += 1
        if float(perm["pnl_dollars"].sum()) >= obs_pnl:
            pnl_ge += 1
        if float((perm["pnl_dollars"] > 0).mean()) >= obs_wr:
            wr_ge += 1
    if n_done < max(20, n_perm // 4):
        return {"pnl_p": None, "win_rate_p": None,
                "perm_reason": f"only {n_done} permutations produced trades"}
    return {"obs_pnl_dollars": round(obs_pnl, 2),
            "obs_win_rate_pct": round(100 * obs_wr, 1),
            "pnl_p": round((1 + pnl_ge) / (n_done + 1), 4),
            "win_rate_p": round((1 + wr_ge) / (n_done + 1), 4),
            "n_perm": int(n_done)}


def bh_fdr(records, alpha: float = 0.10, p_key: str = "p") -> pd.DataFrame:
    """
    Benjamini-Hochberg step-up across a run's full statistics grid.
    records: list of dicts each holding p_key (may be None) plus id fields.
    Returns the rows as a DataFrame with p_adj and reject added; None/NaN
    p-values are excluded from m and never rejected.
    """
    df = pd.DataFrame(records).copy()
    if df.empty:
        df["p_adj"] = pd.Series(dtype=float)
        df["reject"] = pd.Series(dtype=bool)
        return df
    p = pd.to_numeric(df[p_key], errors="coerce")
    df["p_adj"] = np.nan
    df["reject"] = False
    m = int(p.notna().sum())
    if m == 0:
        return df
    ps = p[p.notna()].sort_values()
    ranks = np.arange(1, m + 1)
    adj = ps.to_numpy() * m / ranks
    adj = np.minimum.accumulate(adj[::-1])[::-1]       # enforce monotonicity
    df.loc[ps.index, "p_adj"] = np.clip(adj, 0, 1)
    passed = ps.to_numpy() <= alpha * ranks / m
    k = int(np.max(np.nonzero(passed)[0]) + 1) if passed.any() else 0
    if k:
        df.loc[ps.index[:k], "reject"] = True
    return df
