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

Four concrete probes ship here, one per switch the research names:
  * entry_lag_leakage()          -- same-bar vs next-bar execution
                                     (event_backtest.scenario()).
  * feature_centering_leakage()  -- centered vs trailing feature windows
                                     (meta_label.build_features()).
  * pit_lag_fundamentals_leakage() -- period_end vs filed as the date a
                                     fundamentals fact became knowable.
  * llm_lookahead_leakage()      -- text-only vs text+outcome scoring of
                                     LLM-labeled documents (fed_sentiment).
                                     Checked BEFORE the factor goes near
                                     signal_panel(), since the score's
                                     information set is the model's, not
                                     the repo's (Phase 7 of the backtest
                                     rigor audit).

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

import numpy as np
import pandas as pd


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


_PIT_DATE_COLS = ("filed", "period_end")


def _fundamentals_growth_signal(symbols, metric: str, date_col: str,
                                form: str = "10-K"):
    """
    [symbol, date, value] YoY-growth signal from a single fundamentals_annual
    metric, dated by `date_col`. date_col='filed' (safe) uses the SEC filing
    date -- the same convention analytics.features._asof_fundamentals uses
    in production. date_col='period_end' (leaky) uses the fiscal period's
    own end date, which for an annual filing is knowable only ~1-3 months
    LATER at the real filing date -- production code never does this; it
    exists only so pit_lag_fundamentals_leakage() can compare the two.

    YoY growth (this year's value / last year's value - 1), not the raw
    level, so the signal is comparable across companies of very different
    size -- a standard earnings-surprise-proxy shape.
    """
    if date_col not in _PIT_DATE_COLS:
        raise ValueError(f"date_col must be one of {_PIT_DATE_COLS}, got {date_col!r}")
    import query as q

    df = q.load("fundamentals_annual")
    df = df[(df["metric"] == metric) & (df["form"] == form)
            & df["symbol"].isin(list(symbols)) & df[date_col].notna()]
    if df.empty:
        return pd.DataFrame(columns=["symbol", "date", "value"])
    df = (df.sort_values(["symbol", "period_end"])
            .drop_duplicates(["symbol", "period_end"], keep="last"))
    prior = df.groupby("symbol")["value"].shift(1)
    growth = df["value"] / prior - 1.0
    df = df.assign(_growth=growth.replace([np.inf, -np.inf], np.nan))
    df = df.dropna(subset=["_growth"])
    return (df[["symbol", date_col, "_growth"]]
           .rename(columns={date_col: "date", "_growth": "value"})
           .reset_index(drop=True))


def _run_fundamentals_pipeline(symbols, metric, form, horizon, start, end,
                               benchmark, price_table, date_col) -> "float | None":
    from evaluation import data as ev_data
    from evaluation import ic as ev_ic

    sig = _fundamentals_growth_signal(symbols, metric=metric, date_col=date_col,
                                      form=form)
    if sig.empty:
        return None
    closes = ev_data.load_closes(sig["symbol"].unique().tolist(), start=start,
                                 end=end, benchmark=benchmark,
                                 price_table=price_table)
    panel, _ = ev_data.build_return_panel(sig, closes, horizons=(horizon,),
                                          benchmark=benchmark)
    if panel.empty:
        return None
    ic_res = ev_ic.evaluate_ic(panel, direction=1, horizons=(horizon,))
    return ic_res.get(horizon, {}).get("pooled_ic")


def pit_lag_fundamentals_leakage(symbols, metric: str = "net_income",
                                 form: str = "10-K", horizon: int = 21,
                                 start: "str | None" = None,
                                 end: "str | None" = None,
                                 benchmark: str = "SPY",
                                 price_table: "str | None" = None) -> dict:
    """
    One-switch probe on which fundamentals date column stands in for "when
    was this fact actually knowable": safe='filed' (the real SEC filing
    date) vs leaky='period_end' (the fiscal period's own end date). Using
    period_end pretends the market already had a fact it would not
    actually see for weeks to months, silently pulling the market's real
    reaction to the eventual filing into the "forward return" window that
    starts right after the (fake, early) signal date -- and inflating
    measured predictive power for a reason that has nothing to do with the
    signal's real content. Reports the inflation in pooled IC at `horizon`
    days (evaluation/ic.evaluate_ic) from getting that one switch wrong.
    """
    base_kwargs = dict(symbols=symbols, metric=metric, form=form,
                       horizon=horizon, start=start, end=end,
                       benchmark=benchmark, price_table=price_table,
                       date_col="filed")
    return one_switch_ablation(_run_fundamentals_pipeline, base_kwargs,
                               {"date_col": "period_end"}, lambda r: r)


def _label_outcome_corr(labels: "pd.Series", outcomes: "pd.Series") -> "float | None":
    """
    Spearman correlation between LLM-assigned labels and the realized rate-
    decision outcome, across documents. Guarded the same way the other
    probes' metrics are: fewer than 8 aligned docs or a degenerate (zero-
    variance) side returns None rather than a spurious number. Spearman,
    not Pearson, because labels are bounded [-1, 1] and outcomes are a
    signed rate change -- the monotone association is what matters, not the
    linear fit.
    """
    al = labels.astype(float).reset_index(drop=True)
    ao = outcomes.astype(float).reset_index(drop=True)
    ok = al.notna() & ao.notna()
    if int(ok.sum()) < 8:
        return None
    l, o = al[ok], ao[ok]
    if l.nunique() < 2 or o.nunique() < 2:
        return None
    return float(l.corr(o, method="spearman"))


