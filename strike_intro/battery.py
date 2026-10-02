"""HIT-vs-MISS significance battery (spec: Evaluation)."""
import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats

MIN_N = 10


def _nan_result(a, b):
    return {"n_a": len(a), "n_b": len(b), "mean_a": np.nanmean(a) if len(a) else np.nan,
            "mean_b": np.nanmean(b) if len(b) else np.nan, "median_a": np.nan,
            "median_b": np.nan, "cont_a": np.nan, "cont_b": np.nan, "diff": np.nan,
            "ci_lo": np.nan, "ci_hi": np.nan, "p_welch": np.nan, "p_perm": np.nan,
            "p_boot": np.nan, "p_mw": np.nan, "cohen_d": np.nan}


def compare_groups(a, b, strata_a=None, strata_b=None, weeks_a=None, weeks_b=None,
                   n_perm=10000, n_boot=2000, seed=0, progress=None, label="") -> dict:
    a, b = np.asarray(a, float), np.asarray(b, float)
    ka, kb = np.isfinite(a), np.isfinite(b)
    a, b = a[ka], b[kb]
    strata_a = None if strata_a is None else np.asarray(strata_a)[ka]
    strata_b = None if strata_b is None else np.asarray(strata_b)[kb]
    weeks_a = None if weeks_a is None else np.asarray(weeks_a)[ka]
    weeks_b = None if weeks_b is None else np.asarray(weeks_b)[kb]
    if len(a) < MIN_N or len(b) < MIN_N:
        return _nan_result(a, b)
    rng = np.random.default_rng(seed)
    diff = a.mean() - b.mean()
    pooled = np.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1))
                     / (len(a) + len(b) - 2))

    # permutation: shuffle labels within strata, vectorised (lexsort by
    # (random key, stratum) = an independent random order inside every stratum)
    vals = np.r_[a, b]
    lab = np.r_[np.ones(len(a), bool), np.zeros(len(b), bool)]
    strata = (np.r_[strata_a, strata_b] if strata_a is not None and strata_b is not None
              else np.zeros(len(vals)))
    codes = pd.factorize(strata)[0]
    base = np.lexsort((np.arange(len(vals)), codes))
    null = np.empty(n_perm)
    for i in range(n_perm):
        order = np.lexsort((rng.random(len(vals)), codes))
        perm = np.empty_like(lab)
        perm[base] = lab[order]
        null[i] = vals[perm].mean() - vals[~perm].mean()
        if progress is not None and (i + 1) % 1000 == 0:
            progress.stage("permutation", i + 1, n_perm, label)
            progress.put("perm_null", {"label": label, "observed": float(diff),
                                       "sample": null[: i + 1][-2000:].tolist()})
    p_perm = (1 + np.sum(np.abs(null) >= abs(diff))) / (n_perm + 1)

    # week-block bootstrap, vectorised: resample weeks via a multinomial count
    # matrix and recombine per-week sums
    wa = weeks_a if weeks_a is not None else np.arange(len(a))
    wb = weeks_b if weeks_b is not None else np.arange(len(b)) + 10**9
    codes, uniq = pd.factorize(np.r_[wa, wb])
    ca, cb = codes[:len(a)], codes[len(a):]
    W = len(uniq)
    sa, na = np.bincount(ca, a, W), np.bincount(ca, minlength=W)
    sb, nb = np.bincount(cb, b, W), np.bincount(cb, minlength=W)
    K = rng.multinomial(W, np.full(W, 1.0 / W), size=n_boot)
    with np.errstate(invalid="ignore", divide="ignore"):
        boots = (K @ sa) / (K @ na) - (K @ sb) / (K @ nb)
    boots = boots[np.isfinite(boots)]
    p_boot = float(min(1.0, 2 * min((boots <= 0).mean(), (boots >= 0).mean())))

    return {
        "n_a": len(a), "n_b": len(b), "mean_a": a.mean(), "mean_b": b.mean(),
        "median_a": np.median(a), "median_b": np.median(b),
        "cont_a": (a > 0).mean(), "cont_b": (b > 0).mean(), "diff": diff,
        "ci_lo": float(np.percentile(boots, 2.5)), "ci_hi": float(np.percentile(boots, 97.5)),
        "p_welch": float(stats.ttest_ind(a, b, equal_var=False).pvalue),
        "p_perm": float(p_perm), "p_boot": p_boot,
        "p_mw": float(stats.mannwhitneyu(a, b, alternative="two-sided").pvalue),
        "cohen_d": float(diff / pooled) if pooled > 0 else np.nan,
    }


def mde(n_a, n_b, sd, alpha=0.05, power=0.8) -> float:
    z = stats.norm.ppf(1 - alpha / 2) + stats.norm.ppf(power)
    return float(z * sd * np.sqrt(1 / n_a + 1 / n_b))


def clustered_ols(df, y, xcols, cluster_cols=("entry_date", "symbol")) -> pd.DataFrame:
    d = df.dropna(subset=[y] + list(xcols))
    cols = [c for c in xcols if d[c].nunique() > 1]
    X = sm.add_constant(d[cols].astype(float), has_constant="add")
    groups = np.column_stack([pd.factorize(d[c])[0] for c in cluster_cols])
    res = sm.OLS(d[y].astype(float), X).fit(cov_type="cluster", cov_kwds={"groups": groups})
    return pd.DataFrame({"term": res.params.index, "coef": res.params.values,
                         "se": res.bse.values, "p": res.pvalues.values})


def trading_stats(trades, daily, hold=21, cost_bp=10) -> dict:
    """Illustrative strategy from overlapping positions: each trade holds
    `hold` days after its entry close, equal-weighted across open positions;
    cost_bp is charged on entry and exit. `daily` = date x symbol daily excess
    returns; trades carry symbol, entry_date, direction."""
    idx = daily.index
    cost = cost_bp / 1e4
    contrib = pd.DataFrame(0.0, index=idx, columns=range(len(trades)))
    active = pd.DataFrame(False, index=idx, columns=range(len(trades)))
    trade_ret = []
    for k, (_, t) in enumerate(trades.iterrows()):
        i = idx.get_loc(t["entry_date"])
        days = idx[i + 1:i + 1 + hold]
        if len(days) == 0:
            trade_ret.append(np.nan)
            continue
        r = t["direction"] * daily.loc[days, t["symbol"]].fillna(0.0)
        r.iloc[0] -= cost
        r.iloc[-1] -= cost
        contrib.loc[days, k] = r.values
        active.loc[days, k] = True
        trade_ret.append(float(r.sum()))
    n_open = active.sum(axis=1)
    port = (contrib.sum(axis=1) / n_open.replace(0, np.nan)).dropna()
    equity = (1 + port).cumprod() - 1
    tr = pd.Series(trade_ret).dropna()
    wins, losses = tr[tr > 0].sum(), -tr[tr < 0].sum()
    dd = ((1 + equity) / (1 + equity).cummax() - 1).min() if len(equity) else np.nan
    sd = port.std()
    return {"n_trades": int(len(tr)), "win_rate": float((tr > 0).mean()) if len(tr) else np.nan,
            "profit_factor": float(wins / losses) if losses > 0 else np.nan,
            "sharpe": float(port.mean() / sd * np.sqrt(252)) if sd and sd > 0 else np.nan,
            "max_drawdown": float(dd), "equity": equity}
