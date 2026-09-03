r"""
backtest_app.py -- live interactive explorer for the unified evaluation
framework's results. Reads registry + run artifacts, and for signals with a
recognized TradeRule shape, re-runs evaluation.trades.simulate() live as the
user drags threshold sliders -- no new backtest math, everything here
delegates to evaluation/ and generate_eval_report's existing loaders.

See docs/superpowers/specs/2026-08-03-interactive-backtest-explorer-design.md.
W4 extension (2026-08-23): trade-rule REGISTRATION API, cost-sensitivity
sweep, trade-level drill-down table, walk-forward split inspector, live
robustness diagnostics, and a 2D/3D parameter-surface toggle. Standing
constraint unchanged: DIAGNOSIS ONLY -- nothing here writes to the registry
or feeds the pre-registered campaign endpoints.

Usage
-----
  C:\ProgramData\anaconda3\python.exe backtest_app.py
  (opens http://127.0.0.1:8050)
"""

import dataclasses

import dash
from dash import dcc, html, Input, Output, State, dash_table
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from evaluation import registry as ev_registry
from evaluation import robustness as ev_robustness
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

    sizing_mode="fixed_fraction" with limits_capital=None constructs a VALID
    ExecutionConfig -- Sizing.__post_init__ only requires `fraction` to be
    set for that mode, and PortfolioLimits.__post_init__ has no cross-field
    check against sizing.mode. That combination only fails later, inside
    evaluation/trades.py's _portfolio_pass() when simulate_live() actually
    runs the simulation. Caught here explicitly so it's rejected early and
    cheaply, before any simulation is attempted.
    """
    if sizing_mode == "fixed_fraction" and limits_capital is None:
        return None, ("Execution config error: sizing.mode='fixed_fraction' "
                      "requires limits.capital")
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


def register_trade_rule_signal(name: str, cache_builder, rule_builder) -> None:
    """Register a live-tradeable signal: name -> (cache builder, rule builder).

    This is the extension point that replaced the hardcoded allowlist. CAUTION
    before registering: the cache builder must rebuild the EXACT universe the
    signal's recorded run was evaluated on. The built-in entries share the
    default full-universe adapters.rating_cache() because tv_fade/tv_fade_long
    were evaluated on the same universe as tv_threshold; a basket-scoped or
    Russell-3000-scoped run registered against this default cache would
    silently show the wrong live data under a misleadingly-scoped dropdown
    label -- give such signals their own scoped cache builder first.
    """
    KNOWN_TRADE_RULE_SIGNALS[name] = (cache_builder, rule_builder)


register_trade_rule_signal("tv_threshold",
                           ev_adapters.rating_cache, build_tv_threshold_rule)
register_trade_rule_signal("tv_fade",
                           ev_adapters.rating_cache, build_tv_fade_rule)
register_trade_rule_signal("tv_fade_long",
                           ev_adapters.rating_cache, build_tv_fade_long_rule)


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


_SIM_CACHE: dict = {}   # (name, run_id, bull_min, exit_long_max, bear_max, exit_short_min, config_hash) -> (trades, summary)


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
    children = [dcc.Markdown(gt._headline_tiles(sheet["headline"], sheet["tail_risk"]),
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


def _parameter_grid(name: str, run_id: str) -> "tuple[np.ndarray, np.ndarray, np.ndarray]":
    """Shared sweep for the 2D heatmap and 3D surface: net P&L over a fixed
    (bull entry x exit long) grid at fixed defaults for the other two
    thresholds and LEGACY costs -- deliberately independent of the current
    sliders so the map is a stable landscape view, not an echo of them."""
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
    return bull_range, exit_range, z


def parameter_heatmap_fig(name: str, run_id: str, mode: str = "heatmap") -> go.Figure:
    """Parameter sensitivity over the shared grid, as 2D Heatmap or 3D Surface."""
    bull_range, exit_range, z = _parameter_grid(name, run_id)
    x = [round(float(b), 2) for b in bull_range]
    y = [round(float(e), 2) for e in exit_range]
    title = ("Parameter Sensitivity: Bull Entry vs Exit Long P&L ($) "
             "(legacy costs -- ignores Execution Config panel)")
    if mode == "surface":
        fig = go.Figure(data=go.Surface(z=z, x=x, y=y, colorscale="Viridis"))
        fig.update_layout(title=title + " -- 3D", height=420,
                          scene=dict(xaxis_title="Bull Entry",
                                     yaxis_title="Exit Long",
                                     zaxis_title="Net P&L ($)"))
        return fig
    fig = go.Figure(data=go.Heatmap(z=z, x=x, y=y, colorscale="Viridis"))
    fig.update_layout(title=title, xaxis_title="Bull Entry Threshold",
                      yaxis_title="Exit Long Threshold", height=340)
    return fig


#: Commission levels (bps) swept by cost_sensitivity_fig/cvar_sensitivity_fig.
#: Small on purpose: each point re-simulates the full universe.
COST_SWEEP_BPS = (0, 5, 10, 20, 30, 40, 50)


def cost_sensitivity_fig(name: str, run_id: str, bull_min: float,
                         exit_long_max: float, bear_max: float,
                         exit_short_min: float,
                         config: "ev_execution.ExecutionConfig") -> "go.Figure | None":
    """Net P&L and trade count vs commission level, holding the CURRENT
    thresholds and every other config field fixed. Diagnosis only: shows how
    much of the edge survives hypothetical cost levels; it never writes to
    the registry or feeds any campaign endpoint."""
    pnls, counts = [], []
    for bps in COST_SWEEP_BPS:
        swept = dataclasses.replace(
            config, costs=dataclasses.replace(config.costs, commission_bps=float(bps)))
        try:
            _, summary = simulate_live(name, run_id, bull_min, exit_long_max,
                                       bear_max, exit_short_min, config=swept)
        except ValueError:
            return None
        pnls.append(summary.get("total_pnl_dollars"))
        counts.append(summary.get("n_trades", 0))
    if all(p is None for p in pnls):
        return None
    fig = go.Figure()
    fig.add_scatter(x=list(COST_SWEEP_BPS), y=pnls, mode="lines+markers",
                    name="net P&L ($)", line=dict(color=SLOT0),
                    yaxis="y")
    fig.add_scatter(x=list(COST_SWEEP_BPS), y=counts, mode="lines+markers",
                    name="n trades", line=dict(color="#888888", dash="dot"),
                    yaxis="y2")
    fig.update_layout(
        title="Cost sensitivity at current thresholds (diagnostic)",
        xaxis_title="Commission (bps)", height=320,
        yaxis=dict(title="Net P&L ($)"),
        yaxis2=dict(title="N trades", overlaying="y", side="right"),
        legend=dict(orientation="h"))
    return fig


def cvar_sensitivity_fig(name: str, run_id: str, bull_min: float,
                         exit_long_max: float, bear_max: float,
                         exit_short_min: float,
                         config: "ev_execution.ExecutionConfig") -> "go.Figure | None":
    """CVaR (95%) of the daily-return series vs commission level, the same
    sweep as cost_sensitivity_fig but showing how TAIL RISK degrades as
    costs rise -- a strategy whose net P&L survives higher costs can still
    see its worst-day tail get materially worse, which net P&L alone
    hides. Reuses tearsheet.tail_risk_metrics() (the same CVaR convention
    already shown in the live tearsheet's headline tile) rather than a
    third calculation of the same statistic; bridges trades -> daily
    returns via tearsheet.daily_returns_from_trades(), matching how the
    live tearsheet itself gets there."""
    cvars, n_trades = [], []
    for bps in COST_SWEEP_BPS:
        swept = dataclasses.replace(
            config, costs=dataclasses.replace(config.costs, commission_bps=float(bps)))
        try:
            trades, summary = simulate_live(name, run_id, bull_min, exit_long_max,
                                            bear_max, exit_short_min, config=swept)
        except ValueError:
            return None
        bridged = ev_tearsheet.daily_returns_from_trades(trades)
        tail = (ev_tearsheet.tail_risk_metrics(bridged["returns"])
               if bridged["returns"] is not None else {"cvar_pct": None})
        cvars.append(tail.get("cvar_pct"))
        n_trades.append(summary.get("n_trades", 0))
    if all(c is None for c in cvars):
        return None
    fig = go.Figure()
    fig.add_scatter(x=list(COST_SWEEP_BPS), y=cvars, mode="lines+markers",
                    name="CVaR 95% (%)", line=dict(color=COLOR_CRITICAL),
                    yaxis="y")
    fig.add_scatter(x=list(COST_SWEEP_BPS), y=n_trades, mode="lines+markers",
                    name="n trades", line=dict(color="#888888", dash="dot"),
                    yaxis="y2")
    fig.update_layout(
        title="Tail-risk sensitivity at current thresholds (diagnostic)",
        xaxis_title="Commission (bps)", height=320,
        yaxis=dict(title="CVaR 95% (%, worse = higher)"),
        yaxis2=dict(title="N trades", overlaying="y", side="right"),
        legend=dict(orientation="h"))
    return fig


_TRADE_TABLE_COLUMNS = [
    ("symbol", "symbol"), ("side", "side"), ("entry_date", "entry"),
    ("exit_date", "exit"), ("entry_price", "entry px"),
    ("exit_price", "exit px"), ("pnl_dollars", "P&L $"),
    ("pnl_pct", "P&L %"), ("exit_reason", "exit reason"),
]


def trades_table(trades_df: "pd.DataFrame | None") -> "html.Div":
    """Trade-level drill-down: sortable/filterable table of the live trades
    under the current thresholds + execution config."""
    cols = [c for c, _ in _TRADE_TABLE_COLUMNS if trades_df is not None
            and c in trades_df.columns]
    if trades_df is None or trades_df.empty or not cols:
        return html.Div("no realized trades at this threshold",
                        style={"color": "#666"})
    shown = trades_df[cols].copy()
    table = dash_table.DataTable(
        columns=[{"name": label, "id": c} for c, label in _TRADE_TABLE_COLUMNS
                 if c in cols],
        data=shown.to_dict("records"),
        sort_action="native", filter_action="native",
        page_size=15, page_action="native",
        style_table={"overflowX": "auto", "maxHeight": "420px",
                     "overflowY": "auto"},
        style_cell={"fontSize": 12, "padding": "4px",
                    "whiteSpace": "nowrap"},
        style_data_conditional=[
            {"if": {"filter_query": "{pnl_dollars} > 0",
                    "column_id": "pnl_dollars"},
             "color": COLOR_GOOD},
            {"if": {"filter_query": "{pnl_dollars} <= 0",
                    "column_id": "pnl_dollars"},
             "color": COLOR_CRITICAL},
        ])
    return html.Div(table)


def walk_forward_children(results: dict) -> "list":
    """Walk-forward split inspector: per-fold OOS IC from the run's tier3
    artifacts. Only non-trade-rule runs carry these -- trade-rule-only runs
    have no IC panel to split."""
    wf = ((results.get("tier3") or {}).get("walk_forward") or {})
    folds = wf.get("folds") or []
    if not folds:
        return [html.Div("no walk-forward artifacts for this signal "
                         "(tier3.walk_forward absent -- trade-rule-only "
                         "runs do not carry it)", style={"color": "#666"})]
    oos = wf.get("oos") or {}
    header = html.Div(
        f'OOS mean daily IC {oos.get("mean_daily_ic")} '
        f'(t={oos.get("ic_t_stat")}) over {len(folds)} folds '
        f'(train days min {wf.get("n_train_days")})')
    fig = go.Figure(go.Bar(
        x=[f"f{f['fold']}" for f in folds],
        y=[f.get("mean_daily_ic") for f in folds],
        text=[f.get("date_range") for f in folds],
        hovertemplate="%{text}<br>IC %{y}<extra></extra>"))
    fig.add_hline(y=0, line_width=1)
    fig.update_layout(title="Walk-forward OOS daily IC by fold", height=280,
                      showlegend=False)
    return [header, dcc.Graph(figure=fig)]


#: Trial counts for the live robustness panel -- deliberately below the
#: campaign defaults (100/200/1000): this is an interactive diagnostic, not a
#: pre-registered endpoint, and the panel must answer inside a coffee sip.
ROBUSTNESS_TRIALS = {"noise": 50, "mcpt": 100, "order": 500}


def robustness_children(name: str, run_id: str, bull_min: float,
                        exit_long_max: float, bear_max: float,
                        exit_short_min: float,
                        config: "ev_execution.ExecutionConfig",
                        trades: "pd.DataFrame | None") -> "list":
    """Live W2 diagnostics for the CURRENT rule + config: price-level noise
    test, price-path MCPT p-value, and trade-order drawdown MC."""
    rule_builder = KNOWN_TRADE_RULE_SIGNALS[name][1]
    rule = rule_builder(bull_min, exit_long_max, bear_max, exit_short_min,
                        DEFAULT_NOTIONAL)
    cache = get_cache(name, run_id)
    cards = []

    noise = ev_robustness.noise_test(rule, cache, n_trials=ROBUSTNESS_TRIALS["noise"],
                                     config=config)
    if noise.get("noise_pct_profitable") is not None:
        cards += [
            (f"noise: observed ${noise['observed_pnl_dollars']:,.0f}",
             f"{noise['noise_pct_profitable']}% of perturbed trials profitable "
             f"(median ${noise['noise_median_pnl_dollars']:,.0f}, "
             f"p95 ${noise['noise_p95_pnl_dollars']:,.0f})")]
    else:
        cards.append(("noise: n/a", noise.get("noise_reason", "unavailable")))

    mcpt = ev_robustness.price_mcpt(rule, cache, n_perm=ROBUSTNESS_TRIALS["mcpt"],
                                    config=config)
    if mcpt.get("price_mcpt_p") is not None:
        cards.append((f"price MCPT p = {mcpt['price_mcpt_p']}",
                      f"observed ${mcpt['observed_pnl_dollars']:,.0f} vs "
                      f"{mcpt['n_perm']} shuffled-return permutations"))
    else:
        cards.append(("price MCPT: n/a", "not enough usable symbols"))

    order = ev_robustness.trade_order_mc(trades, n_trials=ROBUSTNESS_TRIALS["order"]) \
        if trades is not None and not trades.empty else {}
    if order:
        cards.append((
            f"trade-order MDD: observed {order['observed_mdd_pct']}% "
            f"({order['observed_mdd_percentile']}th pct)",
            f"shuffled-order median {order['mdd_median_pct']}%, "
            f"worst {order['mdd_worst_pct']}% over "
            f"{order['n_trials']} trials"))
    else:
        cards.append(("trade-order MC: n/a", "no realized trades"))

    children = [html.Div([
        html.Div([
            html.Div(head, style={"fontWeight": "bold"}),
            html.Div(sub, style={"fontSize": "12px", "color": "#666"}),
        ], style={"border": "1px solid #ddd", "borderRadius": "4px",
                  "padding": "8px 12px", "minWidth": "260px",
                  "backgroundColor": "#f9f9f9"})
        for head, sub in cards
    ], style={"display": "flex", "flexWrap": "wrap", "gap": "10px"})]
    children.append(html.Div(
        "Diagnosis only -- trial counts are reduced for interactivity; "
        "these numbers never feed the registry or campaign endpoints.",
        style={"fontSize": "11px", "color": "#999", "marginTop": "4px"}))
    return children


from generate_eval_report import _ic_by_horizon, _spread_with_ci, _regimes, _trades_fig

SLIDER_MIN, SLIDER_MAX, SLIDER_STEP = -1.0, 1.0, 0.05


def _slider(id_, value):
    return dcc.Slider(id=id_, min=SLIDER_MIN, max=SLIDER_MAX, step=SLIDER_STEP,
                      value=value, updatemode="mouseup",
                      marks={-1: "-1", 0: "0", 1: "1"})


def _ranged_slider(id_, value, min_, max_, step):
    return dcc.Slider(id=id_, min=min_, max=max_, step=step, value=value,
                      updatemode="mouseup")


def _execution_config_panel() -> "html.Div":
    return html.Div([
        html.H4("Execution config"),
        html.Div([
            html.H5("Costs"),
            html.Div("Commission (bps)"),
            _ranged_slider("commission-bps", 0.0, 0.0, 50.0, 0.5),
            html.Div("Spread (bps)"),
            _ranged_slider("spread-bps", 0.0, 0.0, 50.0, 0.5),
            html.Div("Borrow fee (bps) (no effect on this engine)"),
            _ranged_slider("borrow-fee-bps", 0.0, 0.0, 50.0, 0.5),
            # "sqrt" impact is turnover-based and only used by the (unused
            # here) weight-matrix engine's daily cost function -- inert on
            # the discrete-trade engine this live app actually simulates
            # against. "flat" DOES apply here via round_trip_rate().
            html.Div("Impact model"),
            dcc.Dropdown(id="impact-model", clearable=False,
                        options=[{"label": "none", "value": "none"},
                                 {"label": "sqrt (no effect here)", "value": "sqrt"},
                                 {"label": "flat", "value": "flat"}],
                        value="none"),
            html.Div("Impact coeff (no effect when impact model = sqrt)"),
            _ranged_slider("impact-coeff", 0.0, 0.0, 50.0, 0.5),
        ]),
        html.Div([
            html.H5("Risk"),
            html.Div("Stop loss %"),
            dcc.Input(id="stop-loss-pct", type="number", value=None),
            html.Div("Take profit %"),
            dcc.Input(id="take-profit-pct", type="number", value=None),
            html.Div("Vol stop mult"),
            dcc.Input(id="vol-stop-mult", type="number", value=None),
            dcc.Checklist(id="trailing",
                         options=[{"label": "Trailing stop", "value": "trailing"}],
                         value=[]),
            html.Div("Max holding days"),
            dcc.Input(id="max-holding-days", type="number", value=None),
        ]),
        html.Div([
            html.H5("Sizing"),
            html.Div("Mode"),
            dcc.Dropdown(id="sizing-mode", clearable=False,
                        options=[{"label": "fixed notional", "value": "fixed_notional"},
                                 {"label": "fixed fraction", "value": "fixed_fraction"}],
                        value="fixed_notional"),
            html.Div("Notional"),
            dcc.Input(id="sizing-notional", type="number", value=DEFAULT_NOTIONAL),
            html.Div("Fraction"),
            dcc.Input(id="sizing-fraction", type="number", value=None),
            html.Div("Max weight"),
            dcc.Input(id="sizing-max-weight", type="number", value=None),
        ]),
        html.Div([
            html.H5("Limits"),
            html.Div("Capital"),
            dcc.Input(id="limits-capital", type="number", value=None),
            html.Div("Max concurrent"),
            dcc.Input(id="limits-max-concurrent", type="number", value=None),
            html.Div("Max drawdown stop (no effect on this engine)"),
            dcc.Input(id="limits-max-drawdown-stop", type="number", value=None),
        ]),
        html.Div(id="execution-config-error", style={"color": "#d03b3b"}),
    ])


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
        _execution_config_panel(),
        html.H4("Cost sensitivity"),
        dcc.Loading(dcc.Graph(id="cost-fig")),
        dcc.Loading(dcc.Graph(id="cvar-fig")),
        html.H4("Trades"),
        dcc.Loading(html.Div(id="trades-table-container")),
        html.Div([
            dcc.Dropdown(id="symbol-dropdown", placeholder="select a symbol"),
            dcc.Loading(dcc.Graph(id="symbol-fig")),
        ]),
        dcc.Loading(dcc.Graph(id="pnl-fig")),
        html.H4("Parameter surface"),
        dcc.RadioItems(id="param-mode",
                       options=[{"label": "2D heatmap", "value": "heatmap"},
                                {"label": "3D surface", "value": "surface"}],
                       value="heatmap", inline=True),
        dcc.Loading(dcc.Graph(id="param-fig")),
        html.H4("Walk-forward splits"),
        dcc.Loading(html.Div(id="wf-panel")),
        html.H4("Robustness (diagnostic)"),
        html.Button("Run robustness on current rule", id="robustness-button",
                    n_clicks=0),
        dcc.Loading(html.Div(id="robustness-panel")),
        html.H4("Tearsheet"),
        dcc.Loading(html.Div(id="tearsheet-container")),
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
        Output("wf-panel", "children"),
        Input("signal-store", "data"))
    def _on_walk_forward(store):
        if not store:
            return []
        return walk_forward_children(store.get("results") or {})

    @app.callback(
        Output("param-fig", "figure"),
        Input("signal-store", "data"), Input("param-mode", "value"))
    def _on_param_mode(store, mode):
        empty_fig = go.Figure()
        if not store:
            return empty_fig
        if not has_trade_rule(store["name"]):
            return empty_fig
        try:
            return parameter_heatmap_fig(store["name"], store["run_id"],
                                         mode=mode or "heatmap")
        except Exception:
            return empty_fig

    @app.callback(
        Output("robustness-panel", "children"),
        Input("robustness-button", "n_clicks"),
        State("signal-store", "data"),
        State("bull-min", "value"), State("exit-long-max", "value"),
        State("bear-max", "value"), State("exit-short-min", "value"),
        State("commission-bps", "value"), State("spread-bps", "value"),
        State("borrow-fee-bps", "value"), State("impact-model", "value"),
        State("impact-coeff", "value"), State("stop-loss-pct", "value"),
        State("take-profit-pct", "value"), State("vol-stop-mult", "value"),
        State("trailing", "value"), State("max-holding-days", "value"),
        State("sizing-mode", "value"), State("sizing-notional", "value"),
        State("sizing-fraction", "value"), State("sizing-max-weight", "value"),
        State("limits-capital", "value"),
        State("limits-max-concurrent", "value"),
        State("limits-max-drawdown-stop", "value"),
        prevent_initial_call=True)
    def _on_robustness(n_clicks, store, bull_min, exit_long_max, bear_max,
                       exit_short_min, commission_bps, spread_bps,
                       borrow_fee_bps, impact_model, impact_coeff,
                       stop_loss_pct, take_profit_pct, vol_stop_mult,
                       trailing, max_holding_days, sizing_mode,
                       sizing_notional, sizing_fraction, sizing_max_weight,
                       limits_capital, limits_max_concurrent,
                       limits_max_drawdown_stop):
        if not n_clicks or not store:
            return []
        if not has_trade_rule(store["name"]):
            return [html.Div("no trade rule defined for this signal",
                             style={"color": "#666"})]
        cfg, cfg_error = resolve_execution_config(
            commission_bps=commission_bps, spread_bps=spread_bps,
            borrow_fee_bps=borrow_fee_bps, impact_model=impact_model,
            impact_coeff=impact_coeff, stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct, vol_stop_mult=vol_stop_mult,
            trailing=trailing, max_holding_days=max_holding_days,
            sizing_mode=sizing_mode, sizing_notional=sizing_notional,
            sizing_fraction=sizing_fraction, sizing_max_weight=sizing_max_weight,
            limits_capital=limits_capital,
            limits_max_concurrent=limits_max_concurrent,
            limits_max_drawdown_stop=limits_max_drawdown_stop)
        if cfg is None:
            return [html.Div(cfg_error, style={"color": COLOR_CRITICAL})]
        try:
            trades, _ = simulate_live(store["name"], store["run_id"], bull_min,
                                      exit_long_max, bear_max, exit_short_min,
                                      config=cfg)
        except ValueError as exc:
            return [html.Div(f"Execution config error: {exc}",
                             style={"color": COLOR_CRITICAL})]
        return robustness_children(store["name"], store["run_id"], bull_min,
                                   exit_long_max, bear_max, exit_short_min,
                                   cfg, trades)

    @app.callback(
        Output("trade-summary", "children"), Output("symbol-fig", "figure"),
        Output("pnl-fig", "figure"), Output("cost-fig", "figure"),
        Output("cvar-fig", "figure"),
        Output("execution-config-error", "children"),
        Output("tearsheet-container", "children"),
        Output("trades-table-container", "children"),
        Input("signal-store", "data"), Input("bull-min", "value"),
        Input("exit-long-max", "value"), Input("bear-max", "value"),
        Input("exit-short-min", "value"), Input("symbol-dropdown", "value"),
        Input("commission-bps", "value"), Input("spread-bps", "value"),
        Input("borrow-fee-bps", "value"), Input("impact-model", "value"),
        Input("impact-coeff", "value"), Input("stop-loss-pct", "value"),
        Input("take-profit-pct", "value"), Input("vol-stop-mult", "value"),
        Input("trailing", "value"), Input("max-holding-days", "value"),
        Input("sizing-mode", "value"), Input("sizing-notional", "value"),
        Input("sizing-fraction", "value"), Input("sizing-max-weight", "value"),
        Input("limits-capital", "value"), Input("limits-max-concurrent", "value"),
        Input("limits-max-drawdown-stop", "value"))
    def _on_sliders_change(store, bull_min, exit_long_max, bear_max,
                          exit_short_min, symbol, commission_bps, spread_bps,
                          borrow_fee_bps, impact_model, impact_coeff,
                          stop_loss_pct, take_profit_pct, vol_stop_mult,
                          trailing, max_holding_days, sizing_mode,
                          sizing_notional, sizing_fraction, sizing_max_weight,
                          limits_capital, limits_max_concurrent,
                          limits_max_drawdown_stop):
        empty_fig = go.Figure()
        if not store:
            return ("select a signal", empty_fig, empty_fig, empty_fig,
                    empty_fig, "", [], html.Div(""))
        if not has_trade_rule(store["name"]):
            return ("no trade rule defined for this signal", empty_fig,
                    empty_fig, empty_fig, empty_fig, "", [], html.Div(""))

        cfg, cfg_error = resolve_execution_config(
            commission_bps=commission_bps, spread_bps=spread_bps,
            borrow_fee_bps=borrow_fee_bps, impact_model=impact_model,
            impact_coeff=impact_coeff, stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct, vol_stop_mult=vol_stop_mult,
            trailing=trailing, max_holding_days=max_holding_days,
            sizing_mode=sizing_mode, sizing_notional=sizing_notional,
            sizing_fraction=sizing_fraction, sizing_max_weight=sizing_max_weight,
            limits_capital=limits_capital,
            limits_max_concurrent=limits_max_concurrent,
            limits_max_drawdown_stop=limits_max_drawdown_stop)
        if cfg is None:
            return (dash.no_update, dash.no_update, dash.no_update,
                    dash.no_update, dash.no_update, cfg_error,
                    dash.no_update, dash.no_update)

        try:
            trades, summary = simulate_live(store["name"], store["run_id"], bull_min,
                                            exit_long_max, bear_max, exit_short_min,
                                            config=cfg)
        except ValueError as exc:
            return (dash.no_update, dash.no_update, dash.no_update,
                    dash.no_update, dash.no_update,
                    f"Execution config error: {exc}",
                    dash.no_update, dash.no_update)
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
        cost_fig = (cost_sensitivity_fig(store["name"], store["run_id"],
                                         bull_min, exit_long_max, bear_max,
                                         exit_short_min, cfg) or empty_fig)
        cvar_fig = (cvar_sensitivity_fig(store["name"], store["run_id"],
                                         bull_min, exit_long_max, bear_max,
                                         exit_short_min, cfg) or empty_fig)
        tearsheet_children = render_tearsheet(live_tearsheet(trades))
        table_children = trades_table(trades)
        return (text, sym_fig, pnl_fig, cost_fig, cvar_fig, "",
                tearsheet_children, table_children)


app = dash.Dash(__name__)
app.layout = build_layout(list_evaluated_signals())
register_callbacks(app)


if __name__ == "__main__":
    app.run(debug=True)

