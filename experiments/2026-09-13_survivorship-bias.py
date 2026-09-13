#!/usr/bin/env python3
"""
Survivorship-bias measurement for the in-house equity price panels.

Answers, for any given price panel, the question the Sharadar delisting
reference was built for: of the companies that were actually trading on date
D, how many does the panel contain? That ratio is the survivorship bias.

Data
----
- Separates the SEP universe (Sharadar TICKERS via delisting_reference_pipeline)
  into alive (isdelisted=N) and delisted (isdelisted=Y) companies, each with
  firstpricedate/lastpricedate. NOTE: lastpricedate is the last day the
  company traded; ticker recycling means matching by symbol string alone
  silently splices one company's history onto another's.
- Two in-store panels: yfinance_universe_prices (Russell 3000-ish, survivor
  snapshot) and prices (27,759 symbols, multiple sources, backadjusted).

Outputs (stdout)
----------------
1. Allocation summary (SEP total / delisted / alive).
2. Genuine recovered delisted names by decade: a panel row whose history BOTH
   starts near firstpricedate AND terminates near lastpricedate. Rows whose
   series run to the present are recycled-ticker pollution for a DIFFERENT
   company (e.g. WM = Waste Mgmt not WaMu; WB = Weibo not Wachovia), not the
   delisted name, and are excluded.
3. Point-in-time coverage by year: fraction of the SEP-alive universe at each
   month-end that the panel contains (with history started).
4. Constructive UMD r12-1 bound (if --umd): how much the survivor-panel L/S
   spread could move if the missing delisting-bound short-decile names' returns
   were added, across a range of (fraction-missing, mean-missing-return).

Verdict (measured 2026-09-13): the delisting-inclusive panel is NOT buildable
free -- genuine recovery is essentially zero before 2020 (0 / 0 / 0.03% / 3.95%
by decade). Full delisted-name price history sits behind the paid Sharadar
SEP/DAILY subscription (or CRSP/Norgate-class). This turns the survivorship
caveat from an assumption into a measurement with a documented upper bound.
"""

import os
import sys
import argparse

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q

PANELS = {
    "yfinance_universe_prices": "storage/curated/yfinance_universe_prices/yfinance_universe_prices.parquet",
    "prices": "storage/curated/prices/prices.parquet",
}

# A "genuine" recovered delisted name: its series must start within 370d of the
# SEP firstpricedate AND end within 370d of lastpricedate. The first check kills
# recycled tickers (series starts years after the dead company's start); the
# second kills live tickers that merely share the dead symbol (series runs to
# the present, long past lastpricedate).
_END_TOL = 370  # days


