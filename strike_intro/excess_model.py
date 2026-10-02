"""
How many strike extensions would mechanics alone predict? Poisson GLM on
covariates known at the close of t-1, fit leave-one-symbol-out so no event's
own symbol informs its expectation (spec: Excess measure).
"""
import numpy as np
import pandas as pd
import statsmodels.api as sm

from strike_intro.events import excess_returns

FEATURES = ["headroom", "move_toward", "sigma60", "earnings", "days_since_monthly", "log_price"]


def _days_since_monthly(index: pd.DatetimeIndex) -> pd.Series:
    third_fri = {(d.year, d.month): d for d in
                 pd.date_range(index.min() - pd.Timedelta(days=40), index.max(),
                               freq="WOM-3FRI")}
    out = []
    for d in index:
        f = third_fri.get((d.year, d.month))
        if f is None or d <= f:
            prev = (d.replace(day=1) - pd.Timedelta(days=1))
            f = third_fri.get((prev.year, prev.month), f)
        out.append(int(np.busday_count(f.date(), d.date())) if f is not None else 0)
    return pd.Series(out, index=index)


def build_model_panel(intros, close, earnings_near, bench="SPY", vol_window=60) -> pd.DataFrame:
    ex = excess_returns(close, bench)
    sigma = ex.rolling(vol_window, min_periods=vol_window).std().shift(1)
    dsm = _days_since_monthly(close.index)
    pos = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(close.index)}
    rows = []
    for sym, g in intros.groupby("symbol"):
        if sym not in close.columns:
            continue
        g = g.sort_values("date").reset_index(drop=True)
        prev = g.shift(1)
        for i, r in g.iterrows():
            t = pos.get(str(r["date"])[:10])
            if not r["valid"] or t is None or t == 0 or i == 0:
                continue
            c_prev = close[sym].iloc[t - 1]
            sd = sigma[sym].iloc[t - 1]
            mv = ex[sym].iloc[t - 1]
            if not np.isfinite(c_prev) or not np.isfinite(sd) or sd == 0:
                continue
            base = {"symbol": sym, "date": str(r["date"])[:10], "sigma60": sd,
                    "earnings": int((sym, close.index[t]) in earnings_near),
                    "days_since_monthly": int(dsm.iloc[t]), "log_price": float(np.log(c_prev))}
            hi, lo = prev.loc[i, "near_max"], prev.loc[i, "near_min"]
            if np.isfinite(hi):
                rows.append({**base, "side": "up", "y": r["above"],
                             "headroom": (hi - c_prev) / c_prev, "move_toward": mv / sd})
            if np.isfinite(lo):
                rows.append({**base, "side": "down", "y": r["below"],
                             "headroom": (c_prev - lo) / c_prev, "move_toward": -mv / sd})
    return pd.DataFrame(rows)


def _design(df, cols):
    return sm.add_constant(df[cols].astype(float), has_constant="add")


def fit_loso(panel, progress=None) -> pd.DataFrame:
    out = panel.copy()
    out["expected"] = np.nan
    syms = sorted(out["symbol"].unique())
    coefs = []
    for i, sym in enumerate(syms, 1):
        train = out[out["symbol"] != sym]
        cols = [c for c in FEATURES if train[c].nunique() > 1]
        model = sm.GLM(train["y"].astype(float), _design(train, cols),
                       family=sm.families.Poisson()).fit()
        test = out["symbol"] == sym
        out.loc[test, "expected"] = model.predict(_design(out[test], cols))
        coefs.append({"symbol": sym, **{k: float(v) for k, v in model.params.items()}})
        if progress is not None:
            progress.stage("excess_model", i, len(syms), sym)
            progress.put("loso_coefs", coefs)
    return out


def event_excess(events, panel, close_index, window=(1, 2)) -> pd.Series:
    exp = panel.set_index(["symbol", "date", "side"])["expected"]
    vals = []
    for _, e in events.iterrows():
        loc = close_index.get_loc(e["event_date"])
        side = "up" if e["direction"] > 0 else "down"
        total = 0.0
        for k in range(window[0], window[1] + 1):
            if loc + k >= len(close_index):
                total = np.nan
                break
            total += exp.get((e["symbol"], close_index[loc + k].strftime("%Y-%m-%d"), side), np.nan)
        vals.append(e["dir_intro"] - total)
    return pd.Series(vals, index=events.index)
