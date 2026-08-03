r"""
backtest_app.py -- live interactive explorer for the unified evaluation
framework's results. Reads registry + run artifacts, and for signals with a
recognized TradeRule shape, re-runs evaluation.trades.simulate() live as the
user drags threshold sliders -- no new backtest math, everything here
delegates to evaluation/ and generate_eval_report's existing loaders.

See docs/superpowers/specs/2026-08-03-interactive-backtest-explorer-design.md.

Usage
-----
  C:\ProgramData\anaconda3\python.exe backtest_app.py
  (opens http://127.0.0.1:8050)
"""

import dash
from dash import dcc, html, Input, Output
import pandas as pd
import plotly.graph_objects as go

from evaluation import registry as ev_registry
from evaluation import trades as ev_trades
from evaluation import adapters as ev_adapters
from evaluation.contracts import TradeRule
from generate_eval_report import find_latest, load_run

# Signals whose TradeRule shape we know how to rebuild live: name -> cache
# builder. Only tv_threshold exists today (adapters.tv_threshold_rule() /
# adapters.rating_cache()); a signal not in this dict still shows its IC &
# Significance panel, just not the Live Trade Rule / Symbol Explorer / P&L
# panels (see has_trade_rule()).
KNOWN_TRADE_RULE_SIGNALS = {
    "tv_threshold": ev_adapters.rating_cache,
}

_CACHE: dict = {}          # signal name -> dict[symbol -> DataFrame], built lazily
_CACHE_RUN_ID: dict = {}   # signal name -> run_id the cache was built for


def list_evaluated_signals() -> "list[dict]":
    """Registry input_names, deduped/sorted, flagged for missing local artifacts."""
    reg = ev_registry.load()
    if reg.empty:
        return []
    names = sorted(reg["input_name"].unique())
    return [{"name": n, "has_local_artifacts": find_latest(n) is not None}
            for n in names]


def load_signal(name: str) -> dict:
    """Latest run's artifacts for one signal, or an {"error": ...} dict if
    the registry knows this name but no local run directory exists (e.g. a
    registry synced from another machine without its gitignored artifacts)."""
    run_dir = find_latest(name)
    if run_dir is None:
        return {"error": f"no local artifacts for {name!r} -- run "
                         f"evaluate.py --adapter ... first"}
    results, meta, trades = load_run(run_dir)
    return {"run_dir": run_dir, "results": results, "meta": meta,
            "trades": trades}


DEFAULT_NOTIONAL = 10_000.0


def _crossed_up(series, level):
    return (series >= level) & (series.shift(1) < level)


def _crossed_down(series, level):
    return (series <= level) & (series.shift(1) > level)


def build_tv_threshold_rule(bull_min: float, exit_long_max: float,
                            bear_max: float, exit_short_min: float,
                            notional: float = DEFAULT_NOTIONAL) -> TradeRule:
    """Same crossed-up/crossed-down shape as evaluation.adapters.
    tv_threshold_rule(), with slider-driven thresholds instead of
    tv_rating_eval's fixed module constants."""
    return TradeRule(
        name="tv_threshold_live",
        entries=lambda d: _crossed_up(d["rating_all"], bull_min),
        exits=lambda d: d["rating_all"] < exit_long_max,
        side="both",
        short_entries=lambda d: _crossed_down(d["rating_all"], bear_max),
        short_exits=lambda d: d["rating_all"] > exit_short_min,
        notional=notional)


def has_trade_rule(name: str) -> bool:
    return name in KNOWN_TRADE_RULE_SIGNALS


def get_cache(name: str, run_id: str) -> dict:
    """Per-symbol price+rating cache for one signal's run, built once and
    reused (module-level, server-side -- NOT round-tripped through
    dcc.Store, which would serialize the full multi-decade panel to
    browser JSON on every slider tick). Keyed by (name, run_id) so a
    Refresh that finds a newer run invalidates and rebuilds the cache
    rather than silently serving a stale panel under a fresh-looking
    banner."""
    if name not in _CACHE or _CACHE_RUN_ID.get(name) != run_id:
        builder = KNOWN_TRADE_RULE_SIGNALS[name]     # raises KeyError if unknown
        _CACHE[name] = builder()
        _CACHE_RUN_ID[name] = run_id
    return _CACHE[name]


_SIM_CACHE: dict = {}   # (name, run_id, bull_min, exit_long_max, bear_max, exit_short_min) -> (trades, summary)


