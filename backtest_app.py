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
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from evaluation import registry as ev_registry
from evaluation import trades as ev_trades
from evaluation import adapters as ev_adapters
from evaluation import execution as ev_execution
from evaluation import tearsheet as ev_tearsheet
from evaluation.contracts import TradeRule
from generate_eval_report import find_latest, load_run
import generate_tearsheet as gt

# Signals whose TradeRule shape we know how to rebuild live: name -> (cache
# builder, rule builder). Populated below, after the rule-builder functions
# are defined (KNOWN_TRADE_RULE_SIGNALS itself is referenced by has_trade_rule/
# get_cache/simulate_live throughout this module). A signal not in this dict
# still shows its IC & Significance panel, just not the Live Trade Rule /
# Symbol Explorer / P&L panels (see has_trade_rule()). All three entries
# share the same default full-universe adapters.rating_cache() -- tv_fade/
# tv_fade_long were evaluated on the same universe as tv_threshold, so this
# is the correct cache for them too (basket-scoped runs like tv_fade_basket/
# tv_fade_long_basket and the Russell-3000 run are NOT wired in: this cache
# builder always rebuilds the default full universe, so a basket/
# Russell-3000-named entry here would silently show the wrong live data
# under a misleadingly-scoped dropdown label).
KNOWN_TRADE_RULE_SIGNALS: dict = {}

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


def build_execution_config(*, commission_bps: float = 0.0, spread_bps: float = 0.0,
                           borrow_fee_bps: float = 0.0,
                           impact_model: "str | None" = None,
                           impact_coeff: float = 0.0,
                           stop_loss_pct: "float | None" = None,
                           take_profit_pct: "float | None" = None,
                           vol_stop_mult: "float | None" = None,
                           trailing: bool = False,
                           max_holding_days: "int | None" = None,
                           sizing_mode: str = "fixed_notional",
                           notional: float = DEFAULT_NOTIONAL,
                           fraction: "float | None" = None,
                           max_weight: "float | None" = None,
                           capital: "float | None" = None,
                           max_concurrent: "int | None" = None,
                           max_drawdown_stop: "float | None" = None
                           ) -> "ev_execution.ExecutionConfig":
    """Assemble a live ExecutionConfig from typed values -- one dataclass
    group per evaluation/execution.py group, no new grouping invented.
    Raises ValueError (via the dataclasses' own __post_init__) on an
    invalid combination; callers catch it and show the message inline
    rather than letting it crash the app."""
    return ev_execution.ExecutionConfig(
        name="live",
        costs=ev_execution.CostModel(
            commission_bps=commission_bps, spread_bps=spread_bps,
            borrow_fee_bps=borrow_fee_bps, impact_model=impact_model,
            impact_coeff=impact_coeff),
        risk=ev_execution.RiskControls(
            stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
            vol_stop_mult=vol_stop_mult, trailing=trailing,
            max_holding_days=max_holding_days),
        sizing=ev_execution.Sizing(
            mode=sizing_mode, notional=notional, fraction=fraction,
            max_weight=max_weight),
        limits=ev_execution.PortfolioLimits(
            capital=capital, max_concurrent=max_concurrent,
            max_drawdown_stop=max_drawdown_stop))


