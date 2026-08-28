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

#: Relative floor below which a standard deviation is treated as zero.
#:
#: `sd > 0` is NOT a sufficient guard, which is the trap this constant exists
#: for. A constant series of 0.001 has an arithmetically-zero sd, but in float64
#: it comes out around 6e-19 rather than exactly 0.0 -- positive, finite, and
#: enough to produce a Sharpe of 2.4e16. That passes every `> 0` check in the
#: repo and would render as a plausible-looking huge number rather than as the
#: degenerate input it is. First caught by tests/test_tearsheet.py in the
#: tearsheet implementation; canonical home moved here (2026-08-23) so the
#: rest of the battery guards the same way.
SD_FLOOR = 1e-12


def _degenerate_sd(sd: float, scale: float = 1.0) -> bool:
    """True when `sd` is zero, non-finite, or float-noise around zero."""
    return not (np.isfinite(sd) and sd > SD_FLOOR * max(1.0, abs(scale)))


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
    if not _degenerate_sd(sd):
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
    # Welch still runs when exactly ONE bucket is flat (its variance term is
    # just 0); both buckets float-noise-flat is the degenerate case.
    if not (_degenerate_sd(sd_t) and _degenerate_sd(sd_b)):
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
    if _degenerate_sd(sd):
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
        sharpes[i] = sample.mean() / ssd * ann if not _degenerate_sd(ssd) else np.nan
    sharpes = sharpes[np.isfinite(sharpes)]
    if len(sharpes) < n_boot // 2:
        return {"sharpe": round(obs, 2), "sharpe_ci_lo": None,
                "sharpe_ci_hi": None,
                "sharpe_reason": "bootstrap degenerate (zero-variance samples)"}
    lo, hi = np.percentile(sharpes, [2.5, 97.5])
    return {"sharpe": round(obs, 2), "sharpe_ci_lo": round(float(lo), 2),
            "sharpe_ci_hi": round(float(hi), 2), "n_boot": int(len(sharpes))}


def permutation_trades(rule, cache: dict, n_perm: int = 200,
                       seed: int = 0, *, config=None) -> dict:
    """
    Permutation null for a trade system: within each symbol, relocate the
    same NUMBER of entry signals to uniformly random days (exit rule kept
    as-is), re-simulate through the same engine, and compare total P&L and
    win rate. One-sided empirical p-values with the +1 correction.

    `config` is an ExecutionConfig applied identically to the observed run and
    to every permutation -- the null must pay the same costs and obey the same
    stops as the strategy, or the comparison is rigged in the strategy's favor.
    None means LEGACY.
    """
    from evaluation import trades as tr        # local import (no cycles)
    from evaluation import execution as ev_execution
    _cfg = ev_execution.resolve(config)
    _perm_needs_portfolio = (_cfg.limits.capital is not None
                             or _cfg.limits.max_concurrent is not None
                             or _cfg.sizing.mode != "fixed_notional")
    obs = tr.simulate(rule, cache, config=config)
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
                                           sym, rule.notional, config=config))
        if _perm_needs_portfolio and rows:
            # The null must face the SAME capital budget and concurrency cap as
            # the observed run. Skipping this would let the null take every
            # trade while the strategy is rationed -- an unconstrained null is
            # a harder bar, so the test would be conservative rather than
            # anti-conservative, but it would no longer be the stated null.
            rows = tr._portfolio_pass(rows, _cfg)
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


# --------------------------------------------------------------- Tier 3