def simulate_live(name: str, run_id: str, bull_min: float, exit_long_max: float,
                  bear_max: float, exit_short_min: float,
                  notional: "float | None" = None):
    """Re-run the trade simulation in-process against the cached panel --
    no disk I/O, cost bounded by in-memory panel size. Memoized by its
    full input key so switching the symbol dropdown (which doesn't change
    any of these inputs) reuses the already-computed trades instead of
    re-simulating the whole universe."""
    key = (name, run_id, bull_min, exit_long_max, bear_max, exit_short_min)
    if key in _SIM_CACHE:
        return _SIM_CACHE[key]
    cache = get_cache(name, run_id)
    rule = build_tv_threshold_rule(bull_min, exit_long_max, bear_max,
                                   exit_short_min,
                                   notional or DEFAULT_NOTIONAL)
    trades = ev_trades.simulate(rule, cache)
    summary = ev_trades.trade_summary(trades)
    _SIM_CACHE[key] = (trades, summary)
    return trades, summary


BASELINE_DIFF_KEYS = ("n_trades", "win_rate_pct", "total_pnl_dollars")


def _fmt_money(v) -> str:
    return "n/a" if v is None else f"${v:,.0f}"


def _fmt_pct(v) -> str:
    return "n/a" if v is None else f"{v}%"


def baseline_vs_live(baseline_summary: dict, live_summary: dict) -> dict:
    return {k: {"baseline": baseline_summary.get(k), "live": live_summary.get(k)}
            for k in BASELINE_DIFF_KEYS}


SLOT0, COLOR_GOOD, COLOR_CRITICAL = "#2a78d6", "#0ca30c", "#d03b3b"


def symbol_price_fig(symbol: str, price_df: "pd.DataFrame", trades_df: "pd.DataFrame") -> go.Figure:
    """Price line always renders; entry/exit markers only if this symbol has
    trades at the current threshold settings."""
    fig = go.Figure()
    fig.add_scatter(x=price_df.index, y=price_df["close"], mode="lines",
                    name=symbol, line=dict(color=SLOT0))
    sub = trades_df[trades_df["symbol"] == symbol] if not trades_df.empty else trades_df
    if not sub.empty:
        wins, losses = sub[sub["pnl_dollars"] > 0], sub[sub["pnl_dollars"] <= 0]
        for grp, color, label in ((wins, COLOR_GOOD, "win"),
                                  (losses, COLOR_CRITICAL, "loss")):
            if grp.empty:
                continue
            fig.add_scatter(x=grp["entry_date"], y=grp["entry_price"],
                            mode="markers", name=f"entry ({label})",
                            marker=dict(symbol="triangle-up", color=color, size=10),
                            hovertemplate="entry %{x}<extra></extra>")
            fig.add_scatter(x=grp["exit_date"], y=grp["exit_price"],
                            mode="markers", name=f"exit ({label})",
                            marker=dict(symbol="x", color=color, size=9),
                            hovertemplate="exit %{x}<extra></extra>")
    fig.update_layout(title=f"{symbol} price + trades", height=360)
    return fig


def cumulative_pnl_fig(trades_df: "pd.DataFrame") -> "go.Figure | None":
    if trades_df.empty:
        return None
    ordered = trades_df.sort_values("exit_date")
    cum = ordered["pnl_dollars"].cumsum()
    fig = go.Figure()
    fig.add_scatter(x=ordered["exit_date"], y=cum, mode="lines",
                    line=dict(color=SLOT0),
                    customdata=ordered[["symbol", "pnl_dollars", "pnl_pct"]].to_numpy(),
                    hovertemplate="%{customdata[0]}: $%{customdata[1]:.2f} "
                                 "(%{customdata[2]:.2f}%%)<extra></extra>")
    fig.update_layout(title="Cumulative P&L (current thresholds)", height=320)
    return fig


from generate_eval_report import _ic_by_horizon, _spread_with_ci, _regimes, _trades_fig

SLIDER_MIN, SLIDER_MAX, SLIDER_STEP = -1.0, 1.0, 0.05


def _slider(id_, value):
    return dcc.Slider(id=id_, min=SLIDER_MIN, max=SLIDER_MAX, step=SLIDER_STEP,
                      value=value, updatemode="mouseup",
                      marks={-1: "-1", 0: "0", 1: "1"})


