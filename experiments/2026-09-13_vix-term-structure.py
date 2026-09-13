#!/usr/bin/env python3
"""
VIX term-structure slope (VTSL) overlay + Cboe options-strategy index backtest.

Data (all keyless, in-house):
  - cboe_volatility: VIX (1990+), VIX9D (2011+), VIX3M (2009+), VIX6M (2008+)
  - cboe_strategy_indices: PUT / WPUT / PUTR / BXM / BXMD / BXD / BXN / CLL /
    CNDR / BFLY / CMBO (1986+ ... 2009+)
  - market_history ^GSPC (1927+) as the S&P 500 benchmark

SLOPE (Johnson 2017 JFQA, "Risk Premia and the VIX Term Structure"): per date,
OLS regression of ln(implied-vol index level) on time-to-maturity in months
across the available curve points {VIX9D:0.30, VIX:1.0, VIX3M:3.0, VIX6M:6.0}.
This is the implied-volatility curve analogue of the paper's VIX-futures-curve
slope: positive (contango) = market pricing rising vol ahead; negative
(backwardation) = stress, near-term vol expensive.

Potential exposures of SLOPE: Johnson finds it forecasts variance-swap returns
(high/contango slope -> reward for being SHORT vol; inverted slope -> stress).
We test it three ways, all PIT (signal at month-end close t0, position from
t0+1, held to next month-end):

  A) Buy-and-hold each of the 11 strategy indices and SPX bench (deciles by
     decade): pure long-only reference, incl. vol-targeted to 10% for fairness.
  B) SLOPE-timed overlay on SPX and on the vol-selling strategies (PUT, BXM):
     long when slope > 0, flat (cash) when slope <= 0. Also vs 3m slope MA.
  C) Slope-quintile conditioning: mean next-month return of SPX / PUT / BXM per
     slope quintile (does the slope sort monthly returns?).

Published Cboe index levels embed option execution costs only at the
proprietary-signature level; we treat them as gross-of-cost but NOTE the
vol-targeting/timing adds realistic turnover.
"""
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANN = 252

VOL_SERIES = {
    "VIX9D": 0.30,
    "VIX": 1.0,
    "VIX3M": 3.0,
    "VIX6M": 6.0,
}
INDEXES = ["PUT", "WPUT", "PUTR", "BXM", "BXMD", "BXD", "BXN",
           "CLL", "CNDR", "BFLY", "CMBO"]
OUT = "storage/reports/eval/vix_term_structure_daily.parquet"


def load_daily() -> pd.DataFrame:
    import query
    v = query.load("cboe_volatility")[["date", "index_name", "close"]]
    v["date"] = pd.to_datetime(v["date"])
    vol = v.pivot_table(index="date", columns="index_name", values="close")
    s = query.load("cboe_strategy_indices")[["date", "index_name", "close"]]
    s["date"] = pd.to_datetime(s["date"])
    strat = s.pivot_table(index="date", columns="index_name", values="close")
    mh = query.load("market_history")
    spx = (mh[mh["symbol"] == "^GSPC"][["date", "close"]]
           .copy().assign(date=pd.to_datetime(mh.loc[mh["symbol"] == "^GSPC", "date"])))
    spx = spx.set_index("date").rename(columns={"close": "SPX"}).sort_index()
    out = vol.join(strat, how="inner").join(spx, how="left").sort_index()
    out["SPX"] = out["SPX"].ffill()
    # mask sparse pre-launch tails per index (e.g. PUT had 1-2 rows/yr 1991-2004)
    for c in INDEXES:
        cnt = out[c].groupby(out[c].index.year).count()
        dense_years = [y for y in cnt.index if cnt.loc[y] >= 100]
        if not dense_years:
            out[c] = np.nan
            continue
        start = pd.Timestamp(f"{min(dense_years)}-01-01")
        out.loc[out.index < start, c] = np.nan
    return out[["VIX9D", "VIX", "VIX3M", "VIX6M"] + INDEXES + ["SPX"]]


def daily_slope(vol: pd.DataFrame, maturity: dict) -> pd.Series:
    """OLS beta of ln(level) on maturity-months, using available curve points."""
    tau = np.asarray([maturity[c] for c in maturity if c in vol.columns])
    xs = np.column_stack([np.ones_like(tau), tau])
    slopes = {}
    for d, row in vol.iterrows():
        lev = np.asarray([row[c] for c in maturity if c in vol.columns], dtype=float)
        ok = np.isfinite(lev) & (lev > 0)
        if ok.sum() < 2:
            slopes[d] = np.nan
            continue
        X, y = xs[ok], np.log(lev[ok])
        XtX = X.T @ X
        if np.linalg.cond(XtX) > 1e12 or np.linalg.det(XtX) == 0:
            slopes[d] = np.nan
            continue
        beta = np.linalg.solve(XtX, X.T @ y)
        slopes[d] = beta[1]
    return pd.Series(slopes, index=vol.index)