def measure_panel(name, path, sep):
    """Return (range_df, allocated_df, coverage_series) for one price panel."""
    import duckdb

    con = duckdb.connect()
    con.execute(f"create or replace table PP as "
                f"select symbol, min(date::DATE) mn, max(date::DATE) mx, count(*) n "
                f"from parquet_scan('{path}') group by 1")
    ran = con.execute("select * from PP").df()
    con.close()

    alive = sep[sep["isdelisted"] == "N"].copy()
    de = sep[sep["isdelisted"] == "Y"].copy()

    tick = set(ran["symbol"])
    print(f"[{name}] {len(tick):,} symbols | "
          f"{int(alive['ticker'].isin(tick).sum()):,} of {len(alive):,} alive SEP names (string)"
          f", {int(de['ticker'].isin(tick).sum()):,} of {len(de):,} delisted SEP names (string)")

    merged = de.merge(ran, left_on="ticker", right_on="symbol", how="inner")
    merged = merged.copy()
    merged["gap_end"] = (merged["mx"] - merged["lastpricedate"]).dt.days
    merged["gap_start"] = (merged["mn"] - merged["firstpricedate"]).dt.days
    merged["genuine"] = (merged["gap_end"] <= _END_TOL) & (merged["gap_start"] >= -_END_TOL)
    merged["dec"] = merged["lastpricedate"].dt.year // 10 * 10
    de = de.assign(dec=de["lastpricedate"].dt.year // 10 * 10)

    tot = de.groupby("dec").size()
    rec = merged[merged["genuine"]].groupby("dec").size()
    alloc = pd.DataFrame({"delisted": tot, "recovered_genuine": rec}).fillna(0)
    alloc["recovery_pct"] = (alloc["recovered_genuine"] / alloc["delisted"] * 100).round(2)
    print(f"  recovered GENUINE delisted names (series starts ~firstpricedate AND ends ~lastpricedate):")
    print(alloc.to_string())
    print(f"  (the {int((~merged['genuine']).sum())} non-genuine string matches are recycled-ticker "
          f"pollution -- different live companies sharing a dead symbol -- excluded)")

    # point-in-time coverage of the TRUE SEP universe alive at D (isdelisted
    # status today is irrelevant: a company that died in 2003 was trading in
    # 1995 and must count against the 1995 denominator, or coverage is
    # overstated to ~90%)
    mn_by_t = ran.set_index("symbol")["mn"]
    me_dates = pd.date_range("1995-01-31", "2026-08-31", freq="ME")
    rows = []
    for D in me_dates:
        alive_d = sep[(sep["firstpricedate"] <= D) & (sep["lastpricedate"] >= D)]
        if len(alive_d) == 0:
            continue
        cov = (alive_d["ticker"].isin(tick)
               & (mn_by_t.reindex(alive_d["ticker"]).values <= np.datetime64(D)).astype(bool))
        rows.append((D, len(alive_d), int(cov.sum())))
    cov = pd.DataFrame(rows, columns=["date", "alive", "covered"])
    cov["coverage"] = cov["covered"] / cov["alive"]
    cov["yr"] = cov["date"].dt.year
    print(f"  point-in-time coverage of the FULL SEP-alive universe by year "
          f"(panel-containing / SEP-alive; low values = delisted-name hole):")
    by = cov.groupby("yr")[["alive", "covered", "coverage"]].agg(
        alive=("alive", "mean"), covered=("covered", "mean"), coverage=("coverage", "mean"))
    print(by.round(4).to_string())
    return ran, alloc, by


def umd_bound():
    p = "storage/reports/eval/umd_equity_momentum_daily.parquet"
    if not os.path.exists(p):
        print("  (no UMD artifact; rerun experiments/2026-09-12_umd_equity-momentum.py first)")
        return
    umd = pd.read_parquet(p)["r12_1"].dropna()
    base = umd.mean() / umd.std() * np.sqrt(12)
    print(f"\n=== Constructive bound: UMD r12-1 L/S under a delisting-inclusive short leg ===")
    print(f"survivor-panel L/S: Sharpe {base:.2f} (ann.mean {umd.mean()*12*100:.2f}%, "
          f"{len(umd)} months)")
    print(f"Fraction f of the true short-decile universe missing; missing names' next-month")
    print(f"return averages mu_missing (<=0). True L/S = panel L/S - f*mu_missing.")
    for f, mu in [(0.20, -0.02), (0.20, -0.05), (0.35, -0.02), (0.35, -0.05)]:
        added = -f * mu
        m = umd + added
        print(f"  f={f:.0%}, mu={mu*100:4.0f}% -> +{added*100:5.2f} pp/mo -> Sharpe "
              f"{m.mean()/m.std()*np.sqrt(12):6.2f}")


def main():
    ap = argparse.ArgumentParser(description="Survivorship-bias measurement")
    ap.add_argument("--panel", choices=list(PANELS), default="prices",
                    help="which price panel to measure (default: prices)")
    ap.add_argument("--umd", action="store_true", help="also print the constructive "
                    "UMD r12-1 corrected-spread bound")
    ns = ap.parse_args()

    print("=== Survivorship-bias measurement ===")
    sep = q.load("delisting_reference")
    sep = sep[sep["price_table"] == "SEP"].copy()
    sep["firstpricedate"] = pd.to_datetime(sep["firstpricedate"], errors="coerce")
    sep["lastpricedate"] = pd.to_datetime(sep["lastpricedate"], errors="coerce")
    sep = sep[sep["firstpricedate"].notna() & sep["lastpricedate"].notna()]
    print(f"SEP equities: {len(sep):,} | alive {int((sep['isdelisted']=='N').sum()):,} "
          f"| delisted {int((sep['isdelisted']=='Y').sum()):,}")
    print(f"NOTE: firstpricedate is a 1986 floor value for many names; delisting counts "
          f"begin 1998 (2 in 1997, 455 in 1998).")

    measure_panel(ns.panel, PANELS[ns.panel], sep)
    if ns.umd:
        umd_bound()


if __name__ == "__main__":
    main()