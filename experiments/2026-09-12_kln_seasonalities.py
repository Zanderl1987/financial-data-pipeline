#!/usr/bin/env python3
"""
KLN monthly return seasonalities (Keloharju-Linnainmaa-Nyberg 2016, "Return
seasonalities"). Not the day-of-week paper -- the monthly-calendar-momentum /
seasonality effect: a stock's average return in a given calendar month over
past years predicts its return in the same month.

Construction:
  - monthly returns per symbol (month-end -> month-end, split-adjusted adj_close)
  - signal at (year y, month m) = mean of that symbol's month-m return over the
    past 1..10 years (excluding year y)
  - cross-sectional deciles within each month; long top decile / short bottom
  - month-end rebalance, next-month return label (PIT-safe)

Same data-caveats as the UMD experiment: yfinance_universe_prices is a 2026-08
snapshot missing ~285 Russell-3000 mega caps (small/mid-cap slice only) and
delisted names are absent -- see TASKS TODO.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PRICES = "storage/curated/yfinance_universe_prices/yfinance_universe_prices.parquet"
START = "1995-01-01"   # need multi-year history for seasonality
MIN_HISTORY = 3        # min prior same-month observations for a signal
MAX_BACK = 10          # look back up to 10 years
OUT = "storage/reports/eval/kln_seasonalities_daily.parquet"


def main():
    df = pd.read_parquet(PRICES, columns=["symbol", "date", "adj_close"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df = df[df["date"] >= "1990-01-01"]

    # month-end close per symbol; monthly return from month-end months ago
    df["ym"] = df["date"].dt.to_period("M")
    me = df.groupby(["symbol", "ym"])["date"].transform("max")
    d = df[df["date"] == me].copy()
    d = d.drop_duplicates(["symbol", "ym"]).reset_index(drop=True)
    g = d.groupby("symbol", sort=False)
    d["prev_close"] = g["adj_close"].shift(1)
    d["mret"] = d["adj_close"] / d["prev_close"] - 1.0
    d["cal_month"] = d["ym"].dt.month
    d["year"] = d["ym"].dt.year

    # signal: mean of same-calendar-month returns across prior years
    rows = []
    for sym, grp in d.groupby("symbol"):
        grp = grp.sort_values("ym")
        # pivot (year, month) -> mret, then rolling mean over past years
        piv = grp.pivot_table(index="year", columns="cal_month", values="mret")
        sig = piv.rolling(MAX_BACK, min_periods=1).mean().shift(1)  # excludes current yr
        for (yr, mo), v in sig.stack().items():
            if not np.isfinite(v):
                continue
            rows.append((sym, yr, mo, v))
    sig = pd.DataFrame(rows, columns=["symbol", "year", "cal_month", "kln"])

    d = d.merge(sig, on=["symbol", "year", "cal_month"], how="left")
    d = d[d["year"] >= 1995]
    d["kln_n"] = d.groupby(["symbol", "year", "cal_month"])["kln"].transform(
        lambda x: np.nan if x.isna().all() else 1
    )
    d = d.dropna(subset=["kln"])

    # per-month decile L/S, label = next-month return
    months = []
    for ym, grp in d.groupby("ym"):
        if len(grp) < 50 or grp["mret"].isna().all():
            continue
        q = pd.qcut(grp["kln"].rank(method="first"), 10, labels=False)
        grp = grp.assign(q=q)
        g2 = grp.groupby("q")["mret"].mean()
        if len(g2) == 10 and np.isfinite(g2).all():
            months.append((ym, g2[9] - g2[0], len(grp), g2[9], g2[0]))
    out = pd.DataFrame(months, columns=["ym", "ls", "n", "p10", "p1"])
    out["ym"] = out["ym"].astype("period[M]")
    ls = pd.Series(out["ls"].values, index=pd.PeriodIndex(out["ym"], freq="M"))
    ls = ls[ls.index.year >= 1995]

    stats = dict(
        mean=ls.mean() * 12 * 100,
        vol=ls.std() * np.sqrt(12) * 100,
        sharpe=ls.mean() / (ls.std() * np.sqrt(12)),
        t=ls.mean() / (ls.std(ddof=1) / np.sqrt(len(ls))),
        n=len(ls),
    )
    print("=== KLN monthly return seasonalities (decile L/S) ===")
    print(f"  ann.mean {stats['mean']:6.2f}%  vol {stats['vol']:5.2f}%  "
          f"Sharpe {stats['sharpe']:5.2f}  t {stats['t']:5.2f}  n={stats['n']} months")
    parts = []
    for dec in sorted(set(ls.index.year // 10 * 10)):
        sub = ls[ls.index.year // 10 * 10 == dec]
        s = sub.mean() / (sub.std() * np.sqrt(12))
        parts.append(f"{dec}s {s:.2f}")
    print(f"  decades: {', '.join(parts)}")
    print(f"  win rate: {(ls > 0).mean():.0%}  avg names/month: {out.n.mean():.0f}")
    print(f"  long-side decile10 ann {out.p10.mean()*1200:.1f}% vs "
          f"decile1 {out.p1.mean()*1200:.1f}% (equal-observed months)")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    ls2 = ls.rename("kln_ls")
    ls2.to_frame().to_parquet(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()