def _run_llm_scoring(docs: "pd.DataFrame", scorer: Callable,
                     outcome_fn: Callable, leaky: bool) -> "float | None":
    """
    Run scorer() over `docs` -- text alone when leaky=False (exactly the
    production fed_sentiment_pipeline path), text PLUS the realized outcome
    appended when leaky=True (what a model whose training data includes the
    outcome effectively sees). Then measure how well the labels correlate
    with the outcome. The outcome series is computed from the same outcome_fn
    in BOTH runs, so the only thing that differs between them is the
    information the scorer was handed -- the one-switch discipline.
    """
    work = docs.copy()
    if leaky:
        outcomes = outcome_fn(work["date"])
        work["text"] = work["text"].astype(str) + (
            " ACTUAL_OUTCOME: " + outcomes.fillna("").astype(str))
    labels = scorer(work)
    outcomes = outcome_fn(work["date"])
    return _label_outcome_corr(labels, outcomes)


def ffr_monthly_outcome(dates: "pd.Series") -> "pd.Series":
    """
    Default outcome function for llm_lookahead_leakage(): the signed change
    in the effective fed funds rate (FRED FEDFUNDS, monthly average) from
    the document's month to the FOLLOWING month, in percentage points.
    Positive = the Fed moved toward tighter policy right after this doc --
    the direction a hawkish label should predict IF the label carries real
    signal. Deliberately coarse (monthly averages, not decision-by-decision)
    because the probe only needs a defensible directional outcome, and it is
    held FIXED across the safe/leaky runs so any measurement coarseness
    cancels out of the inflation.
    """
    import query as q

    df = q.load("fred_rates_gdp_interest_rates")
    df = df[df["series_id"] == "FEDFUNDS"].copy()
    if df.empty:
        return pd.Series(index=dates.index, dtype="float64")
    s = pd.to_numeric(df["value"], errors="coerce")
    ym = pd.to_datetime(df["date"]).dt.to_period("M")
    monthly = pd.Series(s.to_numpy(), index=ym.to_numpy()).sort_index()
    out_dates = pd.to_datetime(dates).dt.to_period("M")
    cur = out_dates.map(monthly)
    nxt = (out_dates + 1).map(monthly)
    return (nxt - cur).astype("float64")


def _claude_fed_scorer(docs: "pd.DataFrame") -> "pd.Series":
    """
    Score documents the way fed_sentiment_pipeline.py does -- batching the
    text through the same SYSTEM_PROMPT / _score_batch path -- and return a
    hawkish_score Series aligned to docs.index. Requires ANTHROPIC_API_KEY.
    This is the production-path scorer; llm_lookahead_leakage() runs it
    untouched in the safe case, and appends the outcome before calling it in
    the leaky case.
    """
    import fed_sentiment_pipeline as fsp

    to_score = [
        {"id": i, "title": str(r["title"]), "text": str(r["text"])}
        for i, r in docs.iterrows()
    ]
    results = fsp._score_batch(to_score)
    by_id = {r["id"]: float(r.get("hawkish_score"))
             for r in (results or [])}
    return pd.Series([by_id.get(i, float("nan")) for i in docs.index],
                     index=docs.index)


def llm_lookahead_leakage(docs: "pd.DataFrame",
                          scorer: "Callable | None" = None,
                          outcome_fn: Callable = ffr_monthly_outcome) -> dict:
    """
    One-switch probe on the INFORMATION SET an LLM scorer is handed.
    Safe = score the document text exactly as fed_sentiment_pipeline.py does
    (no outcome anywhere in the prompt). Leaky = append the realized rate
    outcome to the text before scoring, simulating a model whose training
    data included post-hoc rate history it could "remember" while reading a
    speech. Reports the inflation in Spearman(docs' labels, realized outcome)
    from flipping that one switch.

    The worry this encodes (Phase 7 of the backtest rigor audit): an LLM
    trained through a cutoff has seen news coverage of what the Fed actually
    did. If its hawkish/dovish score shades toward the realized outcome even
    when the text itself says nothing about it, the score's predictive power
    overstates the text's -- and any factor built from these scores inherits
    that overstatement. Run this once before fed_sentiment ever joins
    signal_panel(); it is currently dormant because the pipeline itself is
    unwired (no ANTHROPIC_API_KEY set).

    docs: DataFrame with at least 'date' (pd.Timestamp-able) and 'text'.
    scorer: callable(docs) -> Series of hawkish_score aligned to docs.index.
      Defaults to the pipeline's own Claude scorer (needs ANTHROPIC_API_KEY).
      Tests inject a deterministic stand-in.
    outcome_fn: callable(dates: Series) -> Series of signed outcomes aligned
      to the same index. Defaults to ffr_monthly_outcome().

    Returns the one_switch_ablation() dict: switch, safe_value/leaky_value,
    safe_metric/leaky_metric (both Spearman correlations), inflation.
    """
    base_kwargs = dict(docs=docs, scorer=scorer or _claude_fed_scorer,
                       outcome_fn=outcome_fn, leaky=False)
    return one_switch_ablation(_run_llm_scoring, base_kwargs,
                               {"leaky": True}, lambda r: r)
