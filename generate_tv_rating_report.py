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

import argparse
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
    if not rows:
        return pd.DataFrame(columns=["symbol", "n_signals", "best_horizon",
                                     "best_ic", "worst_horizon", "worst_ic"])
    return pd.DataFrame(rows).sort_values("best_ic", ascending=False).reset_index(drop=True)


def build_ic_bar_chart(ic_stats: dict) -> go.Figure:
    fig = go.Figure()
    for signal, color in COLOR_SERIES.items():
        by_h = ic_stats.get("level_ic", {}).get(signal, {})
        hs = sorted((int(h) for h in by_h), key=int)
        y = [by_h[str(h)].get("mean_daily_ic") for h in hs]
        err = [by_h[str(h)].get("ic_se") or 0 for h in hs]
        fig.add_trace(go.Bar(name=signal, x=[f"{h}d" for h in hs], y=y,
                             error_y=dict(type="data", array=err, visible=True),
                             marker_color=color))
    fig.update_layout(barmode="group", title="Mean daily cross-sectional IC by horizon",
                      yaxis_title="Mean daily IC", template="plotly_white",
                      legend_title_text="Signal")
    return fig


def build_spread_chart(ic_stats: dict) -> go.Figure:
    """
    Small multiples (one subplot per signal) rather than grouped bars colored
    by sign -- keeps signal identity (the subplot title) and bull/bear sign
    (bar color) as two separate encodings instead of overloading one color
    channel with both identity and state.
    """
    from plotly.subplots import make_subplots

    signals = list(COLOR_SERIES)
    horizons_all = sorted({int(h) for sig in ic_stats.get("level_ic", {}).values()
                          for h in sig})
    fig = make_subplots(rows=1, cols=len(signals), subplot_titles=signals,
                        shared_yaxes=True)
    for i, signal in enumerate(signals, start=1):
        by_h = ic_stats.get("level_ic", {}).get(signal, {})
        y = [by_h.get(str(h), {}).get("spread_pct") for h in horizons_all]
        colors = [COLOR_GOOD if (v or 0) >= 0 else COLOR_CRITICAL for v in y]
        fig.add_trace(go.Bar(x=[f"{h}d" for h in horizons_all], y=y,
                             marker_color=colors, showlegend=False, name=signal),
                      row=1, col=i)
    fig.update_layout(title="Bullish minus bearish mean excess return by horizon",
                      template="plotly_white")
    fig.update_yaxes(title_text="Spread (%)", row=1, col=1)
    return fig


def build_scatter_section(panel: pd.DataFrame, signals=SIGNALS, horizons=HORIZONS,
                          sample_n: int = 5000, seed: int = 42) -> go.Figure:
    """
    Rating-vs-forward-return scatter with a single flat "signal @ horizon"
    dropdown (15 options for 3 signals x 5 horizons) -- not two independent
    dropdowns, since Plotly updatemenu buttons fully replace visibility state
    and two independently-stateful dropdowns can't be combined without
    custom JS. Each combo is downsampled to `sample_n` points (deterministic)
    for render performance; panel.parquet retains full data.
    """
    rng = np.random.default_rng(seed)
    combos = [(sig, h) for sig in signals for h in horizons]
    fig = go.Figure()
    for i, (sig, h) in enumerate(combos):
        col = f"fwd_{h}d"
        sub = panel.dropna(subset=[sig, col])
        if len(sub) > sample_n:
            sub = sub.iloc[rng.choice(len(sub), sample_n, replace=False)]
        fig.add_trace(go.Scattergl(
            x=sub[sig], y=100 * sub[col], mode="markers",
            marker=dict(size=5, color=COLOR_SERIES[sig], opacity=0.4),
            text=sub["symbol"].astype(str) + " " + sub["date"].astype(str),
            hovertemplate="%{text}<br>signal=%{x:.3f}<br>fwd return=%{y:.2f}%<extra></extra>",
            visible=(i == 0), showlegend=False, name=f"{sig} @ {h}d"))

    buttons = []
    for i, (sig, h) in enumerate(combos):
        vis = [j == i for j in range(len(combos))]
        buttons.append(dict(label=f"{sig} @ {h}d", method="update",
                            args=[{"visible": vis},
                                  {"xaxis.title.text": sig,
                                   "yaxis.title.text": f"Forward {h}d excess return (%)"}]))
    fig.update_layout(
        updatemenus=[dict(buttons=buttons, x=0, y=1.15, xanchor="left")],
        title="Rating level vs forward excess return",
        xaxis_title=combos[0][0],
        yaxis_title=f"Forward {combos[0][1]}d excess return (%)",
        template="plotly_white")
    return fig


