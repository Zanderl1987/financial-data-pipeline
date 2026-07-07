"""
Exposure map: empirical sensitivity (beta) of each symbol to market drivers.

Answers "how exposed is company X to oil / rates / the dollar / VIX?" with
measured history instead of guesses, using ~25 years of daily futures data
(futures table) and the CBOE volatility indices (cboe_volatility table).

For each (symbol, driver) pair, daily stock returns are regressed on daily
driver returns two ways:
  beta        - univariate OLS slope (raw co-movement)
  beta_ex_mkt - driver coefficient from a joint regression on the market
                (ES=F) AND the driver. This is the exposure that matters:
                "does oil move this stock beyond its ordinary market beta?"

A |t| > ~3 on beta_ex_mkt over years of daily data is a real, persistent
exposure; |t| < 2 means the driver adds nothing beyond market beta.

Usage
-----
  python -m analytics.exposure --symbols XOM CVX AAPL          # chosen names
  python -m analytics.exposure --symbols XOM --drivers oil gold vix
  python -m analytics.exposure --symbols XOM --start 2015-01-01

Library:
  from analytics import exposure
  exposure.exposure_map(["XOM", "AAPL"], drivers=["oil", "t10y", "vix"])
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q
from event_backtest import load_close_matrix

# friendly driver name -> (table, symbol). Futures have daily data back to
# ~2000; cboe_volatility indices back to 2021.
DRIVERS: "dict[str, tuple[str, str]]" = {
    # energy
    "oil":         ("futures", "CL=F"),
    "natgas":      ("futures", "NG=F"),
    "gasoline":    ("futures", "RB=F"),
    "heating_oil": ("futures", "HO=F"),
    # metals
    "gold":        ("futures", "GC=F"),
    "silver":      ("futures", "SI=F"),
    "copper":      ("futures", "HG=F"),
    "platinum":    ("futures", "PL=F"),
    "palladium":   ("futures", "PA=F"),
    # agriculture
    "corn":        ("futures", "ZC=F"),
    "wheat":       ("futures", "ZW=F"),
    "soybeans":    ("futures", "ZS=F"),
    "sugar":       ("futures", "SB=F"),
    "coffee":      ("futures", "KC=F"),
    "cotton":      ("futures", "CT=F"),
    # rates (note/bond futures PRICES: positive beta = benefits when yields FALL)
    "t2y":         ("futures", "ZT=F"),
    "t5y":         ("futures", "ZF=F"),
    "t10y":        ("futures", "ZN=F"),
    "t30y":        ("futures", "ZB=F"),
    # fx (quoted vs USD: positive beta = benefits from a WEAKER dollar)
    "eur":         ("futures", "6E=F"),
    "jpy":         ("futures", "6J=F"),
    "gbp":         ("futures", "6B=F"),
    "cad":         ("futures", "6C=F"),
    "aud":         ("futures", "6A=F"),
    # equity indices
    "spx":         ("futures", "ES=F"),
    "nasdaq":      ("futures", "NQ=F"),
    "russell":     ("futures", "RTY=F"),
    # volatility (2021+ only)
    "vix":         ("cboe_volatility", "VIX"),
}

DEFAULT_DRIVERS = ("oil", "natgas", "gold", "copper", "t10y", "eur", "vix")
MARKET_DRIVER = "spx"          # control used for beta_ex_mkt
MIN_OBS = 120                  # ~6 months of daily overlap required


def load_driver_returns(drivers=DEFAULT_DRIVERS,
                        start: "str | None" = None,
                        end: "str | None" = None) -> pd.DataFrame:
    """
    Wide daily-return matrix (date x driver name) for the requested drivers.
    Unknown driver names raise; drivers with no data are silently omitted.
    """
    unknown = [d for d in drivers if d not in DRIVERS]
    if unknown:
        raise KeyError(f"Unknown driver(s) {unknown}; valid: {sorted(DRIVERS)}")

    out = {}
    fut_syms = {name: sym for name, (tbl, sym) in
                ((d, DRIVERS[d]) for d in drivers) if tbl == "futures"}
    if fut_syms:
        fut = q.load("futures", symbol=list(fut_syms.values()), start=start, end=end)
        if not fut.empty:
            fut = fut.copy()
            fut["date"] = pd.to_datetime(fut["date"])
            if fut["date"].dt.tz is not None:
                fut["date"] = fut["date"].dt.tz_localize(None)
            fut["date"] = fut["date"].dt.normalize()
            wide = fut.pivot_table(index="date", columns="symbol", values="close")
            for name, sym in fut_syms.items():
                if sym in wide.columns:
                    out[name] = wide[sym].dropna()

    vol_names = [d for d in drivers if DRIVERS[d][0] == "cboe_volatility"]
    if vol_names:
        vol = q.load("cboe_volatility", start=start, end=end)
        if not vol.empty:
            vol = vol.copy()
            vol["date"] = pd.to_datetime(vol["date"])
            for name in vol_names:
                s = (vol[vol["index_name"] == DRIVERS[name][1]]
                     .set_index("date")["close"].sort_index().dropna())
                if not s.empty:
                    out[name] = s

    if not out:
        return pd.DataFrame()
    levels = pd.DataFrame(out).sort_index()
    # pct_change is meaningless across non-positive prices (WTI went negative
    # in Apr 2020, producing -306% "returns" that wreck OLS betas)
    levels = levels.where(levels > 0)
    return levels.pct_change()


def compute_exposure(stock_ret: pd.Series,
                     driver_ret: pd.Series,
                     market_ret: "pd.Series | None" = None,
                     min_obs: int = MIN_OBS) -> "dict | None":
    """
    OLS exposure of one return series to one driver return series.

    Returns dict with n, corr, beta, t_stat, r2 and (when market_ret given)
    beta_ex_mkt, t_ex_mkt from the joint [market, driver] regression.
    None when overlap < min_obs or the driver has no variance.
    """
    df = pd.DataFrame({"y": stock_ret, "x": driver_ret})
    if market_ret is not None:
        df["m"] = market_ret
    df = df.dropna()
    if len(df) < min_obs or df["x"].std() == 0:
        return None

    y = df["y"].to_numpy()
    res = {"n": len(df),
           "corr": round(float(df["y"].corr(df["x"], method="spearman")), 3)}

    def _ols(X):
        """coefs, t-stats, r2 for y ~ X (X includes intercept column)."""
        coef, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ coef
        dof = len(y) - X.shape[1]
        sigma2 = float(resid @ resid) / dof
        cov = sigma2 * np.linalg.inv(X.T @ X)
        t = coef / np.sqrt(np.diag(cov))
        ss_tot = float(((y - y.mean()) ** 2).sum())
        r2 = 1.0 - float(resid @ resid) / ss_tot if ss_tot > 0 else 0.0
        return coef, t, r2

    X1 = np.column_stack([np.ones(len(df)), df["x"].to_numpy()])
    coef, t, r2 = _ols(X1)
    res["beta"] = round(float(coef[1]), 3)
    res["t_stat"] = round(float(t[1]), 2)
    res["r2"] = round(r2, 4)

    if market_ret is not None and df["m"].std() > 0:
        X2 = np.column_stack([np.ones(len(df)), df["m"].to_numpy(),
                              df["x"].to_numpy()])
        coef2, t2, _ = _ols(X2)
        res["beta_ex_mkt"] = round(float(coef2[2]), 3)
        res["t_ex_mkt"] = round(float(t2[2]), 2)
    return res


def exposure_map(symbols,
                 drivers=DEFAULT_DRIVERS,
                 start: "str | None" = None,
                 end: "str | None" = None,
                 market: str = MARKET_DRIVER,
                 min_obs: int = MIN_OBS) -> pd.DataFrame:
    """
    Tidy exposure table: one row per (symbol, driver).

    Columns: symbol | driver | n | corr | beta | t_stat | r2 |
             beta_ex_mkt | t_ex_mkt
    Pairs with insufficient overlapping history are omitted.
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    want = list(dict.fromkeys(drivers))
    load = want + ([market] if market and market not in want else [])
    drv = load_driver_returns(load, start=start, end=end)
    if drv.empty:
        return pd.DataFrame(columns=["symbol", "driver", "n", "corr", "beta",
                                     "t_stat", "r2", "beta_ex_mkt", "t_ex_mkt"])
    mkt = drv[market] if market in drv.columns else None

    closes = load_close_matrix(symbols, start=start, end=end)
    rows = []
    for sym in symbols:
        if sym not in closes.columns:
            continue
        sret = closes[sym].pct_change()
        for name in want:
            if name not in drv.columns:
                continue
            # regressing a driver on itself as control is meaningless
            m = None if name == market else mkt
            res = compute_exposure(sret, drv[name], market_ret=m, min_obs=min_obs)
            if res is not None:
                rows.append({"symbol": sym, "driver": name, **res})
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="Empirical driver-exposure map")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--drivers", nargs="+", default=list(DEFAULT_DRIVERS),
                        help=f"any of: {' '.join(sorted(DRIVERS))}")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--min-obs", type=int, default=MIN_OBS)
    args = parser.parse_args()

    df = exposure_map(args.symbols, drivers=args.drivers,
                      start=args.start, end=args.end, min_obs=args.min_obs)
    if df.empty:
        print("No exposures computable (insufficient overlapping history).")
        return
    print("\n=== DRIVER EXPOSURE MAP (daily returns"
          + (f", from {args.start}" if args.start else ", full history") + ") ===")
    print(f"{'symbol':<8}{'driver':<13}{'n':>6}{'corr':>7}{'beta':>8}{'t':>7}"
          f"{'r2':>8}{'b_exmkt':>9}{'t_exmkt':>9}")
    for _, r in df.iterrows():
        print(f"{r['symbol']:<8}{r['driver']:<13}{r['n']:>6}{r['corr']:>7}"
              f"{r['beta']:>8}{r['t_stat']:>7}{r['r2']:>8}"
              f"{str(r.get('beta_ex_mkt', '-')):>9}{str(r.get('t_ex_mkt', '-')):>9}")
    print("\nGuide: b_exmkt is the driver beta after controlling for the market;")
    print("|t_exmkt| > 3 over years of data = persistent exposure. Rate futures")
    print("are PRICES (positive beta = wins when yields fall); FX pairs are vs")
    print("USD (positive beta = wins when the dollar weakens).")


if __name__ == "__main__":
    main()