def walk_forward(panel: pd.DataFrame, value_col: str, fwd_col: str,
                 n_folds: int = 4, min_train_days: int = 126,
                 min_names: int = 5) -> dict:
    """
    Expanding-window walk-forward: the first min_train_days distinct dates
    are the initial in-sample block; the remaining dates split into n_folds
    sequential OOS folds. For unfitted signals this measures out-of-sample
    STABILITY; headline numbers are the OOS aggregate (spec rule).
    """
    sub = panel.dropna(subset=[value_col, fwd_col])
    dates = np.array(sorted(sub["date"].unique()))
    need = min_train_days + n_folds * 21
    if len(dates) < need:
        return {"oos": None, "folds": [],
                "wf_reason": f"only {len(dates)} dates (< {need})"}
    oos_dates = dates[min_train_days:]
    folds = []
    for i, chunk in enumerate(np.array_split(oos_dates, n_folds)):
        fsub = sub[sub["date"].isin(chunk)]
        d = daily_ic(fsub, value_col, fwd_col, min_names=min_names)
        folds.append({"fold": i + 1,
                      "date_range": f"{pd.Timestamp(chunk[0]).date()}"
                                    f"..{pd.Timestamp(chunk[-1]).date()}",
                      "mean_daily_ic": d.get("mean_daily_ic"),
                      "ic_t_stat": d.get("ic_t_stat"),
                      "ic_days": d.get("ic_days")})
    osub = sub[sub["date"].isin(oos_dates)]
    oos = daily_ic(osub, value_col, fwd_col, min_names=min_names)
    oos.update(quantile_spread(osub, value_col, fwd_col))
    return {"oos": oos, "folds": folds, "n_train_days": int(min_train_days)}


def regime_conditioning(panel: pd.DataFrame, value_col: str, fwd_col: str,
                        bench_close: pd.Series, min_names: int = 5,
                        sma_window: int = 200, vol_window: int = 21) -> dict:
    """
    Per-regime Tier-1 stats. Bull/bear: benchmark close >= its sma_window SMA.
    High/low vol: benchmark vol_window realized vol vs its own median.
    Regimes are assigned by SIGNAL date (info available at signal time).
    """
    b = pd.Series(bench_close).dropna()
    if len(b) < sma_window + vol_window:
        return {"regime_reason": f"benchmark history too short ({len(b)} days)"}
    sma = b.rolling(sma_window).mean()
    vol = b.pct_change().rolling(vol_window).std() * math.sqrt(252.0)
    med = vol.median()

    sub = panel.dropna(subset=[value_col, fwd_col]).copy()
    dates = pd.DatetimeIndex(pd.to_datetime(sub["date"]))
    sma_at = sma.reindex(dates).to_numpy()
    close_at = b.reindex(dates).to_numpy()
    vol_at = vol.reindex(dates).to_numpy()
    sma_ok = np.isfinite(sma_at) & np.isfinite(close_at)
    vol_ok = np.isfinite(vol_at)

    masks = {"bull": sma_ok & (close_at >= sma_at),
             "bear": sma_ok & (close_at < sma_at),
             "high_vol": vol_ok & (vol_at > med),
             "low_vol": vol_ok & (vol_at <= med)}
    out = {}
    for name, mask in masks.items():
        fsub = sub[mask]
        res = {"n_days": int(fsub["date"].nunique())}
        res.update(daily_ic(fsub, value_col, fwd_col, min_names=min_names))
        res.update(quantile_spread(fsub, value_col, fwd_col))
        out[name] = res
    return out


