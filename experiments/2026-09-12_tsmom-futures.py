"""
First deep backtest from docs/STRATEGY_CATALOG.md Tier 1: Time-Series Momentum
(Moskowitz, Ooi & Pedersen 2012, "Time Series Momentum", JFE 104(2)) on the
in-store futures table (44 continuous front-month contracts, 1997-10+).

MOP-faithful, PIT-safe construction (see experiments/_tsmom_core.py):
- signal at month-end t0: sign of 12-month log return measured ~13 months to
  ~1 month before t0 (skips the most recent month); positions take effect
  t0+1 (shifted 1 day, no lookahead)
- each instrument vol-scaled to a 40% annualized ex-ante target from trailing
  252-day realized vol
- portfolio = equal-weight average of the vol-scaled positions, self-financed
  long/short; headline run EXCISES roll days (our futures store is un-back-
  adjusted front-month; see _tsmom_core.clean_returns)

Reported like the paper: per-instrument stats + portfolio Sharpe gross & net,
then a 10%-vol-normalized equity curve for drawdowns/CAGR (Sharpe invariant).

Run:  C:\\ProgramData\\anaconda3\\python.exe experiments/2026-09-12_tsmom-futures.py
Validate vs official AQR TSMOM factor data:
      C:\\ProgramData\\anaconda3\\python.exe experiments/2026-09-12_tsmom_validate.py

Companion writeup: experiments/2026-09-12_tsmom-futures.md
"""
import math
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments import _tsmom_core as core

ANN = core.ANN


def _tstat(g: pd.Series) -> float:
    sd = g.std(ddof=1)
    return math.sqrt(len(g)) * g.mean() / sd if sd > 0 else float("nan")


def main() -> None:
    close = core.load_close_wide()
    print(f"loaded {close.shape[1]} symbols, {close.index.min().date()} -> {close.index.max().date()}")

    ret_all = core.clean_returns(mask_rolls=False)
    ret_clean = core.clean_returns(mask_rolls=True)
    pos = core.build_positions(close)

    pa = core.portfolio_returns(pos, close, ret=ret_all, ret_name="raw")
    pc = core.portfolio_returns(pos, close, ret=ret_clean, ret_name="clean")

    print("\n=== TSMOM portfolio (44 futures, vol-targeted per instrument) ===")
    g = pc["clean"]
    g0 = g.dropna()
    print(f"active {g0.index.min().date()} -> {g0.index.max().date()}  ({len(pc)} days in index)")
    print(f"{'construction':<34}{'Sharpe':>8}{'t':>8}{'ann.mean':>10}")
    for lbl, fr in (("raw (no roll mask)", pa), ("roll-masked (headline)", pc)):
        g = fr["clean" if lbl.startswith("roll") else "raw"]
        ann = g.mean() * ANN
        print(f"{lbl:<34}{core.ann_sharpe(g):>8.2f}{_tstat(g):>8.2f}{ann*100:>9.2f}%")
    # clip variant for reference
    gclip = pa["raw"].clip(-0.05, 0.05)
    print(f"{'raw + 5% return clip':<34}{core.ann_sharpe(gclip):>8.2f}{_tstat(gclip):>8.2f}{gclip.mean()*ANN*100:>9.2f}%")

    g = pc["clean"]
    print(f"\navg # active instruments: {pc['active'].mean():.1f}")
    print(f"avg monthly turnover      : {pc['turnover'].mean()/21:.2f} capital units")

    print("\n--- net-of-cost Sharpe grid (one-way bps per position change) ---")
    for bps in (1.0, 2.5, 5.0, 10.0):
        print(f"  {bps:>4g} bps: {core.ann_sharpe(pc[f'net_{bps:g}bps']):6.2f}")

    scale10 = 0.10 / core.ann_vol(g) if core.ann_vol(g) > 0 else 1.0
    eq10 = (1.0 + g * scale10).cumprod()
    yrs = (pc.index.max() - pc.index.min()).days / 365.25
    cagr10 = eq10.iloc[-1] ** (1.0 / yrs) - 1.0
    print("\n--- headline at portfolio 10% annualized vol (de-levered for display) ---")
    print(f"scaling factor     : {scale10:.4f}")
    print(f"CAGR               : {cagr10*100:.2f}%")
    print(f"max drawdown       : {core.max_drawdown(eq10)*100:.2f}%")

    print("\n--- calendar years (10%-vol portfolio) ---")
    yr = (1.0 + g).dropna().resample("YE").prod() - 1.0
    print(yr.to_string(float_format=lambda v: f"{v*100:7.2f}%"))
    print("\n--- decade Sharpe (roll-masked gross) ---")
    for (a, b) in [(2000, 2009), (2010, 2019), (2020, 2026)]:
        gg = g[(g.index.year >= a) & (g.index.year <= b)]
        print(f"  {a}-{b}: {core.ann_sharpe(gg):.2f}")

    print("\n--- per-instrument (roll-masked, vol-scaled own returns) ---")
    held = pos.reindex(close.index, method="ffill").shift(1).fillna(0.0)
    rows = []
    for sym in close.columns:
        s = (held[sym] * ret_clean[sym]).dropna()
        if len(s) < 60:
            continue
        rows.append((sym, core.ann_sharpe(s), len(s), int((s > 0).sum()), int((s < 0).sum())))
    tab = pd.DataFrame(rows, columns=["symbol", "sharpe", "days", "up", "down"]).sort_values("sharpe", ascending=False)
    print(tab.to_string(index=False, float_format=lambda v: f"{v:6.2f}"))
    print(f"\ninstruments w/ positive Sharpe: {(tab['sharpe']>0).sum()}/{len(tab)}")

    print("\n--- by asset class (mean member Sharpe) ---")
    cats = core.FUT[["symbol", "category"]].drop_duplicates()
    tab2 = tab.merge(cats, on="symbol")
    print(tab2.groupby("category")["sharpe"].agg(["count", "mean"]).round(2).to_string())

    out = pc.copy()
    out["raw"] = pa["raw"]
    out.to_parquet("storage/reports/eval/tsmom_futures_daily.parquet")
    print("\nwrote storage/reports/eval/tsmom_futures_daily.parquet")


if __name__ == "__main__":
    main()