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
