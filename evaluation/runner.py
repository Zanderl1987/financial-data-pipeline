"""
evaluation/runner.py -- dispatch an input contract to its evaluations,
write artifacts (results.json / run_meta.json / parquet), append registry
rows. THE place lag_days is applied (via data.apply_lag, exactly once);
evaluators downstream never shift dates.
"""

import json
import os
import subprocess
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from evaluation import data as ev_data
from evaluation import events as ev_events
from evaluation import ic as ev_ic
from evaluation import portfolio as ev_portfolio
from evaluation import registry as ev_registry
from evaluation import stats as ev_stats
from evaluation import trades as ev_trades
from evaluation.contracts import EventSet, Signal, TradeRule


def _git_commit() -> str:
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       cwd=root, text=True).strip()
    except Exception:
        return "unknown"


def _json_safe(v):
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    if isinstance(v, (pd.Timestamp, datetime)):
        return str(v)
    if isinstance(v, pd.Series):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, pd.DataFrame):
        return v.to_dict("records")
    if isinstance(v, float) and not np.isfinite(v):
        return None
    return v


# Sample-size / count / flag leaves produced across the _stat_rows call
# sites in this module (ic_res per-horizon dicts, block_bootstrap_spread,
# walk_forward's oos dict, regime_conditioning's per-regime dicts,
# bootstrap_sharpe, deflated_sharpe, registry_percentile, the events-loop
# rowdict, and the trades summary/perm dicts). These are metadata about
# HOW a statistic was computed, not measured signal quality -- registering
# them as first-class "statistic" rows would let compare()/baselines() diff
# them run-over-run as if they were real regressions. ic_pct_positive is a
# genuine measured statistic (share of days with positive IC) and stays in.
_METADATA_KEYS = {
    "n", "ic_days", "top_n", "bottom_n", "oriented",
    "n_boot", "boot_days", "n_days", "n_trials", "n_population", "n_perm",
    "n_trades", "n_long", "n_short", "n_symbols", "n_scored", "n_kept",
}


def _stat_rows(evaluation: str, horizon: int, d: dict, n_key=None) -> list:
    """One registry row per numeric (non-bool) leaf of a flat result dict,
    excluding sample-size/count/flag metadata keys (see _METADATA_KEYS)."""
    n = 0
    if n_key is not None and d.get(n_key) is not None:
        n = int(d[n_key])
    rows = []
    for k, v in d.items():
        if k in _METADATA_KEYS:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float, np.floating,
                                                     np.integer)):
            continue
        rows.append({"evaluation": evaluation, "horizon": int(horizon),
                     "statistic": str(k), "value": float(v), "n": n})
    return rows


def _meta_label_result(trades_df: pd.DataFrame, cache: dict, threshold: float,
                       min_train: int, refit_every: int, l2: float):
    """
    Meta-labeling (evaluation/meta_label.py) on top of an already-simulated
    TradeRule: build features at each trade's entry_signal_date, score them
    out-of-sample walk-forward, and compare the meta-filtered subset against
    the unfiltered set. Registered under its own "meta_filtered"/
    "meta_unfiltered" evaluation names (never blended into "trades") so a
    meta-labeled strategy gets the same baseline/compare() treatment as
    every other registered result, not a side calculation invisible to the
    registry.

    Returns (result_dict_for_results.json, registry_rows). Best-effort, like
    the Signal path's portfolio evaluation: a meta-labeling failure (e.g.
    every training window single-class) is reported via *_reason, never
    fatal to the rest of the run.
    """
    from evaluation import meta_label as ev_meta

    feats = ev_meta.build_features(trades_df, cache)
    scored = ev_meta.walk_forward_meta_labels(trades_df, feats, min_train=min_train,
                                               refit_every=refit_every, l2=l2)
    result = ev_meta.evaluate_meta_filter(scored, threshold=threshold)
    if "meta_reason" in result:
        return result, []
    rows = (_stat_rows("meta_unfiltered", -1, result["unfiltered"], n_key="n_trades")
            + _stat_rows("meta_filtered", -1, result["filtered"], n_key="n_trades")
            + _stat_rows("meta_summary", -1,
                         {"kept_fraction": result["kept_fraction"],
                          "n_scored": result["n_scored"],
                          "n_kept": result["n_kept"]}, n_key="n_scored"))
    return result, rows


def _oriented(panel: pd.DataFrame, direction: int) -> pd.DataFrame:
    work = panel.copy()
    if direction == -1:
        work["value"] = -work["value"]
    return work


def _returns_series(res):
    rets = res.returns
    if isinstance(rets, pd.DataFrame):
        col = "long_short" if "long_short" in rets.columns else rets.columns[0]
        rets = rets[col]
    return pd.Series(rets).dropna()


