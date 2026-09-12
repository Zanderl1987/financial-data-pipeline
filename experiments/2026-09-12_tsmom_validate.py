"""Compare our futures-table TSMOM reconstruction against the official AQR
'Time Series Momentum: Factors, Monthly' dataset. Isolates construction bugs
from genuine sample/universe effects.

Run: C:\\ProgramData\\anaconda3\\python.exe experiments/2026-09-12_tsmom_validate.py
"""
import io
import math
import os
import sys

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments import _tsmom_core as core

AQR_URL = ("https://www.aqr.com/-/media/AQR/Documents/Insights/Data-Sets/"
           "Time-Series-Momentum-Factors-Monthly.xlsx")


def load_aqr() -> pd.DataFrame:
    raw = requests.get(AQR_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60).content
    full = pd.ExcelFile(io.BytesIO(raw)).parse("TSMOM Factors", header=6)
    d = full.copy()
    d.columns = ["date", "ALL", "EQ", "FX", "FI", "CM"] + [f"c{i}" for i in range(full.shape[1] - 6)]
    keep = d["date"].apply(lambda s: str(s).startswith(("19", "20")) and "-" in str(s))
    d = d[keep]
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    for c in ("ALL", "EQ", "FX", "FI", "CM"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d[["date", "ALL", "EQ", "FX", "FI", "CM"]].dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def monthly(pr: pd.Series) -> pd.Series:
    pr = pr.dropna()
    return pr.resample("ME").apply(lambda x: (1.0 + x).prod() - 1.0)


def main() -> None:
    aqr = load_aqr()
    print(f"AQR: {aqr['date'].min().date()} -> {aqr['date'].max().date()}  n={len(aqr)}")
    for col in ("ALL", "EQ", "FX", "FI", "CM"):
        s = aqr[col]
        sh = s.mean() * 12 / (s.std() * math.sqrt(12)) if s.std() else float("nan")
        print(f"  AQR {col:>3}: Sharpe {sh:.2f}")

    close = core.load_close_wide()

    for variant, kwargs, mask_rolls in [
        ("skip31 + roll-mask (headline)", {"skip_days": 31}, True),
        ("no-skip (12m to t0) + mask", {"skip_days": 0}, True),
        ("skip31, no roll-mask", {"skip_days": 31}, False),
    ]:
        pos = core.build_positions(close, **kwargs)
        ret = core.clean_returns(mask_rolls=mask_rolls)
        pr = core.portfolio_returns(pos, close, ret=ret, ret_name="gross")
        strat = pr.dropna(subset=["gross"])
        g = strat["gross"]
        print(f"\n=== ours [{variant}] ===  period {strat.index.min().date()}->{strat.index.max().date()}  "
              f"Sharpe {core.ann_sharpe(g):.2f}")

        my_m = monthly(g).reset_index()
        my_m.columns = ["date", "ours"]
        j = my_m.merge(aqr, on="date", how="inner").dropna(subset=["ours", "ALL"])
        if len(j) > 30:
            print(f"  overlap {len(j)} months {j['date'].min().date()}->{j['date'].max().date()}")
            for acol in ("ALL", "EQ", "FX", "FI", "CM"):
                r = j["ours"].corr(j[acol])
                print(f"  corr ours vs AQR {acol:>3}: {r:.2f}")
            j["y"] = j["date"].dt.year
            for a0, b in [(2000, 2009), (2010, 2019), (2020, 2026)]:
                m = (j["y"] >= a0) & (j["y"] <= b)
                oo, aa = j["ours"][m], j["ALL"][m]
                if len(oo) < 24:
                    continue
                sho = oo.mean() * 12 / (oo.std() * math.sqrt(12)) if oo.std() else float("nan")
                sha = aa.mean() * 12 / (aa.std() * math.sqrt(12)) if aa.std() else float("nan")
                print(f"  {a0}-{b}: ours Sharpe {sho:.2f} vs AQR ALL {sha:.2f}")


if __name__ == "__main__":
    main()