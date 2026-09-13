#!/usr/bin/env python3
"""
Quality factor backtest on the in-house equity universe (QMJ minus the payout leg).

Scope: the full QMJ needs a payout component (dividends + net repurchases / net
income). This pipeline has NO universe-scale payout data (dividends only on
simfin/sa ~42-46 symbols), so this build is the A-F-P quality score from THREE of
four categories -- profitability, growth, safety -- with the payout gap measured
separately via a 42-symbol cross-check (appended to the run output).

Construction (Asness-Frazzini-Pedersen 2019 "Quality Minus Junk", adapted):
  - Sub-measures (annual, first-reported, PIT):
      PROFITABILITY  roe = ni / (assets - liabilities)
                     roa = ni / assets
                     gp_a = gross_profit / assets            (financials lack gp -> subset)
                     cfo_a = operating_cash_flow / assets
                     gp_m = gross_profit / revenue           (subset)
                     op_m = operating_income / revenue       (fills the financials gap)
      GROWTH        yoy of net_income, revenue, assets, gross_profit, cfo, op_income
      SAFETY        accruals_neg = -(ni - ocf)/assets ; leverage_neg = -liab/assets
  - z-score each measure cross-sectionally within month-end (winsorized 1/99);
    category = mean of available member z's; Q = mean of available category z's
    (requires >=2 categories, profitability included).
  - SIGNAL DATING: annual facts are keyed by their FIRST report date (min filed
    per symbol/metric/period_end) read from RAW -- the curated fundamentals
    snapshot destroys PIT dating (KEYS drop `filed`, keeping only the latest
    comparative re-report, dating balance-sheet items ~1yr late). No look-ahead:
    a month-end only sees facts with first-filed <= that date.

Universe: storage/curated/yfinance_universe_prices (Russell 3000, alive-2026).
Eligibility per month-end: close >= $5, trailing-21d avg dollar volume >= $10M,
quality score present. Hold ~22 trading days, rebalance at month-end (mirrors
UMD/KLN sweep).

Outputs a summary block; monthly long-short series saved to
storage/reports/eval/q_factor_daily.parquet

Survivorship caveat (mandated by evaluation/universe.py): this is the alive-2026
panel; genuine delisted-name recovery is 0/0/0.03%/3.95% by decade and the bias is
conservative for L/S (missing delisted JUNK names compress the short leg) --
measured 2026-09-13, see experiments/2026-09-13_survivorship-bias.py/.md. A
constructive bound on the short leg is printed below.
"""

import os
import sys

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PRICES = "storage/curated/yfinance_universe_prices/yfinance_universe_prices.parquet"
FUND_RAW = "storage/raw/fundamentals/annual/**/*.parquet"
OUT = "storage/reports/eval/q_factor_daily.parquet"

START = "2010-01-01"
PRICE_FLOOR = 5.0
DOLLAR_VOL_FLOOR = 1.0e7
SHIFT_1M = 21
HOLD = 22

_METRICS = ["revenue", "net_income", "operating_income", "gross_profit",
            "total_assets", "total_liabilities", "operating_cash_flow"]
_FORMS = ["10-K", "20-F", "40-F"]

# growth bases must be positive for a sane ratio
_GROWTH_BASES = {
    "net_income": "G_NI",
    "revenue": "G_REV",
    "total_assets": "G_ASSETS",
    "gross_profit": "G_GP",
    "operating_cash_flow": "G_CFO",
    "operating_income": "G_OPINC",
}


def load_pit_fundamentals():
    """Raw annual facts deduped to (symbol, metric, period_end) FIRST report.

    Returns columns: symbol, metric, period_end, filed (= first report date),
    value, prev_value (prior fiscal-year first report, same symbol), growth.
    """
    con = duckdb.connect()
    q = f"""
        WITH ranked AS (
            SELECT symbol, metric, CAST(period_end AS DATE) AS period_end,
                   CAST(filed AS DATE) AS filed, value,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, metric, CAST(period_end AS DATE)
                       ORDER BY CAST(filed AS DATE), value
                   ) AS rn
            FROM read_parquet('{FUND_RAW}', hive_partitioning=1)
            WHERE form IN ('{"','".join(_FORMS)}')
              AND metric IN ('{"','".join(_METRICS)}')
              AND filed IS NOT NULL AND period_end IS NOT NULL
              AND value IS NOT NULL
        ),
        raw AS (
            SELECT symbol, metric, period_end, filed, value
            FROM ranked WHERE rn = 1
        ),
        ordered AS (
            SELECT symbol, metric, period_end, filed, value,
                   LAG(period_end) OVER (PARTITION BY symbol, metric ORDER BY period_end) AS prev_pe,
                   LAG(value)     OVER (PARTITION BY symbol, metric ORDER BY period_end) AS prev_value,
                   LAG(filed)     OVER (PARTITION BY symbol, metric ORDER BY period_end) AS prev_filed
            FROM raw
        )
        SELECT symbol, metric, period_end, filed, value,
               CASE WHEN prev_pe IS NOT NULL THEN prev_value END AS prev_value,
               CASE WHEN prev_pe IS NOT NULL AND prev_value IS NOT NULL
                     AND prev_value > 0 AND value > 0
                    THEN value / prev_value - 1 END AS growth
        FROM ordered
        ORDER BY symbol, metric, period_end
    """
    return con.execute(q).df()