def deflated_sharpe(sharpe_ann, n_days: int, trial_sharpes_ann,
                    skew: float = 0.0, kurt: float = 3.0) -> dict:
    """
    Bailey & Lopez de Prado deflated Sharpe ratio. trial_sharpes_ann is the
    registry's population of previously recorded annualized Sharpes -- a
    REAL 'number of things tried' denominator instead of a guess. Returns
    dsr_prob ~ P(true SR > expected max of N null trials).
    """
    if sharpe_ann is None or not np.isfinite(sharpe_ann):
        return {"dsr_prob": None, "dsr_reason": "no observed Sharpe"}
    trials = np.asarray([s for s in trial_sharpes_ann
                         if s is not None and np.isfinite(s)], dtype=float)
    N = len(trials)
    if N < 2:
        return {"dsr_prob": None,
                "dsr_reason": f"registry population too small (n={N} < 2)"}
    if n_days < 30:
        return {"dsr_prob": None, "dsr_reason": f"only {n_days} days (< 30)"}
    daily = 1.0 / math.sqrt(252.0)
    sr = float(sharpe_ann) * daily
    var_tr = float(np.var(trials * daily, ddof=1))
    if not var_tr > 0:
        return {"dsr_prob": None,
                "dsr_reason": "zero variance across trial Sharpes"}
    gamma = 0.5772156649015329
    z = sps.norm.ppf
    sr0 = math.sqrt(var_tr) * ((1 - gamma) * z(1 - 1.0 / N)
                               + gamma * z(1 - 1.0 / (N * math.e)))
    denom_sq = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if not denom_sq > 0:
        return {"dsr_prob": None, "dsr_reason": "invalid skew/kurtosis adjustment"}
    stat = (sr - sr0) * math.sqrt(n_days - 1) / math.sqrt(denom_sq)
    return {"dsr_prob": round(float(sps.norm.cdf(stat)), 4),
            "sr0_ann": round(float(sr0 / daily), 3), "n_trials": int(N)}


def registry_percentile(value, population) -> dict:
    """Where does `value` sit in the registry's population of the same stat?"""
    pop = np.asarray([v for v in population
                      if v is not None and np.isfinite(v)], dtype=float)
    if len(pop) < 2:
        return {"percentile": None,
                "pct_reason": f"population too small (n={len(pop)})"}
    return {"percentile": round(100.0 * float((pop <= value).mean()), 1),
            "n_population": int(len(pop))}


# --------------------------------------------------------------- Risk & Factor Extensions

def sortino_ratio(ret, rf: float = 0.0, ann: float = 252.0) -> dict:
    """Annualized Sortino ratio (downside deviation risk-adjusted return)."""
    s = pd.Series(ret).dropna()
    if len(s) < 5:
        return {"sortino": None, "sortino_reason": f"fewer than 5 data points (n={len(s)})"}
    downside = s[s < rf] - rf
    if len(downside) == 0:
        return {"sortino": None, "sortino_reason": "no downside returns below threshold"}
    down_vol = float(np.sqrt(np.mean(downside ** 2)) * np.sqrt(ann))
    if down_vol <= 0 or not np.isfinite(down_vol):
        return {"sortino": None, "sortino_reason": "zero downside volatility"}
    mean_excess = float((s.mean() - rf) * ann)
    return {"sortino": round(float(mean_excess / down_vol), 2),
            "downside_vol_pct": round(100.0 * down_vol, 2)}


def calmar_ratio(cagr_pct: "float | None", max_drawdown_pct: "float | None") -> dict:
    """Calmar ratio: CAGR / |Max Drawdown|."""
    if cagr_pct is None or max_drawdown_pct is None or not np.isfinite(cagr_pct) or not np.isfinite(max_drawdown_pct):
        return {"calmar": None, "calmar_reason": "missing CAGR or Max Drawdown"}
    mdd = abs(float(max_drawdown_pct))
    if mdd <= 0:
        return {"calmar": None, "calmar_reason": "zero max drawdown"}
    return {"calmar": round(float(cagr_pct / mdd), 2)}


def omega_ratio(ret, threshold: float = 0.0) -> dict:
    """Omega ratio: sum of gains above threshold / sum of losses below threshold."""
    s = pd.Series(ret).dropna()
    if len(s) < 5:
        return {"omega": None, "omega_reason": f"fewer than 5 data points (n={len(s)})"}
    gains = float((s[s > threshold] - threshold).sum())
    losses = float((threshold - s[s < threshold]).sum())
    if losses <= 0:
        return {"omega": None, "omega_reason": "no losses below threshold"}
    return {"omega": round(float(gains / losses), 2)}


def value_at_risk(ret, alpha: float = 0.05) -> dict:
    """Value at Risk (VaR) at alpha quantile (e.g. 5% worst loss)."""
    s = pd.Series(ret).dropna()
    if len(s) < 10:
        return {"var_95_pct": None, "var_reason": f"fewer than 10 data points (n={len(s)})"}
    q = float(np.percentile(s, alpha * 100))
    return {"var_95_pct": round(100.0 * float(-q), 2)}


