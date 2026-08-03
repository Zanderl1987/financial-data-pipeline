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

_CACHE: dict = {}   # signal name -> dict[symbol -> DataFrame], built lazily


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


def get_cache(name: str) -> dict:
    """Per-symbol price+rating cache for one signal, built once and reused
    (module-level, server-side -- NOT round-tripped through dcc.Store,
    which would serialize the full multi-decade panel to browser JSON on
    every slider tick)."""
    if name not in _CACHE:
        builder = KNOWN_TRADE_RULE_SIGNALS[name]     # raises KeyError if unknown
        _CACHE[name] = builder()
    return _CACHE[name]


def simulate_live(name: str, bull_min: float, exit_long_max: float,
                  bear_max: float, exit_short_min: float,
                  notional: "float | None" = None):
    """Re-run the trade simulation in-process against the cached panel --
    no disk I/O, cost bounded by in-memory panel size."""
    cache = get_cache(name)
    rule = build_tv_threshold_rule(bull_min, exit_long_max, bear_max,
                                   exit_short_min,
                                   notional or DEFAULT_NOTIONAL)
    trades = ev_trades.simulate(rule, cache)
    summary = ev_trades.trade_summary(trades)
    return trades, summary


BASELINE_DIFF_KEYS = ("n_trades", "win_rate_pct", "total_pnl_dollars")


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