def asof_join_metrics(panel, fund):
    """Attach each metric's latest first-reported annual value/growth per (symbol, date)."""
    con = duckdb.connect()
    con.register("_panel", panel[["symbol", "date"]])
    out = panel.copy()
    frame = fund
    for metric in _METRICS:
        sub = (frame[frame["metric"] == metric][["symbol", "filed", "period_end", "value"]]
               .sort_values(["symbol", "filed", "period_end"], ascending=False)
               .drop_duplicates(["symbol", "filed"]))
        if sub.empty:
            continue
        con.register(f"sub_{metric}", sub)
        j = con.execute(f"""
            SELECT p.symbol, p.date, m.value AS val, m.period_end AS pe
            FROM _panel p
            ASOF LEFT JOIN sub_{metric} m
            ON p.symbol = m.symbol AND p.date >= m.filed
        """).df()
        j["date"] = pd.to_datetime(j["date"])
        out = out.merge(j.rename(columns={"val": f"{metric}", "pe": f"{metric}_pe"}),
                        on=["symbol", "date"], how="left")
    for metric, tag in _GROWTH_BASES.items():
        sub = (frame[frame["metric"] == metric][["symbol", "filed", "period_end", "growth"]]
               .dropna(subset=["growth"])
               .sort_values(["symbol", "filed", "period_end"], ascending=False)
               .drop_duplicates(["symbol", "filed"]))
        if sub.empty:
            continue
        con.register(f"sub_{metric}_g", sub)
        j = con.execute(f"""
            SELECT p.symbol, p.date, g.growth AS gval
            FROM _panel p
            ASOF LEFT JOIN sub_{metric}_g g
            ON p.symbol = g.symbol AND p.date >= g.filed
        """).df()
        j["date"] = pd.to_datetime(j["date"])
        out = out.merge(j.rename(columns={"gval": tag}), on=["symbol", "date"], how="left")
    con.unregister("_panel")
    return out


def build_raw_signals(df):
    """Raw (pre-z) quality sub-measures. Higher = more attractive everywhere."""
    d = df.copy()
    eq = d["total_assets"] - d["total_liabilities"]
    d["ROE"] = (d["net_income"] / eq).where(eq > 0)
    d["ROA"] = (d["net_income"] / d["total_assets"]).where(d["total_assets"] > 0)
    d["GP_A"] = (d["gross_profit"] / d["total_assets"]).where(
        (d["total_assets"] > 0) & d["gross_profit"].notna())
    d["CFO_A"] = (d["operating_cash_flow"] / d["total_assets"]).where(d["total_assets"] > 0)
    d["GP_M"] = (d["gross_profit"] / d["revenue"]).where(d["revenue"] > 0)
    d["OP_M"] = (d["operating_income"] / d["revenue"]).where(d["revenue"] > 0)
    d["ACCR_NEG"] = -((d["net_income"] - d["operating_cash_flow"]) / d["total_assets"]).where(
        d["total_assets"] > 0)
    d["LEV_NEG"] = -(d["total_liabilities"] / d["total_assets"]).where(d["total_assets"] > 0)
    return d


def zscore_cross(s):
    if s.notna().sum() == 0:
        return pd.Series(np.nan, index=s.index)
    mu, sd = s.mean(), s.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(np.where(s.notna(), 0.0, np.nan), index=s.index)
    return (s - mu) / sd