def build_layout(signals: "list[dict]") -> "html.Div":
    options = [{"label": s["name"] + ("" if s["has_local_artifacts"]
                                      else "  [no local artifacts]"),
               "value": s["name"]} for s in signals]
    return html.Div([
        html.Div([
            dcc.Dropdown(id="signal-dropdown", options=options,
                        value=options[0]["value"] if options else None,
                        placeholder="no evaluated signals yet"),
            html.Button("Refresh", id="refresh-button", n_clicks=0),
            html.Div(id="run-banner"),
        ]),
        html.Div([
            dcc.Loading(html.Div(id="ic-panel")),
            html.Div([
                html.Div("Bull entry"), _slider("bull-min", 0.5),
                html.Div("Exit long"), _slider("exit-long-max", 0.1),
                html.Div("Bear entry"), _slider("bear-max", -0.5),
                html.Div("Exit short"), _slider("exit-short-min", -0.1),
                dcc.Loading(html.Div(id="trade-summary")),
            ]),
        ]),
        html.Div([
            dcc.Dropdown(id="symbol-dropdown", placeholder="select a symbol"),
            dcc.Loading(dcc.Graph(id="symbol-fig")),
        ]),
        dcc.Loading(dcc.Graph(id="pnl-fig")),
        dcc.Store(id="signal-store"),
    ])


def _render_ic_panel(meta: dict, results: dict, trades: "pd.DataFrame | None") -> "list":
    if meta.get("input_type") == "trade_rule":
        fig = _trades_fig(trades)
        return [dcc.Graph(figure=fig)] if fig is not None else []
    ic = results.get("ic", {})
    figs = [_ic_by_horizon(ic), _spread_with_ci(ic, results.get("tier2", {})),
           _regimes(results.get("tier3", {}))]
    return [dcc.Graph(figure=f) for f in figs if f is not None]


def register_callbacks(app: "dash.Dash") -> None:
    @app.callback(
        Output("signal-store", "data"), Output("run-banner", "children"),
        Output("ic-panel", "children"), Output("symbol-dropdown", "options"),
        Input("signal-dropdown", "value"), Input("refresh-button", "n_clicks"))
    def _on_signal_change(name, _n_clicks):
        if not name:
            return None, "no evaluated signals yet", [], []
        loaded = load_signal(name)
        if "error" in loaded:
            return None, loaded["error"], [], []
        meta = loaded["meta"]
        banner = (f'{meta.get("run_id")} - {meta.get("date_range")} - '
                 f'loaded {pd.Timestamp.now():%H:%M:%S}')
        symbol_options = []
        run_id = meta.get("run_id")
        if has_trade_rule(name):
            cache = get_cache(name, run_id)
            symbol_options = [{"label": s, "value": s} for s in sorted(cache.keys())]
            recorded_n = len(meta.get("universe") or [])
            if recorded_n and len(cache) != recorded_n:
                banner += (f' [WARNING: live universe has {len(cache)} symbols, '
                          f'recorded run had {recorded_n} -- live vs baseline '
                          f'numbers may not be directly comparable]')
        return ({"name": name, "run_id": run_id, "results": loaded["results"]}, banner,
               _render_ic_panel(meta, loaded["results"], loaded["trades"]), symbol_options)

    @app.callback(
        Output("trade-summary", "children"), Output("symbol-fig", "figure"),
        Output("pnl-fig", "figure"),
        Input("signal-store", "data"), Input("bull-min", "value"),
        Input("exit-long-max", "value"), Input("bear-max", "value"),
        Input("exit-short-min", "value"), Input("symbol-dropdown", "value"))
    def _on_sliders_change(store, bull_min, exit_long_max, bear_max,
                          exit_short_min, symbol):
        empty_fig = go.Figure()
        if not store:
            return "select a signal", empty_fig, empty_fig
        if not has_trade_rule(store["name"]):
            return "no trade rule defined for this signal", empty_fig, empty_fig
        trades, summary = simulate_live(store["name"], store["run_id"], bull_min,
                                        exit_long_max, bear_max, exit_short_min)
        baseline = store["results"].get("summary", {})
        diff = baseline_vs_live(baseline, summary)
        if summary.get("n_trades", 0) == 0:
            text = "0 realized trades at this threshold"
        else:
            text = (f'n={diff["n_trades"]["live"]} trades | '
                   f'win {_fmt_pct(diff["win_rate_pct"]["live"])} | '
                   f'{_fmt_money(diff["total_pnl_dollars"]["live"])} net '
                   f'(baseline: {diff["n_trades"]["baseline"]} / '
                   f'{_fmt_pct(diff["win_rate_pct"]["baseline"])} / '
                   f'{_fmt_money(diff["total_pnl_dollars"]["baseline"])})')
        cache = get_cache(store["name"], store["run_id"])
        sym_fig = (symbol_price_fig(symbol, cache[symbol], trades)
                  if symbol and symbol in cache else empty_fig)
        pnl_fig = cumulative_pnl_fig(trades) or empty_fig
        return text, sym_fig, pnl_fig


app = dash.Dash(__name__)
app.layout = build_layout(list_evaluated_signals())
register_callbacks(app)


if __name__ == "__main__":
    app.run(debug=True)
