r"""
generate_tearsheet.py -- self-contained performance tearsheet (W3 render stage).

Renders what evaluation/tearsheet.py computes. This file draws and NEVER
computes a statistic: every number on the page comes from tearsheet.py, the same
functions W4's interactive layer will call. That split is the point -- a chart
that recomputes its own numbers is a second implementation waiting to disagree
with the first.

Palette and layout constants are imported from generate_eval_report rather than
copied, so the two reports cannot drift apart visually.

Usage
-----
  # from a unified-eval run directory (uses its trades.parquet)
  C:\ProgramData\anaconda3\python.exe generate_tearsheet.py --latest pine_my_strategy

  # from an explicit run directory
  C:\ProgramData\anaconda3\python.exe generate_tearsheet.py --run-dir storage/reports/eval/foo_20260817_120000

Output: <run_dir>/tearsheet.html (override with --out)
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from evaluation import tearsheet as ts
from generate_eval_report import (BASE, COLOR_CRITICAL, COLOR_GOOD,
                                  COLOR_MUTED, EVAL_ROOT, GRID, INK, INK2,
                                  PAGE, SLOT, SURFACE, _layout, _tile,
                                  find_latest, load_run)


def _fmt(v, nd=2, suffix=""):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    if isinstance(v, (int, np.integer)):
        return f"{v:,}{suffix}"
    return f"{v:,.{nd}f}{suffix}"


# --------------------------------------------------------------- figures


def _equity_fig(returns, bench=None) -> "go.Figure | None":
    eq = ts.to_equity(returns)
    if eq.empty:
        return None
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=eq.index, y=eq.to_numpy(), name="strategy",
                             line=dict(color=SLOT[0], width=2)))
    if bench is not None:
        beq = ts.to_equity(bench)
        if not beq.empty:
            fig.add_trace(go.Scatter(x=beq.index, y=beq.to_numpy(),
                                     name="benchmark",
                                     line=dict(color=SLOT[1], width=1.5,
                                               dash="dot")))
    fig.update_layout(**_layout("Equity curve (growth of $1)", "equity", 380))
    return fig


def _underwater_fig(returns) -> "go.Figure | None":
    u = ts.drawdown_series(returns)
    if u.empty:
        return None
    fig = go.Figure(go.Scatter(
        x=u.index, y=u.to_numpy(), name="drawdown", fill="tozeroy",
        line=dict(color=COLOR_CRITICAL, width=1)))
    fig.update_layout(**_layout("Underwater (drawdown from peak)", "%", 280))
    return fig


def _monthly_fig(monthly: dict) -> "go.Figure | None":
    table = monthly.get("table")
    if table is None or table.empty:
        return None
    grid = table[ts.MONTH_NAMES]
    z = grid.to_numpy(dtype=float)
    lim = float(np.nanmax(np.abs(z))) if np.isfinite(z).any() else 1.0
    fig = go.Figure(go.Heatmap(
        z=z, x=ts.MONTH_NAMES, y=[str(y) for y in grid.index],
        # diverging, symmetric about zero -- a month at 0% must read as neutral
        # regardless of how good or bad the rest of the sample was
        colorscale=[[0.0, COLOR_CRITICAL], [0.5, SURFACE], [1.0, COLOR_GOOD]],
        zmid=0.0, zmin=-lim, zmax=lim,
        text=np.where(np.isfinite(z), np.round(z, 1).astype(object), ""),
        texttemplate="%{text}", hovertemplate="%{y} %{x}: %{z:.2f}%<extra></extra>",
        colorbar=dict(title="%")))
    fig.update_layout(**_layout("Monthly returns (%)", "", 90 + 34 * len(grid)))
    fig.update_yaxes(autorange="reversed")
    return fig


def _rolling_fig(rolling: dict) -> "go.Figure | None":
    frame = rolling.get("frame")
    if frame is None or frame.empty:
        return None
    w = rolling.get("window")
    fig = go.Figure()
    for i, (col, label) in enumerate([("rolling_sharpe", "Sharpe"),
                                      ("rolling_sortino", "Sortino")]):
        fig.add_trace(go.Scatter(x=frame.index, y=frame[col].to_numpy(),
                                 name=label, line=dict(color=SLOT[i], width=1.5)))
    fig.add_trace(go.Scatter(x=frame.index, y=frame["rolling_vol_pct"].to_numpy(),
                             name="ann vol %", yaxis="y2",
                             line=dict(color=COLOR_MUTED, width=1, dash="dot")))
    lay = _layout(f"Rolling {w}-day risk-adjusted return", "ratio", 340)
    lay["yaxis2"] = dict(title="vol %", overlaying="y", side="right",
                         gridcolor=GRID, linecolor=BASE, zeroline=False)
    fig.update_layout(**lay)
    return fig


# --------------------------------------------------------------- tables


def _headline_tiles(h: dict, tail: dict) -> str:
    if h.get("headline_reason") and h.get("sharpe") is None:
        return _tile("headline", "n/a", h["headline_reason"])
    tiles = [
        _tile("CAGR", _fmt(h.get("cagr_pct"), 2, "%"),
              f"total {_fmt(h.get('total_return_pct'), 1, '%')}"),
        _tile("Sharpe", _fmt(h.get("sharpe")),
              f"Sortino {_fmt(h.get('sortino'))}"),
        _tile("Max DD", _fmt(h.get("max_drawdown_pct"), 2, "%"),
              f"Calmar {_fmt(h.get('calmar'))}"),
        _tile("Ann vol", _fmt(h.get("ann_vol_pct"), 2, "%"),
              f"hit rate {_fmt(h.get('hit_rate_pct'), 1, '%')}"),
        _tile(f"CVaR {_fmt(100 * tail.get('alpha'), 0) if tail.get('alpha') else ''}%",
              _fmt(tail.get("cvar_pct"), 2, "%"),
              f"VaR {_fmt(tail.get('var_pct'), 2, '%')}"
              if tail.get("cvar_pct") is not None
              else tail.get("tail_risk_reason", "n/a")),
        _tile("Days", _fmt(h.get("n_days")), ""),
    ]
    return ('<div style="display:flex;gap:12px;flex-wrap:wrap;'
            'margin:10px 0 18px">' + "".join(tiles) + "</div>")


def _benchmark_tiles(b: dict) -> str:
    if b.get("beta") is None:
        return (f'<p style="color:{INK2};font-size:12px">Benchmark: '
                f'{b.get("bench_reason", "unavailable")}</p>')
    tiles = [
        _tile("Beta", _fmt(b.get("beta"), 3),
              f"R2 {_fmt(b.get('r_squared'), 3)}"),
        _tile("Alpha (ann)", _fmt(b.get("alpha_ann_pct"), 2, "%"),
              f"IR {_fmt(b.get('information_ratio'))}"),
        _tile("Capture", f"{_fmt(b.get('up_capture_pct'), 0, '%')} up",
              f"{_fmt(b.get('down_capture_pct'), 0, '%')} down"),
        _tile("Tracking err", _fmt(b.get("tracking_error_pct"), 2, "%"),
              f"{b.get('n_overlap')} overlapping days"),
    ]
    return ('<div style="display:flex;gap:12px;flex-wrap:wrap;'
            'margin:10px 0 18px">' + "".join(tiles) + "</div>")


def _drawdown_table(dd: dict) -> str:
    table = dd.get("table")
    if table is None:
        return (f'<p style="color:{INK2};font-size:12px">Drawdowns: '
                f'{dd.get("dd_reason", "unavailable")}</p>')
    if table.empty:
        return (f'<p style="color:{INK2};font-size:12px">No drawdowns — the '
                f'equity curve never fell below a prior peak.</p>')

    def _d(v):
        # pd.isna, NOT `v is None`: an unrecovered drawdown is stored as None,
        # but pandas coerces it to NaT as soon as the column holds any real
        # timestamp. A `is None` check therefore works on an all-unrecovered
        # table and silently renders "NaT" on a mixed one -- which is the case
        # that occurs on real data. Caught by the W3 real-data smoke run.
        return "still under water" if pd.isna(v) else str(pd.Timestamp(v).date())

    rows = "".join(
        f'<tr><td>{pd.Timestamp(r.peak_date).date()}</td>'
        f'<td>{pd.Timestamp(r.valley_date).date()}</td>'
        f'<td>{_d(r.recovery_date)}</td>'
        f'<td style="color:{COLOR_CRITICAL}">{_fmt(r.depth_pct, 2, "%")}</td>'
        f'<td>{r.days_to_valley}</td>'
        f'<td>{"-" if pd.isna(r.days_to_recovery) else r.days_to_recovery}</td>'
        f'<td>{r.total_days}</td></tr>'
        for r in table.itertuples())
    note = ""
    if dd.get("n_unrecovered"):
        note = (f'<p style="color:{INK2};font-size:11px">'
                f'{dd["n_unrecovered"]} drawdown(s) had not recovered by the end '
                f'of the sample — shown as "still under water" rather than '
                f'closed off at the last bar.</p>')
    return (f'<h3 style="color:{INK}">Worst drawdowns '
            f'({dd.get("n_periods")} total)</h3>'
            f'<table style="border-collapse:collapse;color:{INK2};'
            'font-size:12px" border="1" cellpadding="5">'
            '<tr><th>peak</th><th>valley</th><th>recovered</th><th>depth</th>'
            '<th>days to valley</th><th>days to recover</th><th>total days</th>'
            '</tr>' + rows + "</table>" + note)


# --------------------------------------------------------------- assembly


def build_html(returns, *, title: str = "Tearsheet", subtitle: str = "",
               bench_returns=None, window: int = 63, basis_note: str = "") -> str:
    sheet = ts.tearsheet(returns, bench_returns=bench_returns, window=window)

    figs = [_equity_fig(returns, bench_returns),
            _underwater_fig(returns),
            _monthly_fig(sheet["monthly"]),
            _rolling_fig(sheet["rolling"])]
    figs = [f for f in figs if f is not None]

    parts = [f'<body style="background:{PAGE};font-family:system-ui,'
             '\'Segoe UI\',sans-serif;margin:24px">',
             f'<h2 style="color:{INK}">{title}</h2>']
    if subtitle:
        parts.append(f'<div style="color:{INK2};font-size:12px">{subtitle}</div>')
    if basis_note:
        parts.append(f'<div style="color:{INK2};font-size:12px;'
                     f'border-left:3px solid {BASE};padding:6px 10px;'
                     f'margin:10px 0">{basis_note}</div>')
    parts.append(_headline_tiles(sheet["headline"], sheet["tail_risk"]))

    for i, fig in enumerate(figs):
        parts.append(fig.to_html(full_html=False, include_plotlyjs=(i == 0)))

    parts.append(f'<h3 style="color:{INK}">Versus benchmark</h3>')
    parts.append(_benchmark_tiles(sheet["benchmark"]))
    parts.append(_drawdown_table(sheet["drawdowns"]))

    m = sheet["monthly"]
    if m.get("table") is not None:
        parts.append(f'<p style="color:{INK2};font-size:11px">'
                     f'{m["n_months"]} month(s) of data - '
                     f'{_fmt(m["pct_positive_months"], 1, "%")} positive - '
                     f'best {_fmt(m["best_month_pct"], 2, "%")} / '
                     f'worst {_fmt(m["worst_month_pct"], 2, "%")}</p>')
    parts.append("</body>")
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>tearsheet: {title}</title></head>"
            + "".join(parts) + "</html>")


REALIZED_NOTE = (
    "Basis: <b>realized</b>. Each trade's P&amp;L lands on its exit date, so an "
    "open position that is deeply under water contributes nothing until it "
    "closes. Drawdowns here are a LOWER BOUND on what was actually experienced, "
    "and this Sharpe is not comparable to a mark-to-market Sharpe from "
    "backtest.py.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Performance tearsheet from a unified-eval run directory")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-dir", help="one run's artifact directory")
    g.add_argument("--latest", help="newest run dir for this input name "
                                    f"under {EVAL_ROOT}")
    ap.add_argument("--out", default=None)
    ap.add_argument("--window", type=int, default=63,
                    help="rolling window in trading days (default 63 ~ 1 quarter)")
    ap.add_argument("--starting-equity", type=float, default=100_000.0)
    args = ap.parse_args(argv)

    run_dir = args.run_dir or find_latest(args.latest)
    if not run_dir or not os.path.isdir(run_dir):
        print(f"X run directory not found: {run_dir or args.latest}")
        return 1

    _results, meta, trades = load_run(run_dir)
    if trades is None or trades.empty:
        print(f"X {run_dir} has no trades.parquet -- nothing to build a "
              f"tearsheet from")
        return 1

    bridged = ts.daily_returns_from_trades(
        trades, starting_equity=args.starting_equity)
    if bridged["returns"] is None:
        print(f"X cannot build a return series: {bridged['returns_reason']}")
        return 1

    html = build_html(
        bridged["returns"],
        title=f"Tearsheet: {meta.get('input_name', os.path.basename(run_dir))}",
        subtitle=(f"{meta.get('input_type')} - run {meta.get('run_id')} - "
                  f"{meta.get('date_range')} - {bridged['n_trades']} trades - "
                  f"{meta.get('created_at')}"),
        window=args.window, basis_note=REALIZED_NOTE)

    out = args.out or os.path.join(run_dir, "tearsheet.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"+ tearsheet written: {out} ({len(html) // 1024} KB, from {run_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