def resolve_execution_config(*, commission_bps, spread_bps, borrow_fee_bps,
                             impact_model, impact_coeff, stop_loss_pct,
                             take_profit_pct, vol_stop_mult, trailing,
                             max_holding_days, sizing_mode, sizing_notional,
                             sizing_fraction, sizing_max_weight, limits_capital,
                             limits_max_concurrent, limits_max_drawdown_stop
                             ) -> "tuple[ev_execution.ExecutionConfig | None, str]":
    """Adapt raw Dash control values into build_execution_config()'s typed
    kwargs and catch the ValueError an invalid combination raises, so the
    caller can show it inline instead of the callback crashing.

    trailing: dcc.Checklist value, a list ("trailing" in it, or empty).
    impact_model: dropdown value; "none" is the not-clearable sentinel for
    Python None (dcc.Dropdown can't hold None as a real option value).
    sizing_notional: blank (None) falls back to DEFAULT_NOTIONAL rather than
    reaching Sizing's `notional must be > 0` check with None, which would
    raise TypeError instead of the intended ValueError.
    """
    try:
        cfg = build_execution_config(
            commission_bps=commission_bps, spread_bps=spread_bps,
            borrow_fee_bps=borrow_fee_bps,
            impact_model=None if impact_model == "none" else impact_model,
            impact_coeff=impact_coeff, stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct, vol_stop_mult=vol_stop_mult,
            trailing=bool(trailing), max_holding_days=max_holding_days,
            sizing_mode=sizing_mode,
            notional=(sizing_notional if sizing_notional is not None
                      else DEFAULT_NOTIONAL),
            fraction=sizing_fraction, max_weight=sizing_max_weight,
            capital=limits_capital, max_concurrent=limits_max_concurrent,
            max_drawdown_stop=limits_max_drawdown_stop)
        return cfg, ""
    except ValueError as exc:
        return None, f"Execution config error: {exc}"


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


def build_tv_fade_rule(bull_min: float, exit_long_max: float,
                       bear_max: float, exit_short_min: float,
                       notional: float = DEFAULT_NOTIONAL) -> TradeRule:
    """tv_threshold with sides swapped at each trigger (see
    experiments/2026-08-08_tv-technical-rating-signal-eval.md): go LONG on
    the bear-entry cross (buy the oversold crash) instead of short, go
    SHORT on the bull-entry cross (fade the overbought surge) instead of
    long. NOTE: the layout's slider labels ("Bull entry"/"Bear entry") are
    shared across all signals and describe tv_threshold's semantics -- for
    this rule "Bull entry" drives the SHORT trigger and "Bear entry" drives
    the LONG trigger, the opposite of what the label says."""
    return TradeRule(
        name="tv_fade_live",
        entries=lambda d: _crossed_down(d["rating_all"], bear_max),
        exits=lambda d: d["rating_all"] > exit_short_min,
        side="both",
        short_entries=lambda d: _crossed_up(d["rating_all"], bull_min),
        short_exits=lambda d: d["rating_all"] < exit_long_max,
        notional=notional)


def build_tv_fade_long_rule(bull_min: float, exit_long_max: float,
                            bear_max: float, exit_short_min: float,
                            notional: float = DEFAULT_NOTIONAL) -> TradeRule:
    """Long-only half of build_tv_fade_rule (the side that showed a
    significant edge on the 69-symbol universe but failed to replicate at
    Russell 3000 scale -- see the writeup). bull_min/exit_long_max are
    accepted for a uniform call signature but unused: this rule has no
    short leg."""
    return TradeRule(
        name="tv_fade_long_live",
        entries=lambda d: _crossed_down(d["rating_all"], bear_max),
        exits=lambda d: d["rating_all"] > exit_short_min,
        side="long",
        notional=notional)


KNOWN_TRADE_RULE_SIGNALS.update({
    "tv_threshold": (ev_adapters.rating_cache, build_tv_threshold_rule),
    "tv_fade": (ev_adapters.rating_cache, build_tv_fade_rule),
    "tv_fade_long": (ev_adapters.rating_cache, build_tv_fade_long_rule),
})


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
        cache_builder, _ = KNOWN_TRADE_RULE_SIGNALS[name]   # raises KeyError if unknown
        _CACHE[name] = cache_builder()
        _CACHE_RUN_ID[name] = run_id
    return _CACHE[name]


_SIM_CACHE: dict = {}   # (name, run_id, bull_min, exit_long_max, bear_max, exit_short_min) -> (trades, summary)