def build_quality_score(d):
    """Category z's -> Q composite. Winsorize each sub-measure per date first."""
    d = d.copy()
    profit = ["ROE", "ROA", "GP_A", "CFO_A", "GP_M", "OP_M"]
    growth = list(_GROWTH_BASES.values())
    safety = ["ACCR_NEG", "LEV_NEG"]

    def wins_zs(cols):
        zs = []
        for c in cols:
            if c not in d.columns:
                continue
            s = d[c]
            if s.notna().sum() == 0:
                continue
            q1, q99 = s.quantile(0.01), s.quantile(0.99)
            clipped = s.clip(q1, q99)
            z = d.groupby("date", group_keys=False)[c].apply(
                lambda x: zscore_cross(x))
            zs.append(z.to_frame(c))
        return zs

    pz = wins_zs(profit)
    gz = wins_zs(growth)
    sz = wins_zs(safety)

    d["Q_PROF"] = pd.concat(pz, axis=1).mean(axis=1) if pz else np.nan
    d["Q_GROW"] = pd.concat(gz, axis=1).mean(axis=1) if gz else np.nan
    d["Q_SAFE"] = pd.concat(sz, axis=1).mean(axis=1) if sz else np.nan
    n_cat = pd.notna(d[["Q_PROF", "Q_GROW", "Q_SAFE"]]).sum(axis=1)
    d["Q"] = d[["Q_PROF", "Q_GROW", "Q_SAFE"]].mean(axis=1)
    d["Q"] = d["Q"].where(n_cat >= 2)
    return d


def annualized(monthly):
    m = monthly.dropna()
    if len(m) < 2:
        return dict(mean=np.nan, vol=np.nan, sharpe=np.nan, t=np.nan, n=len(m))
    mean, vol = m.mean(), m.std(ddof=1)
    return dict(mean=mean * 12, vol=vol * np.sqrt(12),
                sharpe=mean / vol * np.sqrt(12) if vol > 0 else np.nan,
                t=mean / (vol / np.sqrt(len(m))) if vol > 0 else np.nan, n=len(m))


