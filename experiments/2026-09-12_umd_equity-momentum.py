#!/usr/bin/env python3
"""
Cross-sectional equity momentum (UMD) backtest on the in-house equity prices.

Universe: storage/curated/yfinance_universe_prices (Russell 3000 constituents,
split-adjusted adj_close, yfinance backfill built 2026-08-08).

Construction (Jegadeesh-Titman with skip):
  - formation window: 12m return skipping the most recent month (t-252 -> t-21
    closes), secondary variants r6-1 (t-126 -> t-21) and r12-0 (no skip).
  - cross-section within each month-end; 1%/99% winsorize before ranking.
  - deciles by past return; long top decile / short bottom decile, equal weight.
  - rebalance at month-end, hold ~22 trading days; PIT-safe (signals use only
    closes on or before t-21; label = close[t]/close[t+22]).
  - eligibility per month-end: close >= $5, trailing-21d avg dollar volume
    >= $10M (computed PIT), full formation window present.

Outputs to stdout a summary block; detailed series saved to
storage/reports/eval/umd_equity_momentum_daily.parquet
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PRICES = "storage/curated/yfinance_universe_prices/yfinance_universe_prices.parquet"
START = "1990-01-01"
PRICE_FLOOR = 5.0
DOLLAR_VOL_FLOOR = 1.0e7
SHIFT_1M = 21
SHIFT_6M = 126
SHIFT_12M = 252
OUT = "storage/reports/eval/umd_equity_momentum_daily.parquet"


def annualized(monthly):
    m = monthly.dropna()
    if len(m) < 2:
        return dict(mean=np.nan, vol=np.nan, sharpe=np.nan, t=np.nan, n=len(m))
    mean = m.mean()
    vol = m.std(ddof=1)
    sharpe = mean / vol * np.sqrt(12) if vol > 0 else np.nan
    t = mean / (vol / np.sqrt(len(m))) if vol > 0 else np.nan
    return dict(mean=mean * 12, vol=vol * np.sqrt(12), sharpe=sharpe, t=t, n=len(m))


def decade_stats(monthly):
    m = pd.Series(monthly).sort_index().dropna()
    if m.empty:
        return ""
    parts = []
    for d in sorted(set(m.index.year // 10 * 10)):
        stats = annualized(m[m.index.year // 10 * 10 == d])
        parts.append(f"{d}s {stats['sharpe']:.2f} (t {stats['t']:.2f})")
    return ", ".join(parts)


def main():
    df = pd.read_parquet(PRICES, columns=["symbol", "date", "adj_close", "volume"])
    df["date"] = pd.to_datetime(df["date"])

    present = set(df["symbol"].unique())
    try:
        import query as q

        sec = q.load("securities")
        if sec is not None and "is_russell3000" in sec.columns:
            r3 = set(sec.loc[sec.get("is_russell3000") == True, "symbol"])
            missing = sorted(r3 - present)
            if missing:
                print(f"WARNING universe hole: {len(missing)} Russell-3000 names absent "
                      f"(megacaps incl. " + ", ".join(missing[:12]) + ") -- "
                      f"this backtest covers the small/mid-cap slice only; see TASKS TODO "
                      f"(backfill the missing 285 before any cross-sectional factor backtest).")
    except Exception as e:
        print(f"(universe-hole check skipped: {type(e).__name__}: {e})")

    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)

    g = df.groupby("symbol", sort=False)
    df["close"] = df["adj_close"].astype(float)
    df["prev1m"] = g["close"].shift(SHIFT_1M)      # t-21 close
    df["prev6m"] = g["close"].shift(SHIFT_6M)      # t-126 close
    df["prev12m"] = g["close"].shift(SHIFT_12M)    # t-252 close
    df["next1m"] = g["close"].shift(-SHIFT_1M - 1)  # t+22 close (hold ~22 days)
    df["dvol21"] = (
        g["close"].rolling(SHIFT_1M, min_periods=SHIFT_1M).mean().reset_index(level=0, drop=True)
        * df["volume"]
    )

    df = df[df["date"] >= START]
    month_ends = df.groupby(df["date"].dt.to_period("M"))["date"].transform("max")
    df = df[df["date"] == month_ends].copy()

    df["r12_1"] = df["prev12m"] / df["prev1m"] - 1.0
    df["r6_1"] = df["prev6m"] / df["prev1m"] - 1.0
    df["r12_0"] = df["prev12m"] / df["close"] - 1.0
    df["f_ret"] = df["next1m"] / df["close"] - 1.0

    elig = (
        (df["close"] >= PRICE_FLOOR)
        & (df["dvol21"] >= DOLLAR_VOL_FLOOR)
        & df[["r12_1", "r6_1", "r12_0", "f_ret"]].notna().all(axis=1)
    )
    sub = df[elig].copy()
    print(f"rows     : {len(df):,} month-end observations "
          f"({df.date.min():%Y-%m-%d} -> {df.date.max():%Y-%m-%d})")
    print(f"eligible : {len(sub):,} stock-months entering a universe")

    results = {}
    for name, sig_col in [("r12_1", "r12_1"), ("r6_1", "r6_1"), ("r12_0", "r12_0")]:
        s = sub.assign(sig=sub[sig_col], w=sig_col)
        q1 = s["sig"].quantile(0.01)
        q99 = s["sig"].quantile(0.99)
        s["sig"] = s["sig"].clip(q1, q99)
        s["decile"] = s.groupby("date")["sig"].transform(
            lambda x: pd.qcut(x.rank(method="first"), 10, labels=False, duplicates="drop")
        ).astype("Int64")

        months = (
            s.groupby("date")
            .apply(
                lambda d: pd.Series(
                    {
                        "d10": float(d.loc[d["decile"] == 9, "f_ret"].mean())
                        if (d["decile"] == 9).any()
                        else np.nan,
                        "d1": float(d.loc[d["decile"] == 0, "f_ret"].mean())
                        if (d["decile"] == 0).any()
                        else np.nan,
                        "n": int(len(d)),
                    }
                ),
                include_groups=False,
            )
            .reset_index()
        )
        months["ls"] = months["d10"] - months["d1"]
        months["date"] = pd.to_datetime(months["date"])
        months = months.set_index("date").sort_index()
        months = months[months["n"] >= 100]
        ls = months["ls"]
        stats = annualized(ls)
        tov = turnover(s, decile_col="decile", n_col="n")
        results[name] = dict(months=months, ls=ls, stats=stats, tov=tov, raw=s)

        print(f"\n=== UMD {name}: long top-decile - short bottom-decile ===")
        print(f"  ann. mean {stats['mean']*100:7.2f}%  vol {stats['vol']*100:5.2f}%  "
              f"Sharpe {stats['sharpe']:5.2f}  t {stats['t']:5.2f}  n={stats['n']}")
        print(f"  decades (Sharpe, t): {decade_stats(ls)}")
        print(f"  per-month turnover (L basket {tov['tov10']:.0%}, S basket {tov['tov1']:.0%}, "
              f"avg {tov['tovmean']:.0%})")
        print(f"  net-of-cost Sharpe: 0bps {stats['sharpe']:.2f} | "
              f"5bps {net_sharpe(ls, tov, 5e-4):.2f} | 10bps {net_sharpe(ls, tov, 1e-3):.2f} | "
              f"25bps {net_sharpe(ls, tov, 25e-4):.2f}")
        print(f"  LS wins / months: {(ls > 0).mean():.0%}")

    # decile monotonicity on the primary signal
    s = results["r12_1"]["raw"]
    bs = []
    for d, grp in s.groupby("date"):
        row = grp.groupby("decile")["f_ret"].mean().sort_index().values
        if len(row) == 10 and np.isfinite(row).all():
            bs.append(np.polyfit(np.arange(10), row, 1)[0])
    slope = float(np.nanmean(bs)) if bs else np.nan
    print(f"\n  decile slope (regression of next-month ret on decile rank, "
          f"avg across months): {slope * 1e4:.1f} bp/decile")

    out = pd.DataFrame({n: results[n]["ls"] for n in results}).dropna()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out.to_parquet(OUT)
    print(f"\nWrote {OUT} ({len(out)} monthly observations)")


def turnover(s, decile_col, n_col):
    """Average per-month basket turnover for the long (d10) and short (d1)
    equal-weight baskets: fraction of the basket replaced month to month."""
    tov10, tov1, tovmean = [], [], []
    s = s.sort_values(["date", "symbol"])
    prev10, prev1 = set(), set()
    for d, grp in s.groupby("date"):
        cur10 = set(grp.loc[grp[decile_col] == 9, "symbol"])
        cur1 = set(grp.loc[grp[decile_col] == 0, "symbol"])
        if prev10 and prev1 and cur10 and cur1:
            tov10.append(len(cur10 - prev10) / len(cur10))
            tov1.append(len(cur1 - prev1) / len(cur1))
            tovmean.append(
                (len(cur10 - prev10) + len(cur1 - prev1)) / (len(cur10) + len(cur1))
            )
        prev10, prev1 = cur10, cur1
    return dict(tov10=float(np.mean(tov10)) if tov10 else np.nan,
                tov1=float(np.mean(tov1)) if tov1 else np.nan,
                tovmean=float(np.mean(tovmean)) if tovmean else np.nan)


def net_sharpe(ls, tov, cost):
    """Cost in decimal per $ traded per side; both baskets rebalance monthly."""
    net_series = ls.values - np.repeat(cost * (tov["tov10"] + tov["tov1"]), len(ls))
    return annualized(pd.Series(net_series, index=ls.index))["sharpe"]


if __name__ == "__main__":
    main()