def simulate_live(name: str, run_id: str, bull_min: float, exit_long_max: float,
                  bear_max: float, exit_short_min: float,
                  notional: "float | None" = None, *,
                  config: "ev_execution.ExecutionConfig | None" = None):
    """Re-run the trade simulation in-process against the cached panel --
    no disk I/O, cost bounded by in-memory panel size. Memoized by its full
    input key, INCLUDING the execution config's hash, so switching the
    symbol dropdown (which doesn't change any of these inputs) reuses the
    already-computed trades, and two different execution configs against
    identical thresholds never collide in the memo. config=None means
    ExecutionConfig LEGACY (today's behavior: no costs, no stops,
    unlimited concurrency) -- unchanged from before this config parameter
    existed."""
    key = (name, run_id, bull_min, exit_long_max, bear_max, exit_short_min,
           ev_execution.config_hash(config))
    if key in _SIM_CACHE:
        return _SIM_CACHE[key]
    cache = get_cache(name, run_id)
    _, rule_builder = KNOWN_TRADE_RULE_SIGNALS[name]
    rule = rule_builder(bull_min, exit_long_max, bear_max, exit_short_min,
                        notional or DEFAULT_NOTIONAL)
    trades = ev_trades.simulate(rule, cache, config=config)
    summary = ev_trades.trade_summary(trades)
    _SIM_CACHE[key] = (trades, summary)
    return trades, summary


def live_tearsheet(trades: "pd.DataFrame | None") -> dict:
    """Bridge realized trades -> the same tearsheet dict generate_tearsheet.py
    computes for the static HTML report -- daily_returns_from_trades() then
    tearsheet(), both unchanged from W3. Returns {"returns_reason": ...}
    when there aren't enough realized trades to build a return series, the
    same shape daily_returns_from_trades() itself uses for its empty
    states, so callers check one key regardless of where the gap occurred."""
    if trades is None or trades.empty:
        return {"returns_reason": "no realized trades"}
    bridged = ev_tearsheet.daily_returns_from_trades(trades)
    if bridged["returns"] is None:
        return {"returns_reason": bridged["returns_reason"]}
    return ev_tearsheet.tearsheet(bridged["returns"])


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


def render_risk_card(metrics: dict) -> "html.Div":
    """Render advanced risk metrics and factor attribution card."""
    items = [
        ("Sortino Ratio", metrics.get("sortino")),
        ("Calmar Ratio", metrics.get("calmar")),
        ("Omega Ratio", metrics.get("omega")),
        ("VaR (95%)", f"{metrics.get('var_95_pct')}%" if metrics.get("var_95_pct") is not None else None),
        ("CVaR (95%)", f"{metrics.get('cvar_95_pct')}%" if metrics.get("cvar_95_pct") is not None else None),
        ("Gain-to-Pain", metrics.get("gain_to_pain")),
        ("FF Alpha (ann)", f"{metrics.get('ff_alpha_ann')}%" if metrics.get("ff_alpha_ann") is not None else None),
        ("FF R²", metrics.get("ff_r_squared")),
    ]
    cards = []
    for label, val in items:
        display_val = "n/a" if val is None else str(val)
        cards.append(html.Div([
            html.Div(label, style={"fontSize": "12px", "color": "#666"}),
            html.Div(display_val, style={"fontSize": "16px", "fontWeight": "bold", "marginTop": "2px"}),
        ], style={
            "border": "1px solid #ddd", "borderRadius": "4px", "padding": "8px 12px",
            "minWidth": "110px", "textAlign": "center", "backgroundColor": "#f9f9f9"
        }))
    return html.Div(cards, style={"display": "flex", "flexWrap": "wrap", "gap": "10px", "marginTop": "15px"})


