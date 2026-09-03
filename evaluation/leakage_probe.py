"""
evaluation/leakage_probe.py -- one-switch decision-timing leakage diagnostic.

A May-2026 result (arXiv:2605.23959, "When Alpha Disappears: A One-Switch
Benchmark for Decision-Time Leakage") formalizes a specific ablation: hold
the data panel, split, model, and cost convention fixed, and toggle exactly
ONE timing convention at a time (same-bar vs next-bar execution, a centered
vs trailing feature window, a PIT lag on point-in-time data) to measure how
much each individually inflates a reported Sharpe. This complements rather
than duplicates PBO/CPCV/DSR (evaluation/robustness.py, stats.py): those
ask "is this edge real given how many things were tried"; this asks "if my
number IS real, which specific protocol choice would be responsible for
some of it being fake" -- a diagnostic for THIS repo's own PIT-safety
discipline (see the signal-eval skill, sentiment_eval.py's PIT harness),
not a significance test.

one_switch_ablation() is the general primitive and enforces the discipline
programmatically: it refuses to run if the "leaky" kwargs differ from the
"safe" kwargs in more than one key, because a two-switch ablation can't
attribute the inflation to either one.

entry_lag_leakage() is the one ready-to-use concrete probe this module
ships: event_backtest.scenario() already exposes entry_lag as a real
parameter (entry_lag=1 is the safe, documented default; entry_lag=0 means
executing on the same bar the signal fired -- the exact bug this repo has
shipped twice before, per event_backtest.py's own module comments). It
reports the inflation in a per-trade risk-adjusted return (mean/std of
sc.trades["return_pct"], NOT the daily overlay equity curve -- see
_scenario_sharpe's docstring for why that curve is structurally blind to
this exact leak) from getting that one switch wrong.
"""

from __future__ import annotations

from typing import Callable


def one_switch_ablation(fn: Callable, base_kwargs: dict, switch: dict,
                        metric_fn: Callable) -> dict:
    """
    Run fn(**base_kwargs) as the SAFE case and fn(**{**base_kwargs, **switch})
    as the LEAKY case, apply metric_fn to each result, and report the
    difference. Raises ValueError if `switch` does not change exactly one
    key relative to base_kwargs -- the whole point of "one-switch" is that
    the inflation is attributable to a single, named protocol choice, not a
    bundle of changes.
    """
    changed = {k for k, v in switch.items()
              if k not in base_kwargs or base_kwargs[k] != v}
    if len(changed) != 1:
        raise ValueError(
            f"switch must change exactly one key relative to base_kwargs, "
            f"changed {len(changed)}: {sorted(changed)}")
    switch_key = next(iter(changed))

    safe_result = fn(**base_kwargs)
    leaky_result = fn(**{**base_kwargs, **switch})
    safe_metric = metric_fn(safe_result)
    leaky_metric = metric_fn(leaky_result)
    inflation = (None if safe_metric is None or leaky_metric is None
                else leaky_metric - safe_metric)
    return {"switch": switch_key,
           "safe_value": base_kwargs.get(switch_key),
           "leaky_value": switch[switch_key],
           "safe_metric": safe_metric,
           "leaky_metric": leaky_metric,
           "inflation": inflation}


def _scenario_sharpe(sc) -> "float | None":
    """
    Per-trade risk-adjusted return: mean(return_pct) / std(return_pct) across
    sc.trades. Deliberately NOT the daily overlay equity curve -- that's
    built from close-to-close pct_change() starting AT the entry bar, which
    structurally cannot see the entry bar's own same-day return (the very
    thing a same-bar-execution leak bakes into a trade's measured return),
    making it blind to exactly the leak this probe exists to catch. And
    deliberately NOT annualized: event trades are irregularly spaced and
    variable-length, so there is no single "trading day" to scale by the
    way a daily-bar Sharpe assumes -- this is a per-trade ratio, not
    comparable to tearsheet.py's daily-bar Sharpe elsewhere in this repo.
    """
    if sc.trades is None or len(sc.trades) < 5:
        return None
    r = sc.trades["return_pct"].astype(float)
    sd = float(r.std(ddof=1))
    if not sd > 1e-12:
        return None
    return float(r.mean() / sd)


def entry_lag_leakage(events, symbols=None, holding_days: int = 21,
                      price_table: "str | None" = None, **scenario_kwargs) -> dict:
    """
    One-switch probe on event_backtest.scenario()'s entry_lag: safe=1
    (next-bar execution, the documented default) vs leaky=0 (same-bar
    execution -- acting on information not yet available at the close it
    trades on). Reports the inflation in a per-trade risk-adjusted return
    (see _scenario_sharpe) from getting that one switch wrong.

    Any of scenario()'s other kwargs (cost_bps, stop_loss_pct, ...) may be
    passed through and are held fixed across both runs, consistent with
    "hold everything else fixed, toggle one switch."
    """
    import event_backtest as eb

    base_kwargs = dict(events=events, symbols=symbols, holding_days=holding_days,
                       price_table=price_table, entry_lag=1, **scenario_kwargs)
    return one_switch_ablation(eb.scenario, base_kwargs, {"entry_lag": 0},
                               _scenario_sharpe)


def _meta_lift(result: dict) -> "float | None":
    """
    Win-rate lift the meta-filter appears to add: filtered win_rate_pct
    minus unfiltered win_rate_pct. feature_centering_leakage() compares
    this between safe/leaky feature windows -- a future-peeking feature
    inflates this apparent lift by handing the classifier information it
    would not actually have at decision time, without changing a single
    underlying trade.
    """
    if "meta_reason" in result:
        return None
    unf = result.get("unfiltered") or {}
    filt = result.get("filtered") or {}
    lo, hi = unf.get("win_rate_pct"), filt.get("win_rate_pct")
    if lo is None or hi is None:
        return None
    return float(hi - lo)


def _run_meta_pipeline(trades, cache, windows, threshold, min_train,
                       refit_every, l2, centered) -> dict:
    from evaluation import meta_label as ev_meta

    feats = ev_meta.build_features(trades, cache, windows=windows,
                                   centered=centered)
    scored = ev_meta.walk_forward_meta_labels(trades, feats, min_train=min_train,
                                              refit_every=refit_every, l2=l2)
    return ev_meta.evaluate_meta_filter(scored, threshold=threshold)


def feature_centering_leakage(trades, cache, windows=(5, 10, 21),
                              threshold: float = 0.5, min_train: int = 50,
                              refit_every: int = 20, l2: float = 1.0) -> dict:
    """
    One-switch probe on meta_label.build_features()'s window convention:
    safe=trailing-only (centered=False, the documented default -- every
    feature window ends AT entry_signal_date) vs leaky=centered
    (centered=True -- each window straddles entry_signal_date, so the
    meta-model is scored using bars that had not happened yet at decision
    time). Reports the inflation in the meta-filter's apparent win-rate
    lift (_meta_lift) from getting that one switch wrong.
    """
    base_kwargs = dict(trades=trades, cache=cache, windows=windows,
                       threshold=threshold, min_train=min_train,
                       refit_every=refit_every, l2=l2, centered=False)
    return one_switch_ablation(_run_meta_pipeline, base_kwargs,
                               {"centered": True}, _meta_lift)