def build_transition_chart(transitions_df: pd.DataFrame) -> go.Figure:
    """One line per transition type; toggle via the default Plotly legend
    click behavior (no custom JS needed for a same-y-scale multi-line toggle)."""
    fig = go.Figure()
    if transitions_df.empty:
        fig.update_layout(title="Rating-transition event study (no qualifying transitions)")
        return fig
    for (frm, to), grp in transitions_df.groupby(["from_label", "to_label"]):
        grp = grp.sort_values("rel_day")
        fig.add_trace(go.Scatter(
            x=grp["rel_day"], y=grp["mean_car_pct"], mode="lines",
            name=f"{frm} -> {to}  (n={int(grp['n'].iloc[0])})"))
    fig.update_layout(title="Average cumulative return after a rating transition "
                            "(click legend entries to toggle)",
                      xaxis_title="Trading days after transition",
                      yaxis_title="Mean cumulative excess return (%)",
                      template="plotly_white", hovermode="x unified")
    fig.add_hline(y=0, line_color=COLOR_MUTED, line_width=1)
    return fig


def build_price_trades_chart(panel: pd.DataFrame, trades: pd.DataFrame,
                             symbols: "list[str] | None" = None) -> go.Figure:
    """
    Symbol dropdown (one option per symbol) over a fixed 5-trace-per-symbol
    layout: price line, win-entry markers, win-exit markers, loss-entry
    markers, loss-exit markers -- kept fixed-width (even when a symbol has
    zero wins or losses) so the dropdown's visibility-array indexing stays
    simple and correct regardless of trade counts. Direction is shape
    (triangle-up=long, triangle-down=short); outcome is color (state, not
    identity: COLOR_GOOD/COLOR_CRITICAL).
    """
    symbols = symbols or sorted(panel["symbol"].unique())
    traces_per_symbol = 5
    fig = go.Figure()
    for i, sym in enumerate(symbols):
        p = panel[panel["symbol"] == sym].sort_values("date")
        fig.add_trace(go.Scatter(x=p["date"], y=p["close"], mode="lines",
                                 line=dict(color="#52514e", width=1.5),
                                 name=f"{sym} price", visible=(i == 0),
                                 showlegend=False))
        t = trades[trades["symbol"] == sym] if not trades.empty else trades
        wins = t[t["pnl_dollars"] > 0] if len(t) else t
        losses = t[t["pnl_dollars"] <= 0] if len(t) else t
        for side_df, tag in ((wins, "win"), (losses, "loss")):
            color = COLOR_GOOD if tag == "win" else COLOR_CRITICAL
            shapes = side_df["side"].map({"long": "triangle-up",
                                          "short": "triangle-down"}) if len(side_df) else []
            fig.add_trace(go.Scatter(
                x=side_df["entry_date"], y=side_df["entry_price"], mode="markers",
                marker=dict(symbol=list(shapes), size=11, color=color,
                           line=dict(width=1, color="#0b0b0b")),
                name=f"{sym} entry ({tag})", visible=(i == 0), showlegend=False,
                hovertemplate="entry %{x}<br>$%{y:.2f}<extra></extra>"))
            fig.add_trace(go.Scatter(
                x=side_df["exit_date"], y=side_df["exit_price"], mode="markers",
                marker=dict(symbol="x", size=9, color=color,
                           line=dict(width=1, color="#0b0b0b")),
                name=f"{sym} exit ({tag})", visible=(i == 0), showlegend=False,
                hovertemplate="exit %{x}<br>$%{y:.2f}<extra></extra>"))

    buttons = []
    for i, sym in enumerate(symbols):
        vis = [False] * (len(symbols) * traces_per_symbol)
        for j in range(traces_per_symbol):
            vis[i * traces_per_symbol + j] = True
        buttons.append(dict(label=sym, method="update", args=[{"visible": vis}]))

    fig.update_layout(
        updatemenus=[dict(buttons=buttons, x=0, y=1.15, xanchor="left")],
        title=f"Price with simulated trades -- {symbols[0] if symbols else ''}",
        yaxis_title="Price ($)", template="plotly_white")
    return fig


def build_cumulative_pnl_chart(trades: pd.DataFrame) -> go.Figure:
    if trades.empty:
        fig = go.Figure()
        fig.update_layout(title="Cumulative realized P&L (no trades)")
        return fig
    t = trades.sort_values("exit_date").reset_index(drop=True)
    t["cum_pnl"] = t["pnl_dollars"].cumsum()
    colors = [COLOR_GOOD if v > 0 else COLOR_CRITICAL for v in t["pnl_dollars"]]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=t["exit_date"], y=t["cum_pnl"], mode="lines+markers",
        line=dict(color="#2a78d6", width=2),
        marker=dict(size=7, color=colors, line=dict(width=1, color="#0b0b0b")),
        text=[f"{r.symbol} {r.side}: ${r.pnl_dollars:,.0f} ({r.pnl_pct:+.2f}%)"
             for r in t.itertuples()],
        hovertemplate="%{x}<br>%{text}<br>cumulative: $%{y:,.0f}<extra></extra>",
        name="Cumulative realized P&L"))

    show_annotations = len(t) <= 200
    annotations = [dict(x=r.exit_date, y=r.cum_pnl,
                        text=f"${r.pnl_dollars:,.0f} ({r.pnl_pct:+.1f}%)",
                        showarrow=True, arrowhead=2, ax=0, ay=-30,
                        font=dict(size=9, color="#52514e"))
                  for r in t.itertuples()] if show_annotations else []
    fig.update_layout(
        title="Cumulative realized P&L -- sum of independently-sized $10k trades "
             "(not a capital-constrained portfolio curve)",
        yaxis_title="Cumulative P&L ($)", template="plotly_white",
        annotations=annotations, hovermode="closest")
    if not show_annotations:
        fig.add_annotation(text="Per-trade $ / % labels hidden above 200 trades -- "
                                "hover each point for its P&L.",
                           xref="paper", yref="paper", x=0, y=1.08, showarrow=False,
                           font=dict(size=11, color=COLOR_MUTED))
    return fig