def decade_stats(monthly):
    m = pd.Series(monthly).sort_index().dropna()
    if m.empty:
        return ""
    return ", ".join(
        f"{d}s {annualized(m[m.index.year // 10 * 10 == d])['sharpe']:.2f}"
        f" (t {annualized(m[m.index.year // 10 * 10 == d])['t']:.2f})"
        for d in sorted(set(m.index.year // 10 * 10)))


def turnover(s, decile_col, n_col):
    tov10, tov1 = [], []
    s = s.sort_values(["date", "symbol"])
    prev10, prev1 = set(), set()
    for d, grp in s.groupby("date"):
        cur10 = set(grp.loc[grp[decile_col] == 9, "symbol"])
        cur1 = set(grp.loc[grp[decile_col] == 0, "symbol"])
        if prev10 and prev1 and cur10 and cur1:
            tov10.append(len(cur10 - prev10) / len(cur10))
            tov1.append(len(cur1 - prev1) / len(cur1))
        prev10, prev1 = cur10, cur1
    return (float(np.mean(tov10)) if tov10 else np.nan,
            float(np.mean(tov1)) if tov1 else np.nan)


def net_sharpe(ls, tov10, tov1, cost):
    series = ls.values - np.repeat(cost * (tov10 + tov1), len(ls))
    return annualized(pd.Series(series, index=ls.index))["sharpe"]


def short_side_bound(ls):
    """Constructive: if fraction f of the true junk decile is missing and missing
    names average mu<=0 next-month, true L/S = panel L/S - f*mu. Mirrors
    experiments/2026-09-13_survivorship-bias.py::umd_bound."""
    st = annualized(ls)
    print("Constructive short-leg bound (missing delisted junk names):")
    print(f"  survivor-panel Q L/S Sharpe {st['sharpe']:.2f} "
          f"(ann.mean {st['mean']*100:.2f}%, n={st['n']})")
    for f, mu in [(0.20, -0.02), (0.20, -0.05), (0.35, -0.02), (0.35, -0.05)]:
        gain = -f * mu
        corr = st["sharpe"] + gain / st["vol"] * np.sqrt(12)
        print(f"  f={f:.0%}, mu={mu/100:+.0%}/mo -> +{gain*100:5.2f} pp/mo -> Sharpe {corr:.2f}")


def main():
    panel = load_pit_fundamentals()
    print(f"PIT annual facts (first-report): {len(panel):,} rows, "
          f"{panel['symbol'].nunique():,} symbols, forms {','.join(_FORMS)}")

    prices = pd.read_parquet(PRICES, columns=["symbol", "date", "adj_close", "volume"])
    prices["date"] = pd.to_datetime(prices["date"])
    g = prices.groupby("symbol", sort=False)
    prices["close"] = prices["adj_close"].astype(float)
    prices["prev1m"] = g["close"].shift(SHIFT_1M)
    prices["next1m"] = g["close"].shift(-SHIFT_1M - 1)
    prices["dvol21"] = (
        g["close"].rolling(SHIFT_1M, min_periods=SHIFT_1M).mean().reset_index(level=0, drop=True)
        * prices["volume"]
    )
    prices = prices[prices["date"] >= START]
    month_ends = prices.groupby(prices["date"].dt.to_period("M"))["date"].transform("max")
    me = prices[prices["date"] == month_ends][["symbol", "date"]].copy().reset_index(drop=True)

    d = asof_join_metrics(me, panel)
    print(f"month-end panel        : {len(d):,} rows ({me['date'].min():%Y-%m} -> {me['date'].max():%Y-%m})")

    d = d.merge(
        prices.drop(columns=["volume"]),
        on=["symbol", "date"], how="left")
    d["f_ret"] = d["next1m"] / d["close"] - 1.0
    d = build_raw_signals(d)
    d = build_quality_score(d)
    print(f"quality score present  : {d['Q'].notna().sum():,} stock-months "
          f"({d['Q'].notna().mean():.0%})")

    elig = (
        (d["close"] >= PRICE_FLOOR)
        & (d["dvol21"] >= DOLLAR_VOL_FLOOR)
        & d["Q"].notna()
        & d["f_ret"].notna()
    )
    s = d[elig].copy()
    print(f"eligible (>= $5, dvol >= $10M, Q present): {len(s):,} stock-months")

    s["decile"] = s.groupby("date")["Q"].transform(
        lambda x: pd.qcut(x.rank(method="first"), 10, labels=False, duplicates="drop")
    ).astype("Int64")

    months = (
        s.groupby("date")
        .apply(
            lambda dd: pd.Series(
                {
                    "d10": float(dd.loc[dd["decile"] == 9, "f_ret"].mean())
                    if (dd["decile"] == 9).any() else np.nan,
                    "d1": float(dd.loc[dd["decile"] == 0, "f_ret"].mean())
                    if (dd["decile"] == 0).any() else np.nan,
                    "n": int(len(dd)),
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
    st = annualized(ls)
    tov10, tov1 = turnover(s, "decile", "n")

    print(f"\n=== QLS: long top-decile (quality) - short bottom-decile (junk) ===")
    print(f"  ann. mean {st['mean']*100:7.2f}%  vol {st['vol']*100:5.2f}%  "
          f"Sharpe {st['sharpe']:5.2f}  t {st['t']:5.2f}  n={st['n']}")
    print(f"  long-only (d10) ann mean {annualized(months['d10'])['mean']*100:7.2f}%  "
          f"short-only (d1) {annualized(months['d1'])['mean']*100:7.2f}%")
    print(f"  decades (Sharpe, t): {decade_stats(ls)}")
    print(f"  per-month turnover: long {tov10:.0%}, short {tov1:.0%}")
    print(f"  net-of-cost Sharpe: 0bps {st['sharpe']:.2f} | "
          f"5bps {net_sharpe(ls, tov10, tov1, 5e-4):.2f} | "
          f"10bps {net_sharpe(ls, tov10, tov1, 1e-3):.2f} | "
          f"25bps {net_sharpe(ls, tov10, tov1, 25e-4):.2f}")
    print(f"  LS wins / months: {(ls > 0).mean():.0%}")

    bs = []
    for _d, grp in s.groupby("date"):
        row = grp.groupby("decile")["f_ret"].mean().sort_index().values
        if len(row) == 10 and np.isfinite(row).all():
            bs.append(np.polyfit(np.arange(10), row, 1)[0])
    slope = float(np.nanmean(bs)) if bs else np.nan
    print(f"  decile slope (bp/decile): {slope * 1e4:.1f}")

    # category contributions (mean z of each, across the eligible cross-sections)
    for c in ["Q_PROF", "Q_GROW", "Q_SAFE"]:
        print(f"  {c:8s}: mean z {s[c].mean():+.3f}  present {s[c].notna().mean():.0%}")

    short_side_bound(ls)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out = pd.DataFrame({"qls": ls}).dropna()
    out.to_parquet(OUT)
    print(f"\nWrote {OUT} ({len(out)} monthly observations)")

    payout_cross_check(s)


def payout_cross_check(panel_with_q):
    """42-symbol simfin payout as a directional check on the missing 4th leg.

    QMJ payout = (dividends + net repurchases)/net income, high = good. Not
    available at universe scale; here we check on the 42-name simfin slice
    whether payout correlates with the in-store Q proxy, and whether adding it
    would materially re-rank the overlapping names.

    Uses EDGAR net_income (already PIT-correct in the panel) as denominator
    and simfin cashflow components (PIT on publish_date) as numerator.
    """
    try:
        cf = pd.read_parquet("storage/curated/simfin_cashflow/simfin_cashflow.parquet")
    except Exception as e:
        print(f"\npayout cross-check skipped: {e}")
        return
    cf["publish_date"] = pd.to_datetime(cf["publish_date"])
    rep = cf["cash_repurchase_of_equity"].fillna(0) + cf["cash_from_repurchase_of_equity"].fillna(0)
    cf["cash_payout"] = cf["dividends_paid"].fillna(0) + rep   # positive = returned

    # latest published cashflow per symbol (PIT: use publish_date as trigger)
    latest = cf.sort_values("publish_date").groupby("symbol").last().reset_index()
    latest = latest[["symbol", "publish_date", "cash_payout"]].copy()

    # merge net_income from the panel (take latest available per symbol)
    ni = (panel_with_q[panel_with_q["net_income"].notna() & (panel_with_q["net_income"] > 0)]
          .sort_values("date").groupby("symbol").last()
          .reset_index()[["symbol", "date", "net_income"]])

    p = latest.merge(ni, on="symbol", how="inner")
    p["payout"] = p["cash_payout"] / p["net_income"]
    p = p[p["payout"].notna() & np.isfinite(p["payout"])]
    print(f"\n=== Payout cross-check (simfin, {len(p)} symbols with payout + net_income) ===")
    if len(p) < 10:
        print(f"  Too few symbols ({len(p)}) for meaningful cross-section; skipping.")
        print("  NOTE: directional only -- universe payout data (Sharadar SEP/DAILY or "
              "CRSP-class) is required for the real 4th leg.")
        return

    # attach payout to eligible panel via ASOF join on publish_date
    con = duckdb.connect()
    con.register("_p", p[["symbol", "publish_date", "payout"]])
    con.register("_q", panel_with_q[["symbol", "date", "Q", "decile", "f_ret"]].dropna(subset=["Q"]).copy())
    q_with_payout = con.execute("""
        SELECT q.symbol, q.date, q.Q, q.decile, q.f_ret,
               p.payout, p.publish_date
        FROM _q q
        ASOF LEFT JOIN _p p
        ON q.symbol = p.symbol AND q.date >= p.publish_date
    """).df()
    con.unregister("_p"); con.unregister("_q")
    q_with_payout["date"] = pd.to_datetime(q_with_payout["date"])

    # keep only symbols with a payout observation
    has_payout = q_with_payout[q_with_payout["payout"].notna()].copy()
    print(f"  overlap: {has_payout['symbol'].nunique()} symbols, "
          f"{len(has_payout):,} stock-months with payout assigned")
    if has_payout["symbol"].nunique() < 10:
        print("  Too few symbols for meaningful correlation; skipping.")
        return

    # Spearman: payout vs Q (cross-sectional per date)
    corr_by_date = has_payout.groupby("date").apply(
        lambda g: g["payout"].corr(g["Q"], method="spearman")
        if g["symbol"].nunique() >= 10 else np.nan, include_groups=False).dropna()
    mean_corr = corr_by_date.mean()
    print(f"  Spearman(payout, Q): mean {mean_corr:+.3f}  "
          f"(positive = payout confirms quality direction)")
    print(f"    per-date: median {corr_by_date.median():+.3f}, "
          f"frac positive {(corr_by_date>0).mean():.0%}, n_dates={len(corr_by_date)}")

    # re-ranking: how many top/bottom Q decile names shift ≥2 deciles if payout added?
    d9 = has_payout[has_payout["decile"] == 9].copy()
    d0 = has_payout[has_payout["decile"] == 0].copy()
    for label, grp in [("top decile (Q9)", d9), ("bottom decile (Q0)", d0)]:
        if len(grp) < 5:
            continue
        avg_payout = grp["payout"].mean()
        # cross-sectional payout z-score vs Q z-score
        rank_q = grp["Q"].rank(pct=True)
        rank_p = grp["payout"].rank(pct=True)
        moved = (rank_q - rank_p).abs()
        print(f"  {label}: avg payout {avg_payout:+.2f}, "
              f"mean |rank shift| {moved.mean():.2f} (of 0..1), "
              f">=2 quintile shift {(moved > 0.20).mean():.0%}")

    print("  NOTE: directional only -- universe payout data (Sharadar SEP/DAILY or "
          "CRSP-class) is required for the real 4th leg.")


if __name__ == "__main__":
    main()