def render_tearsheet(sheet: dict) -> "list":
    """Dash children for the live tearsheet section. Reuses
    generate_tearsheet.py's figure/HTML builders directly -- no metric or
    chart logic duplicated here, matching the W3 compute/render split this
    was built for. HTML-string builders (_headline_tiles, _drawdown_table)
    render via dcc.Markdown(dangerously_allow_html=True); Figure builders
    (_monthly_fig, _rolling_fig) render via dcc.Graph and are skipped (not
    an empty Graph) when the underlying data is too thin to plot."""
    if "returns_reason" in sheet:
        return [html.Div(f"no realized trades to compute tearsheet: "
                         f"{sheet['returns_reason']}")]
    children = [dcc.Markdown(gt._headline_tiles(sheet["headline"]),
                             dangerously_allow_html=True)]
    monthly_fig = gt._monthly_fig(sheet["monthly"])
    if monthly_fig is not None:
        children.append(dcc.Graph(figure=monthly_fig))
    rolling_fig = gt._rolling_fig(sheet["rolling"])
    if rolling_fig is not None:
        children.append(dcc.Graph(figure=rolling_fig))
    children.append(dcc.Markdown(gt._drawdown_table(sheet["drawdowns"]),
                                 dangerously_allow_html=True))
    return children


def parameter_heatmap_fig(name: str, run_id: str) -> go.Figure:
    """2D Parameter Sensitivity Heatmap (Bull Entry vs. Exit Long)."""
    bull_range = np.linspace(0.2, 0.8, 5)
    exit_range = np.linspace(-0.2, 0.4, 5)
    z = np.zeros((len(exit_range), len(bull_range)))

    for j, b in enumerate(bull_range):
        for i, e in enumerate(exit_range):
            try:
                _, summary = simulate_live(name, run_id, float(b), float(e), -0.5, -0.1)
                z[i, j] = summary.get("total_pnl_dollars", 0.0)
            except Exception:
                z[i, j] = 0.0

    fig = go.Figure(data=go.Heatmap(
        z=z, x=[round(b, 2) for b in bull_range], y=[round(e, 2) for e in exit_range],
        colorscale="Viridis"
    ))
    fig.update_layout(
        title="Parameter Sensitivity: Bull Entry vs Exit Long P&L ($)",
        xaxis_title="Bull Entry Threshold", yaxis_title="Exit Long Threshold",
        height=340
    )
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
        dcc.Loading(html.Div(id="risk-card-container")),
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
        dcc.Loading(dcc.Graph(id="heatmap-fig")),
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
        Output("risk-card-container", "children"),
        Input("signal-dropdown", "value"), Input("refresh-button", "n_clicks"))
    def _on_signal_change(name, _n_clicks):
        if not name:
            return None, "no evaluated signals yet", [], [], None
        loaded = load_signal(name)
        if "error" in loaded:
            return None, loaded["error"], [], [], None
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
        risk_card = render_risk_card(loaded["results"].get("summary", {}))
        return ({"name": name, "run_id": run_id, "results": loaded["results"]}, banner,
               _render_ic_panel(meta, loaded["results"], loaded["trades"]), symbol_options, risk_card)

    @app.callback(
        Output("trade-summary", "children"), Output("symbol-fig", "figure"),
        Output("pnl-fig", "figure"), Output("heatmap-fig", "figure"),
        Input("signal-store", "data"), Input("bull-min", "value"),
        Input("exit-long-max", "value"), Input("bear-max", "value"),
        Input("exit-short-min", "value"), Input("symbol-dropdown", "value"))
    def _on_sliders_change(store, bull_min, exit_long_max, bear_max,
                          exit_short_min, symbol):
        empty_fig = go.Figure()
        if not store:
            return "select a signal", empty_fig, empty_fig, empty_fig
        if not has_trade_rule(store["name"]):
            return "no trade rule defined for this signal", empty_fig, empty_fig, empty_fig
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
        h_fig = parameter_heatmap_fig(store["name"], store["run_id"])
        return text, sym_fig, pnl_fig, h_fig


app = dash.Dash(__name__)
app.layout = build_layout(list_evaluated_signals())
register_callbacks(app)


if __name__ == "__main__":
    app.run(debug=True)

