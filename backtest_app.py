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