def _run_signal(obj: Signal, universe, start, end, benchmark, price_table,
                quantiles, rebalance, long_short, n_boot, n_perm, seed,
                registry_path):
    lagged = ev_data.apply_lag(obj.frame, obj.lag_days)
    symbols = (sorted(universe) if universe
               else sorted(lagged["symbol"].unique()))
    closes = ev_data.load_closes(symbols, start=start, end=end,
                                 benchmark=benchmark, price_table=price_table)
    panel, dropped = ev_data.build_return_panel(lagged, closes,
                                                ev_data.HORIZONS, benchmark)
    results = {}
    rows = []
    if panel.empty:
        return results, rows, panel, dropped, symbols

    # Tier 1: pooled/daily IC + cross-sectional bucket spread, per horizon
    ic_res = ev_ic.evaluate_ic(panel, direction=obj.direction)
    results["ic"] = ic_res
    for h, d in ic_res.items():
        rows += _stat_rows("ic", h, d, n_key="n")

    work = _oriented(panel, obj.direction)

    # Tier 2: date-block bootstrap CI on the bucket spread, per horizon
    tier2 = {}
    for h in ev_data.HORIZONS:
        fcol = f"fwd_{h}d"
        if fcol not in work.columns:
            continue
        boot = ev_stats.block_bootstrap_spread(work, "value", fcol,
                                               n_boot=n_boot, seed=seed)
        tier2[h] = boot
        rows += _stat_rows("ic_boot", h, boot, n_key="boot_days")
    results["tier2"] = tier2

    # Tier 3: walk-forward OOS + regime conditioning on the 5d horizon
    tier3 = {}
    ref_col = "fwd_5d" if "fwd_5d" in work.columns else None
    if ref_col is not None:
        tier3["walk_forward"] = ev_stats.walk_forward(work, "value", ref_col)
        oos = tier3["walk_forward"].get("oos")
        if isinstance(oos, dict):
            rows += _stat_rows("tier3_wf_oos", 5, oos, n_key="ic_days")
        if benchmark and benchmark in closes.columns:
            tier3["regimes"] = ev_stats.regime_conditioning(
                work, "value", ref_col, closes[benchmark].dropna())
            for regime, d in tier3["regimes"].items():
                if isinstance(d, dict):
                    rows += _stat_rows(f"tier3_regime_{regime}", 5, d,
                                       n_key="n_days")

    # Quantile portfolio (wraps backtest.backtest) + Sharpe bootstrap + DSR
    portfolio = None
    try:
        res = ev_portfolio.evaluate_portfolio(
            lagged, direction=obj.direction, quantiles=quantiles,
            rebalance=rebalance, long_short=long_short, start=start, end=end,
            price_table=price_table)
        portfolio = ev_portfolio.summarize_portfolio(res)
        rets = _returns_series(res)
        boot_sharpe = ev_stats.bootstrap_sharpe(rets, n_boot=n_boot, seed=seed)
        portfolio["sharpe_bootstrap"] = boot_sharpe
        rows += _stat_rows("portfolio_boot", -1, boot_sharpe, n_key="n_boot")
        sharpe_now = boot_sharpe.get("sharpe")
        if sharpe_now is not None:
            trials = ev_registry.population("sharpe", path=registry_path,
                                            exclude_input_name=obj.name)
            trials = trials + [sharpe_now]
            dsr = ev_stats.deflated_sharpe(sharpe_now, len(rets), trials)
            pct = ev_stats.registry_percentile(sharpe_now, trials)
            tier3["deflated_sharpe"] = dsr
            tier3["registry_percentile"] = pct
            rows += _stat_rows("tier3", -1, dsr, n_key="n_trials")
            rows += _stat_rows("tier3", -1, pct, n_key="n_population")
    except Exception as exc:  # portfolio eval is best-effort, never fatal
        portfolio = {"portfolio_reason": f"{type(exc).__name__}: {exc}"}
    results["portfolio"] = portfolio
    results["tier3"] = tier3

    # BH-FDR across every p-value this run produced
    records = []
    for h, d in ic_res.items():
        for stat, p in (("pooled_p", d.get("pooled_p")),
                        ("spread_p", d.get("spread_p"))):
            records.append({"evaluation": "ic", "horizon": h,
                            "statistic": stat, "p": p})
        t = d.get("ic_t_stat")
        records.append({"evaluation": "ic", "horizon": h,
                        "statistic": "daily_ic_p",
                        "p": ev_stats.t_to_p(t) if t is not None else None})
    if records:
        fdr = ev_stats.bh_fdr(pd.DataFrame(records))
        results["fdr"] = fdr.to_dict("records")

    return results, rows, panel, dropped, symbols