def ann_stats(daily: pd.Series) -> dict:
    r = daily.dropna()
    if len(r) < 3 or r.std(ddof=1) == 0:
        return dict(mean=np.nan, vol=np.nan, sharpe=np.nan, maxdd=np.nan, n=len(r))
    mean, vol = r.mean() * ANN, r.std(ddof=1) * math.sqrt(ANN)
    eq = (1 + r).cumprod()
    return dict(mean=mean, vol=vol, sharpe=mean / vol,
                maxdd=(eq / eq.cummax() - 1).min(), n=len(r))


def decade_str(daily: pd.Series) -> str:
    d = pd.Series(daily).sort_index()
    return ", ".join(
        f"{y}s {ann_stats(d[d.index.year // 10 * 10 == y])['sharpe']:.2f}"
        for y in sorted(set(d.index.year // 10 * 10)))


def vol_target(daily_ret: pd.Series, target: float = 0.10,
               floor: float = 0.05) -> pd.Series:
    v = daily_ret.rolling(252).std(ddof=1) * math.sqrt(ANN)
    v = v.clip(lower=floor)
    return daily_ret * target / v.shift(1)


def month_positions(signal: pd.Series, ret: pd.Series,
                    threshold: float = 0.0) -> pd.Series:
    """Position from month-end signal: 1 if signal>threshold else 0, effective t0+1."""
    sig = signal.dropna()
    me = pd.Series(sig.index, index=sig.index).resample("ME").last().dropna()
    pos = pd.Series(np.nan, index=ret.index)
    for m0 in me:
        nxt = me[me > m0]
        end = nxt.iloc[0] if len(nxt) else ret.index[-1]
        pos.loc[m0:] = (1.0 if sig.loc[m0] > threshold else 0.0)
        pos.loc[end:] = np.nan
    return pos.shift(1).fillna(0.0)


def main():
    df = load_daily()
    print(f"curve sample: {df['VIX'].notna().sum():,} days VIX "
          f"({df['VIX'].first_valid_index().date()} -> {df.index[-1].date()})")
    for c in ("VIX9D", "VIX3M", "VIX6M"):
        fv = df[c].first_valid_index()
        print(f"  {c}: {len(df[c].dropna()):,} days, starts {fv.date()}")

    slope = daily_slope(df[["VIX9D", "VIX", "VIX3M", "VIX6M"]], VOL_SERIES)
    print(f"SLOPE 3+ point curve: {slope.notna().sum():,} days "
          f"({slope.first_valid_index().date() if slope.notna().any() else '-'} -> {slope.index[-1].date()})")

    ret = df.pct_change()
    spx_ret = ret["SPX"]

    # ---- A) buy-and-hold vs SPX (incl. vol-targeted) ----
    print("\n=== A) Long-only: strategy indices vs SPX (daily, common sample SPX) ===")
    hdr = f"{'index':6s} {'ann%':>7s} {'vol%':>6s} {'Sharpe':>7s} {'maxDD':>7s} {'decades (Sharpe)':>30s}  VT10"
    print(hdr)
    for idx in INDEXES + ["SPX"]:
        r = ret[idx]
        st = ann_stats(r)
        vt = ann_stats(vol_target(r))
        print(f"{idx:6s} {st['mean']*100:7.2f} {st['vol']*100:6.1f} "
              f"{st['sharpe']:7.2f} {st['maxdd']*100:6.1f}%  "
              f"{decade_str(r):>30s}  {vt['sharpe']:.2f}")

    # ---- B) SLOPE-timed overlay (both directions, same sample as slope) ----
    print("\n=== B) SLOPE-timed overlay, same-sample as slope (2008+) ===")
    for idx, label in [("SPX", "SPX"), ("PUT", "PUT (putwrite)"), ("BXM", "BXM (buywrite)")]:
        r = ret[idx]
        bh = ann_stats(r.loc[slope.first_valid_index():])
        up = r * month_positions(slope, r)
        dn = r * month_positions(-slope, r)
        tu, td = ann_stats(up), ann_stats(dn)
        print(f"  {label:16s} same-sample B&H Sharpe {bh['sharpe']:5.2f} "
              f"ann {bh['mean']*100:6.2f}% vol {bh['vol']*100:5.1f}%")
        print(f"      TIMED long-if-slope>0  Sharpe {tu['sharpe']:5.2f} ann {tu['mean']*100:6.2f}% "
              f"vol {tu['vol']*100:5.1f}% maxDD {tu['maxdd']*100:6.1f}%")
        print(f"      TIMED long-if-slope<=0 Sharpe {td['sharpe']:5.2f} ann {td['mean']*100:6.2f}% "
              f"vol {td['vol']*100:5.1f}% maxDD {td['maxdd']*100:6.1f}%")

    # ---- C) quintile conditioning ----
    print("\n=== C) Slope-quintile -> next-month return ===")
    sig = slope.dropna()
    me = list(pd.Series(sig.index, index=sig.index).resample("ME").last().dropna().values)
    rows = []
    for i, m0 in enumerate(me[:-1]):
        m1 = me[i + 1]
        nxt = ret.loc[m0:m1].dropna()
        if not len(nxt):
            continue
        rows.append((m0, sig.loc[m0], (1 + nxt["SPX"]).prod() - 1,
                     (1 + nxt["PUT"]).prod() - 1, (1 + nxt["BXM"]).prod() - 1))
    m = pd.DataFrame(rows, columns=["date", "slope", "SPX", "PUT", "BXM"])
    m["q"] = pd.qcut(m["slope"], 5, labels=False)
    print("  quintile (1=flat/backwardation, 5=steep contango): next-month avg")
    for q in range(5):
        sub = m[m["q"] == q]
        print(f"    q{q+1} slope mean {sub['slope'].mean():+.3f}  "
              f"SPX {sub['SPX'].mean()*100:+6.2f}%  PUT {sub['PUT'].mean()*100:+6.2f}%  "
              f"BXM {sub['BXM'].mean()*100:+6.2f}%  (n={len(sub)})")
    from scipy import stats
    for c in ("SPX", "PUT", "BXM"):
        rho, p = stats.spearmanr(m["slope"], m[c])
        print(f"    Spearman(slope, next-{c}) = {rho:+.3f} (p={p:.3f})")

    # ---- D) walk-forward OOS on the PUT/BXM overlay ----
    print("\n=== D) Slope>k overlay sensitivity (full sample, PUT/BXM) ===")
    thresh_grid = [0.0, 0.02, 0.04]
    for idx in ("PUT", "BXM"):
        r = ret[idx]
        bh_ = ann_stats(r.loc[slope.first_valid_index():])
        print(f"  {idx}: same-sample B&H Sharpe {bh_['sharpe']:.2f} ann {bh_['mean']*100:.2f}% "
              f"vol {bh_['vol']*100:.1f}% maxDD {bh_['maxdd']*100:.1f}%")
        for k in thresh_grid:
            o = r * month_positions(slope, r, threshold=k)
            t_ = ann_stats(o)
            print(f"    slope> {k:+.2f}: timed Sharpe {t_['sharpe']:6.2f} "
                  f"ann {t_['mean']*100:6.2f}% vol {t_['vol']*100:5.1f}% "
                  f"maxDD {t_['maxdd']*100:6.1f}%")

    # ---- E) out-of-sample discipline ----
    print("\n=== E) OOS discipline (fixed a-priori rule k=0, no fitting) ===")
    start = slope.first_valid_index()
    half_ts = start + (df.index[-1] - start) / 2
    print(f"  train/dev sample: {start.date()} -> {half_ts.date()} | OOS holdout: {half_ts.date()} -> {df.index[-1].date()}")
    for idx in ("PUT", "BXM", "SPX"):
        r = ret[idx]
        timed = r * month_positions(slope, r, threshold=0.0)
        for label, lo, hi in [("dev ", start, half_ts), ("OOS ", half_ts, df.index[-1])]:
            bb = ann_stats(r.loc[lo:hi])
            tt = ann_stats(timed.loc[lo:hi])
            print(f"  {idx} {label}: B&H Sharpe {bb['sharpe']:5.2f} (ann {bb['mean']*100:5.2f}%)  "
                  f"TIMED Sharpe {tt['sharpe']:5.2f} (ann {tt['mean']*100:5.2f}%)  "
                  f"vol {tt['vol']*100:4.1f}%  maxDD {tt['maxdd']*100:6.1f}%  n={tt['n']}")

    print("\n  per-decade (TIMED vs B&H Sharpe) on k=0:")
    for idx in ("PUT", "BXM", "SPX"):
        r = ret[idx]
        timed = r * month_positions(slope, r, threshold=0.0)
        parts = []
        for y in sorted(set(df.index.year // 10 * 10)):
            band = df.index.year // 10 * 10 == y
            bb = ann_stats(r[band]); tt = ann_stats(timed[band])
            if tt["n"] < 200 or not np.isfinite(tt["sharpe"]):
                continue
            parts.append(f"{y}s bh={bb['sharpe']:.2f}->{tt['sharpe']:.2f}")
        print(f"  {idx}: " + ", ".join(parts))

    # -- persist --
    pos = month_positions(slope, ret["SPX"])
    out = pd.DataFrame({
        "slope": slope,
        "spx_timed_pos": pos,
        "put": ret["PUT"],
        "bxm": ret["BXM"],
        "spx": spx_ret,
    })
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out.to_parquet(OUT)
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()