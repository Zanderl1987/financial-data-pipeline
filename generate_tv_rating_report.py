"""
generate_tv_rating_report.py — self-contained interactive HTML dashboard for
the tv_rating_eval.py backtest results.

Reads ONLY the artifacts tv_rating_eval.py writes (storage/reports/tv_rating_eval/)
-- never recomputes indicators -- and writes a single HTML file with embedded
Plotly.js (no server, no external requests).

Usage
-----
  python tv_rating_eval.py                    # (once) produce the artifacts
  python generate_tv_rating_report.py         # build the report from them

Output: storage/reports/tv_rating_backtest.html
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tv_rating_eval as tve

SIGNALS = tve.SIGNALS
HORIZONS = tve.HORIZONS

# Categorical identity (fixed order, dataviz reference palette slots 1/2/3).
COLOR_SERIES = {"rating_all": "#2a78d6", "rating_ma": "#008300", "rating_osc": "#e87ba4"}
# Status/state colors -- reserved for significance tiers and win/loss/bull-bear
# sign, never used for series identity.
COLOR_GOOD = "#0ca30c"
COLOR_WARNING = "#fab219"
COLOR_CRITICAL = "#d03b3b"
COLOR_MUTED = "#898781"
TIER_COLOR = {"significant": COLOR_GOOD, "weak": COLOR_WARNING, "noise": COLOR_MUTED}


def load_artifacts(out_dir: "str | None" = None):
    out_dir = out_dir or tve.OUT_DIR
    with open(os.path.join(out_dir, "ic_stats.json")) as f:
        ic_stats = json.load(f)
    panel = pd.read_parquet(os.path.join(out_dir, "panel.parquet"))
    transitions = pd.read_parquet(os.path.join(out_dir, "transitions.parquet"))
    trades = pd.read_parquet(os.path.join(out_dir, "trades.parquet"))
    return ic_stats, panel, transitions, trades


def classify_significance(mean_daily_ic, ic_t_stat) -> str:
    """
    Skepticism-default tiers (see signal-eval skill / how-to-read panel):
    |IC| < 0.02 or |t| < 2 -> noise; 0.02 <= |IC| < 0.05 with |t| >= 2 -> weak;
    |IC| >= 0.05 with |t| >= 2 -> significant (report text flags this band as
    worth a leak check, not an automatic celebration).
    """
    if mean_daily_ic is None or ic_t_stat is None:
        return "noise"
    ic, t = abs(mean_daily_ic), abs(ic_t_stat)
    if ic < 0.02 or t < 2:
        return "noise"
    if ic < 0.05:
        return "weak"
    return "significant"


def build_headline_rows(ic_stats: dict) -> "list[dict]":
    rows = []
    for signal, by_h in ic_stats.get("level_ic", {}).items():
        for h_str, r in by_h.items():
            tier = classify_significance(r.get("mean_daily_ic"), r.get("ic_t_stat"))
            rows.append({
                "signal": signal, "horizon": int(h_str), "n": r.get("n"),
                "pooled_ic": r.get("pooled_ic"), "pooled_p": r.get("pooled_p"),
                "mean_daily_ic": r.get("mean_daily_ic"),
                "ic_t_stat": r.get("ic_t_stat"), "ic_days": r.get("ic_days"),
                "spread_pct": r.get("spread_pct"), "spread_t": r.get("spread_t"),
                "tier": tier,
            })
    return sorted(rows, key=lambda r: (r["signal"], r["horizon"]))


def build_symbol_table(panel: pd.DataFrame, signal: str = "rating_all",
                       horizons=HORIZONS) -> pd.DataFrame:
    """Per-symbol pooled IC at each horizon; reports the best/worst horizon."""
    rows = []
    for sym, grp in panel.groupby("symbol"):
        ics = {}
        for h in horizons:
            col = f"fwd_{h}d"
            if col not in grp.columns:
                continue
            sub = grp.dropna(subset=[col, signal])
            if len(sub) >= 10:
                rho, _ = stats.spearmanr(sub[signal], sub[col])
                if np.isfinite(rho):
                    ics[h] = rho
        if not ics:
            continue
        best_h = max(ics, key=ics.get)
        worst_h = min(ics, key=ics.get)
        rows.append({
            "symbol": sym, "n_signals": len(grp),
            "best_horizon": best_h, "best_ic": round(ics[best_h], 4),
            "worst_horizon": worst_h, "worst_ic": round(ics[worst_h], 4),
        })
    return pd.DataFrame(rows).sort_values("best_ic", ascending=False).reset_index(drop=True)
