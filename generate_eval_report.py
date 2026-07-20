r"""
generate_eval_report.py -- self-contained interactive HTML report for one
unified-evaluation run (the artifacts evaluate.py / evaluation.runner.run
writes under storage/reports/eval/<name>_<ts>/).

Reads ONLY the artifacts (results.json, run_meta.json, trades.parquet) --
never recomputes statistics -- and writes a single HTML file with embedded
Plotly.js (no server, no external requests).

Usage
-----
  C:\ProgramData\anaconda3\python.exe evaluate.py --adapter sentiment
  C:\ProgramData\anaconda3\python.exe generate_eval_report.py --latest news_sentiment
  C:\ProgramData\anaconda3\python.exe generate_eval_report.py --run-dir storage/reports/eval/news_sentiment_20260719_120000

Output: <run_dir>/report.html (override with --out)
"""

import argparse
import glob
import json
import os
import sys

import pandas as pd
import plotly.graph_objects as go

EVAL_ROOT = os.path.join("storage", "reports", "eval")

# Categorical identity (dataviz reference palette, FIXED slot order -- never
# cycled, never reassigned when a series is filtered out).
SLOT = ["#2a78d6", "#008300", "#e87ba4", "#eda100"]
# Status/state colors -- reserved for significance tiers, win/loss and
# bull/bear regime state, never for series identity.
COLOR_GOOD = "#0ca30c"
COLOR_WARNING = "#fab219"
COLOR_CRITICAL = "#d03b3b"
COLOR_MUTED = "#898781"
TIER_COLOR = {"significant": COLOR_GOOD, "weak": COLOR_WARNING,
              "noise": COLOR_MUTED}
SURFACE, PAGE = "#fcfcfb", "#f9f9f7"
INK, INK2, GRID, BASE = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7"


def classify_significance(mean_daily_ic, ic_t_stat) -> str:
    """Same skepticism tiers as generate_tv_rating_report.py: |IC|<0.02 or
    |t|<2 -> noise; |IC|<0.05 -> weak; else significant (leak-check band)."""
    if mean_daily_ic is None or ic_t_stat is None:
        return "noise"
    ic, t = abs(mean_daily_ic), abs(ic_t_stat)
    if ic < 0.02 or t < 2:
        return "noise"
    if ic < 0.05:
        return "weak"
    return "significant"


def find_latest(name: str, root: str = EVAL_ROOT):
    dirs = sorted(d for d in glob.glob(os.path.join(root, f"{name}_*"))
                  if os.path.isdir(d))
    return dirs[-1] if dirs else None


