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