def conditional_var(ret, alpha: float = 0.05) -> dict:
    """Conditional Value at Risk (CVaR / Expected Shortfall) beyond alpha quantile."""
    s = pd.Series(ret).dropna()
    if len(s) < 10:
        return {"cvar_95_pct": None, "cvar_reason": f"fewer than 10 data points (n={len(s)})"}
    q = float(np.percentile(s, alpha * 100))
    tail = s[s <= q]
    if len(tail) == 0:
        return {"cvar_95_pct": None, "cvar_reason": "empty tail"}
    return {"cvar_95_pct": round(100.0 * float(-tail.mean()), 2)}


def gain_to_pain_ratio(ret) -> dict:
    """Schwager's Gain-to-Pain ratio: sum of all returns / absolute sum of negative returns."""
    s = pd.Series(ret).dropna()
    if len(s) < 5:
        return {"gain_to_pain": None, "gtp_reason": f"fewer than 5 data points (n={len(s)})"}
    total_gain = float(s[s > 0].sum())
    total_loss = float(abs(s[s < 0].sum()))
    if total_loss <= 0:
        return {"gain_to_pain": None, "gtp_reason": "zero total loss"}
    return {"gain_to_pain": round(float((total_gain - total_loss) / total_loss), 2)}


def fama_french_factor_attribution(ret_series: pd.Series, start: "str | None" = None,
                                   end: "str | None" = None) -> dict:
    """
    OLS Factor Attribution against Fama-French factors.
    Returns alpha, beta_mkt, beta_smb, beta_hml, r_squared or ff_reason string.
    """
    s = pd.Series(ret_series).dropna()
    if len(s) < 30:
        return {"ff_alpha": None, "ff_reason": f"fewer than 30 return observations (n={len(s)})"}
    try:
        import query as q
        df = q.load("ff_factors", start=start, end=end)
        if df.empty:
            return {"ff_alpha": None, "ff_reason": "ff_factors dataset empty"}
        freq_df = df[df["frequency"].isin(["daily", "monthly"])]
        if freq_df.empty:
            return {"ff_alpha": None, "ff_reason": "no daily/monthly Fama-French factors available in storage"}
        piv = (freq_df.drop_duplicates(["date", "factor"])
               .pivot(index="date", columns="factor", values="value") / 100.0)
        piv.index = pd.to_datetime(piv.index)
        s.index = pd.to_datetime(s.index)
        aligned = pd.concat([s.rename("strategy"), piv], axis=1, join="inner").dropna()
        if len(aligned) < 30:
            return {"ff_alpha": None, "ff_reason": f"fewer than 30 overlapping dates (n={len(aligned)})"}
        factors = [c for c in ["Mkt-RF", "SMB", "HML", "RMW", "CMA"] if c in aligned.columns]
        if not factors:
            return {"ff_alpha": None, "ff_reason": "required factor columns missing"}
        rf = aligned["RF"] if "RF" in aligned.columns else 0.0
        y = aligned["strategy"] - rf
        X = aligned[factors].copy()
        X.insert(0, "Alpha", 1.0)
        coefs, _, _, _ = np.linalg.lstsq(X.values, y.values, rcond=None)
        y_pred = X.values @ coefs
        ss_tot = float(np.sum((y.values - y.values.mean()) ** 2))
        ss_res = float(np.sum((y.values - y_pred) ** 2))
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
        res = {
            "ff_alpha_ann": round(float(coefs[0] * 252.0 * 100), 2),
            "ff_r_squared": round(float(r2), 4),
        }
        for i, f in enumerate(factors, start=1):
            res[f"beta_{f.lower().replace('-', '_')}"] = round(float(coefs[i]), 3)
        return res
    except Exception as e:
        return {"ff_alpha": None, "ff_reason": f"Fama-French attribution error: {e}"}