def build_headline_table(ic_stats: dict) -> go.Figure:
    rows = build_headline_rows(ic_stats)
    header = ["Signal", "Horizon", "n", "Pooled IC", "Pooled p", "Daily IC",
             "IC t-stat", "IC days", "Spread %", "Spread t"]
    if rows:
        cols = list(zip(*[[r["signal"], f"{r['horizon']}d", r["n"], r["pooled_ic"],
                          r["pooled_p"], r["mean_daily_ic"], r["ic_t_stat"],
                          r["ic_days"], r["spread_pct"], r["spread_t"]]
                         for r in rows]))
        fill_colors = [TIER_COLOR[r["tier"]] for r in rows]
    else:
        cols = [[] for _ in header]
        fill_colors = []

    fig = go.Figure(data=[go.Table(
        header=dict(values=header, fill_color="#e1e0d9", align="left"),
        cells=dict(values=cols,
                  fill_color=[["#fcfcfb"] * len(rows)] * (len(header) - 1) + [fill_colors],
                  align="left"))])
    fig.update_layout(title="Headline IC / significance by signal x horizon "
                            "(Spread t column: grey=noise, yellow=weak, green=significant)")
    return fig


HOW_TO_READ = """
<div style="max-width:900px;margin:24px auto;padding:16px;
           border:1px solid #e1e0d9;border-radius:8px;
           font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
           color:#52514e;">
<h3 style="color:#0b0b0b;margin-top:0;">How to read this report</h3>
<ul>
  <li><b>|IC| &lt; 0.02</b> is noise. <b>0.02-0.05</b> is weak-but-real only if the
      t-stat also holds (|t| &ge; 2). <b>|IC| &gt; 0.05</b> on daily data is
      suspicious -- before celebrating, re-check for a look-ahead leak.</li>
  <li>A t-stat needs roughly <b>250+ days</b> of daily IC observations before
      it's trustworthy, and this report tests 3 signals x 5 horizons x 2 stats
      -- one marginal t of about 2 among ~30 numbers is expected by chance alone.</li>
  <li><b>Sign flips across horizons</b> (e.g. positive at 1 day, negative at 3
      days) mean the signal is noise, not "momentum then reversal," unless that
      flip was predicted before looking.</li>
  <li>Universe is a fixed 69-symbol list (mega-caps + sector/bond/commodity
      ETFs) -- not a broad-market sample. Returns are excess vs SPY.</li>
  <li>The cumulative P&amp;L chart sums independently-sized $10,000 trades in
      exit-date order -- it is <b>not</b> a capital-constrained portfolio
      equity curve, since trades can overlap in time.</li>
</ul>
</div>
"""


def assemble_report(out_dir: "str | None" = None,
                    report_path: str = "storage/reports/tv_rating_backtest.html") -> str:
    ic_stats, panel, transitions, trades = load_artifacts(out_dir)
    symbols = sorted(panel["symbol"].unique())

    symbol_table = build_symbol_table(panel)
    table_fig = go.Figure(data=[go.Table(
        header=dict(values=list(symbol_table.columns), fill_color="#e1e0d9", align="left"),
        cells=dict(values=[symbol_table[c] for c in symbol_table.columns],
                  fill_color="#fcfcfb", align="left"))])
    table_fig.update_layout(title="Per-symbol best/worst horizon IC (rating_all)")

    figs = [
        build_headline_table(ic_stats),
        build_ic_bar_chart(ic_stats),
        build_spread_chart(ic_stats),
        build_scatter_section(panel),
        build_transition_chart(transitions),
        table_fig,
        build_price_trades_chart(panel, trades, symbols),
        build_cumulative_pnl_chart(trades),
    ]

    html_parts = ['<html><head><title>TV Rating Backtest</title></head><body>',
                 '<h1 style="font-family:system-ui,sans-serif;">'
                 'TradingView Rating Backtest</h1>']
    for i, fig in enumerate(figs):
        html_parts.append(fig.to_html(full_html=False, include_plotlyjs=(i == 0)))
    html_parts.append(HOW_TO_READ)
    html_parts.append("</body></html>")

    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(html_parts))
    return report_path


def main():
    parser = argparse.ArgumentParser(description="Build the TV rating backtest HTML report")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--report-path", default="storage/reports/tv_rating_backtest.html")
    args = parser.parse_args()
    path = assemble_report(args.out_dir, args.report_path)
    print(f"-> wrote {path}")


if __name__ == "__main__":
    main()