def run(obj, universe=None, start=None, end=None, benchmark="SPY",
        price_table=None, quantiles=5, rebalance="M", long_short=True,
        out_root=os.path.join("storage", "reports", "eval"),
        registry_path=None, write_registry=True,
        n_boot=1000, n_perm=200, seed=0, cache=None,
        meta_label=False, meta_threshold=0.5, meta_min_train=50,
        meta_refit_every=20, meta_l2=1.0) -> dict:
    registry_path = registry_path or ev_registry.REG_PATH
    panel = trades_df = None
    dropped = {}

    if isinstance(obj, Signal):
        input_type = "signal"
        results, rows, panel, dropped, symbols = _run_signal(
            obj, universe, start, end, benchmark, price_table, quantiles,
            rebalance, long_short, n_boot, n_perm, seed, registry_path)
    elif isinstance(obj, EventSet):
        input_type = "event_set"
        lagged = ev_data.apply_lag(obj.frame, obj.lag_days)
        symbols = (sorted(universe) if universe
                   else sorted(lagged["symbol"].unique()))
        ev_res = ev_events.evaluate_events(
            lagged, min_events=obj.min_events, benchmark=benchmark,
            window=(0, 21), entry_lag=1, price_table=price_table)
        results = {"events": ev_res}
        rows = []
        for label, d in ev_res.get("labels", {}).items():
            for h, rowdict in d.get("horizons", {}).items():
                rows += _stat_rows(f"events:{label}", int(h), rowdict,
                                   n_key="n")
    elif isinstance(obj, TradeRule):
        input_type = "trade_rule"
        if cache is None:
            raise ValueError(
                "TradeRule evaluation needs cache={symbol: DataFrame} -- "
                "pass the per-symbol frames the rule's callables read")
        symbols = sorted(cache.keys())
        trades_df = ev_trades.simulate(obj, cache)
        summary = ev_trades.trade_summary(trades_df)
        perm = ev_stats.permutation_trades(obj, cache, n_perm=n_perm,
                                           seed=seed)
        results = {"summary": summary, "permutation": perm}
        rows = (_stat_rows("trades", -1, summary, n_key="n_trades")
                + _stat_rows("trades_perm", -1, perm, n_key="n_perm"))
        if meta_label:
            try:
                meta_result, meta_rows = _meta_label_result(
                    trades_df, cache, meta_threshold, meta_min_train,
                    meta_refit_every, meta_l2)
            except Exception as exc:   # best-effort, never fatal to the run
                meta_result, meta_rows = (
                    {"meta_reason": f"{type(exc).__name__}: {exc}"}, [])
            results["meta_filtered"] = meta_result
            rows += meta_rows
    else:
        raise TypeError(f"cannot evaluate object of type {type(obj).__name__}"
                        " -- expected Signal, EventSet, or TradeRule")

    # --- artifacts -------------------------------------------------------
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(out_root, f"{obj.name}_{ts}")
    os.makedirs(out_dir, exist_ok=True)

    if input_type in ("signal", "event_set"):
        d = pd.to_datetime(obj.frame["date"])
        date_range = f"{d.min():%Y-%m-%d}..{d.max():%Y-%m-%d}"
    elif trades_df is not None and not trades_df.empty:
        date_range = (f"{trades_df['entry_date'].min():%Y-%m-%d}.."
                      f"{trades_df['exit_date'].max():%Y-%m-%d}")
    else:
        date_range = ".."
    uhash = ev_registry.universe_hash(symbols)
    run_id = ev_registry.new_run_id()
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    n_evaluations = sum(1 for v in results.values() if v)
    meta = {"run_id": run_id, "input_name": obj.name,
            "input_type": input_type, "created_at": created_at,
            "git_commit": _git_commit(), "universe": list(symbols),
            "universe_hash": uhash, "date_range": date_range,
            "dropped": dropped, "n_evaluations": n_evaluations,
            "params": {"lag_days": getattr(obj, "lag_days", None),
                       "direction": getattr(obj, "direction", None),
                       "benchmark": benchmark, "price_table": price_table,
                       "quantiles": quantiles, "rebalance": rebalance,
                       "long_short": long_short, "n_boot": n_boot,
                       "n_perm": n_perm, "seed": seed,
                       "start": start, "end": end,
                       "meta_label": meta_label,
                       "meta_threshold": meta_threshold if meta_label else None}}
    with open(os.path.join(out_dir, "run_meta.json"), "w",
              encoding="utf-8") as fh:
        json.dump(_json_safe(meta), fh, indent=2, default=str)
    with open(os.path.join(out_dir, "results.json"), "w",
              encoding="utf-8") as fh:
        json.dump(_json_safe(results), fh, indent=2, default=str)
    if panel is not None and not panel.empty:
        panel.to_parquet(os.path.join(out_dir, "panel.parquet"), index=False)
    if trades_df is not None:
        trades_df.to_parquet(os.path.join(out_dir, "trades.parquet"),
                             index=False)

    # --- registry --------------------------------------------------------
    rows_written = 0
    if write_registry and rows:
        frame = pd.DataFrame(rows)
        frame["run_id"] = run_id
        frame["input_name"] = obj.name
        frame["input_type"] = input_type
        frame["universe_hash"] = uhash
        frame["date_range"] = date_range
        frame["created_at"] = created_at
        rows_written = ev_registry.append(frame, path=registry_path)

    return {"name": obj.name, "input_type": input_type, "run_id": run_id,
            "out_dir": out_dir, "n_evaluations": n_evaluations,
            "results": results, "rows_written": rows_written}