def load_run(run_dir: str):
    with open(os.path.join(run_dir, "results.json"), encoding="utf-8") as fh:
        results = json.load(fh)
    with open(os.path.join(run_dir, "run_meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    trades = None
    tp = os.path.join(run_dir, "trades.parquet")
    if os.path.exists(tp):
        trades = pd.read_parquet(tp)
    return results, meta, trades


def _layout(title: str, ytitle: str = "", height: int = 360) -> dict:
    return dict(
        title=dict(text=title, font=dict(color=INK, size=15)),
        height=height, paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family='system-ui, "Segoe UI", sans-serif', color=INK2,
                  size=12),
        margin=dict(l=60, r=30, t=50, b=40),
        xaxis=dict(gridcolor=GRID, linecolor=BASE, zeroline=False),
        yaxis=dict(title=ytitle, gridcolor=GRID, linecolor=BASE,
                   zeroline=True, zerolinecolor=BASE, zerolinewidth=1),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        bargap=0.35)


def _ic_by_horizon(ic: dict) -> "go.Figure | None":
    hs = sorted(int(k) for k in ic)
    if not hs:
        return None
    pooled = [ic[str(h)].get("pooled_ic") for h in hs]
    daily = [ic[str(h)].get("mean_daily_ic") for h in hs]
    fig = go.Figure()
    fig.add_bar(x=[f"{h}d" for h in hs], y=pooled, name="pooled IC",
                marker_color=SLOT[0],
                hovertemplate="pooled IC %{y:.4f}<extra>%{x}</extra>")
    fig.add_bar(x=[f"{h}d" for h in hs], y=daily, name="mean daily IC",
                marker_color=SLOT[1],
                hovertemplate="mean daily IC %{y:.4f}<extra>%{x}</extra>")
    fig.update_layout(**_layout("Spearman IC by horizon (oriented)",
                                "IC"))
    return fig


def _spread_with_ci(ic: dict, tier2: dict) -> "go.Figure | None":
    hs = sorted(int(k) for k in ic)
    if not hs:
        return None
    spread = [ic[str(h)].get("spread_pct") for h in hs]
    lo, hi = [], []
    for h in hs:
        b = tier2.get(str(h), {})
        m, l_, h_ = (b.get("spread_boot_mean_pct"),
                     b.get("spread_ci_lo_pct"), b.get("spread_ci_hi_pct"))
        s = spread[hs.index(h)]
        if None in (m, l_, h_, s):
            lo.append(None)
            hi.append(None)
        else:
            lo.append(s - l_)
            hi.append(h_ - s)
    fig = go.Figure()
    fig.add_bar(x=[f"{h}d" for h in hs], y=spread,
                name="top-bottom quintile spread", marker_color=SLOT[0],
                error_y=dict(type="data",
                             array=[v if v is not None else 0 for v in hi],
                             arrayminus=[v if v is not None else 0
                                         for v in lo],
                             color=INK2),
                hovertemplate="spread %{y:.3f}%<extra>%{x}</extra>")
    fig.update_layout(**_layout(
        "Bucket spread with bootstrap 95% CI (Tier 2)", "excess return %"))
    return fig


def _regimes(tier3: dict) -> "go.Figure | None":
    reg = tier3.get("regimes") or {}
    order = [k for k in ("bull", "bear", "high_vol", "low_vol")
             if isinstance(reg.get(k), dict)]
    if not order:
        return None
    # bull/bear are STATE -> status colors; vol regimes stay muted grays
    color = {"bull": COLOR_GOOD, "bear": COLOR_CRITICAL,
             "high_vol": COLOR_MUTED, "low_vol": BASE}
    fig = go.Figure()
    fig.add_bar(x=order, y=[reg[k].get("mean_daily_ic") for k in order],
                marker_color=[color[k] for k in order], showlegend=False,
                text=[f"n={reg[k].get('n_days')}" for k in order],
                textposition="outside",
                hovertemplate="mean daily IC %{y:.4f}<extra>%{x}</extra>")
    fig.update_layout(**_layout("Regime conditioning, 5d horizon (Tier 3)",
                                "mean daily IC"))
    return fig


def _events_fig(ev: dict) -> "go.Figure | None":
    labels = list(ev.get("labels", {}))
    if not labels:
        return None
    fig = go.Figure()
    for i, label in enumerate(labels[:4]):        # slot cap; rest in table
        d = ev["labels"][label]
        hs = sorted(int(k) for k in d.get("horizons", {}))
        fig.add_bar(x=[f"{h}d" for h in hs],
                    y=[d["horizons"][str(h)].get("edge_pct",
                       d["horizons"][str(h)].get("mean_pct"))
                       for h in hs],
                    name=f"{label} (n={d.get('n_events')})",
                    marker_color=SLOT[i],
                    hovertemplate="%{y:.3f}%<extra>%{x}</extra>")
    fig.update_layout(**_layout("Event edge vs baseline by horizon",
                                "edge %"))
    return fig


def _car_fig(ev: dict) -> "go.Figure | None":
    labels = list(ev.get("labels", {}))
    fig = go.Figure()
    added = False
    for i, label in enumerate(labels[:4]):        # same slots as the bars
        car = ev["labels"][label].get("mean_car_pct") or {}
        if not car:
            continue
        days = sorted(int(k) for k in car)
        fig.add_scatter(x=days, y=[car[str(d)] for d in days], mode="lines",
                        name=label, line=dict(color=SLOT[i], width=2),
                        hovertemplate="day %{x}: %{y:.3f}%<extra></extra>")
        added = True
    if not added:
        return None
    fig.update_layout(**_layout(
        "Mean cumulative abnormal return by relative day", "CAR %"))
    return fig


def _trades_fig(trades: "pd.DataFrame | None") -> "go.Figure | None":
    if trades is None or trades.empty:
        return None
    wins = trades[trades["pnl_pct"] > 0]["pnl_pct"]
    losses = trades[trades["pnl_pct"] <= 0]["pnl_pct"]
    fig = go.Figure()
    fig.add_histogram(x=wins, name="wins", marker_color=COLOR_GOOD,
                      nbinsx=30)
    fig.add_histogram(x=losses, name="losses", marker_color=COLOR_CRITICAL,
                      nbinsx=30)
    fig.update_layout(barmode="overlay", **_layout(
        "Realized trade P&L distribution", "trades"))
    fig.update_traces(opacity=0.85)
    return fig


def _tile(label: str, value: str, sub: str = "") -> str:
    return (f'<div style="background:{SURFACE};border:1px solid {GRID};'
            'border-radius:8px;padding:14px 18px;min-width:150px">'
            f'<div style="color:{INK2};font-size:12px">{label}</div>'
            f'<div style="color:{INK};font-size:24px;font-weight:600">'
            f'{value}</div>'
            f'<div style="color:{INK2};font-size:11px">{sub}</div></div>')


def _fmt(v, nd=3):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _signal_tiles(results: dict, meta: dict) -> str:
    tiles = []
    ic = results.get("ic", {})
    if ic:
        best_h = max(ic, key=lambda k: abs(ic[k].get("mean_daily_ic") or 0))
        d = ic[best_h]
        tier = classify_significance(d.get("mean_daily_ic"),
                                     d.get("ic_t_stat"))
        tiles.append(_tile(f"daily IC ({best_h}d, best)",
                           _fmt(d.get("mean_daily_ic"), 4),
                           f"t={_fmt(d.get('ic_t_stat'), 2)} - "
                           f"verdict: {tier}"))
    port = results.get("portfolio") or {}
    boot = port.get("sharpe_bootstrap") or {}
    if boot.get("sharpe") is not None:
        tiles.append(_tile("portfolio Sharpe", _fmt(boot["sharpe"], 2),
                           f"95% CI [{_fmt(boot.get('sharpe_ci_lo'), 2)}, "
                           f"{_fmt(boot.get('sharpe_ci_hi'), 2)}]"))
    dsr = (results.get("tier3") or {}).get("deflated_sharpe") or {}
    if dsr.get("dsr_prob") is not None:
        tiles.append(_tile("deflated Sharpe prob", _fmt(dsr["dsr_prob"], 2),
                           f"{dsr.get('n_trials')} registry trials"))
    tiles.append(_tile("universe", str(len(meta.get("universe", []))),
                       f"{len(meta.get('dropped', {}))} dropped"))
    return ('<div style="display:flex;gap:12px;flex-wrap:wrap;'
            'margin:10px 0 18px">' + "".join(tiles) + "</div>")


def _fdr_table(results: dict) -> str:
    fdr = results.get("fdr") or []
    if not fdr:
        return ""
    rows = "".join(
        f'<tr><td>{r.get("evaluation")}</td><td>{r.get("horizon")}</td>'
        f'<td>{r.get("statistic")}</td><td>{_fmt(r.get("p"), 4)}</td>'
        f'<td>{_fmt(r.get("p_adj"), 4)}</td>'
        f'<td>{"yes" if r.get("reject") else "no"}</td></tr>'
        for r in fdr)
    return ('<h3 style="color:' + INK + '">Benjamini-Hochberg FDR '
            '(all p-values this run)</h3>'
            f'<table style="border-collapse:collapse;color:{INK2};'
            'font-size:12px"><tr><th>evaluation</th><th>horizon</th>'
            '<th>statistic</th><th>p</th><th>p_adj</th>'
            '<th>reject@10%</th></tr>' + rows + "</table>")


def _baseline_table(baselines) -> str:
    """Registry baselines for this input (read, never recomputed)."""
    if baselines is None or getattr(baselines, "empty", True):
        return ""
    keep = baselines[baselines["statistic"].isin(
        ["pooled_ic", "mean_daily_ic", "spread_pct", "sharpe"])]
    if keep.empty:
        return ""
    keep = keep.sort_values(["evaluation", "horizon", "statistic"])
    rows = "".join(
        f'<tr><td>{r.evaluation}</td><td>{r.horizon}</td>'
        f'<td>{r.statistic}</td><td>{_fmt(r.value, 4)}</td>'
        f'<td>{str(r.created_at)[:19]}</td></tr>'
        for r in keep.itertuples())
    return ('<h3 style="color:' + INK + '">Registry baselines '
            '(latest per statistic)</h3>'
            f'<table style="border-collapse:collapse;color:{INK2};'
            'font-size:12px"><tr><th>evaluation</th><th>horizon</th>'
            '<th>statistic</th><th>value</th><th>recorded</th></tr>'
            + rows + "</table>")


def build_html(results: dict, meta: dict, trades, baselines=None) -> str:
    figs = []
    if meta.get("input_type") == "signal":
        figs = [_ic_by_horizon(results.get("ic", {})),
                _spread_with_ci(results.get("ic", {}),
                                results.get("tier2", {})),
                _regimes(results.get("tier3", {}))]
    elif meta.get("input_type") == "event_set":
        figs = [_events_fig(results.get("events", {})),
                _car_fig(results.get("events", {}))]
    elif meta.get("input_type") == "trade_rule":
        figs = [_trades_fig(trades)]
    figs = [f for f in figs if f is not None]

    parts = [f'<body style="background:{PAGE};font-family:system-ui,'
             '\'Segoe UI\',sans-serif;margin:24px">',
             f'<h2 style="color:{INK}">Evaluation report: '
             f'{meta.get("input_name")}</h2>',
             f'<div style="color:{INK2};font-size:12px">'
             f'{meta.get("input_type")} - run {meta.get("run_id")} - '
             f'{meta.get("date_range")} - commit {meta.get("git_commit")} - '
             f'{meta.get("created_at")}</div>',
             _signal_tiles(results, meta)
             if meta.get("input_type") == "signal" else ""]
    for i, fig in enumerate(figs):
        parts.append(fig.to_html(full_html=False,
                                 include_plotlyjs=(i == 0)))
    if meta.get("input_type") == "trade_rule":
        s = results.get("summary", {})
        p = results.get("permutation", {})
        parts.append(_tile("trades", str(s.get("n_trades", 0)),
                           f"win rate {_fmt(s.get('win_rate_pct'), 1)}% - "
                           f"P&L ${_fmt(s.get('total_pnl_dollars'), 0)} - "
                           f"perm p={_fmt(p.get('pnl_p'), 3)}"))
    parts.append(_fdr_table(results))
    parts.append(_baseline_table(baselines))
    if meta.get("dropped"):
        drops = "; ".join(f"{k}: {v}" for k, v in
                          list(meta["dropped"].items())[:20])
        parts.append(f'<p style="color:{INK2};font-size:11px">Dropped '
                     f'symbols: {drops}</p>')
    parts.append("</body>")
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>eval: {meta.get('input_name')}</title></head>"
            + "".join(parts) + "</html>")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Unified evaluation framework -- report stage")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-dir", help="one run's artifact directory")
    g.add_argument("--latest",
                   help="newest run dir for this input name under "
                        "storage/reports/eval/")
    ap.add_argument("--out", default=None)
    ap.add_argument("--registry-path", default=None)
    args = ap.parse_args(argv)

    run_dir = args.run_dir or find_latest(args.latest)
    if not run_dir or not os.path.isdir(run_dir):
        print(f"X run directory not found: {run_dir or args.latest}")
        return 1
    if not os.path.exists(os.path.join(run_dir, "results.json")):
        print(f"X {run_dir} has no results.json -- not a run directory")
        return 1

    results, meta, trades = load_run(run_dir)
    baselines = None
    try:
        from evaluation import registry as ev_registry
        baselines = ev_registry.baselines(
            input_name=meta.get("input_name"),
            path=args.registry_path or ev_registry.REG_PATH)
    except Exception:
        baselines = None                # report still renders without it
    out = args.out or os.path.join(run_dir, "report.html")
    html = build_html(results, meta, trades, baselines=baselines)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"+ report written: {out} ({len(html) // 1024} KB, "
          f"from {run_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
