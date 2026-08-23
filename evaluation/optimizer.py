"""
evaluation/optimizer.py -- W5: parameter search + walk-forward OPTIMIZATION.

This module is, by design, the most dangerous code in the repo. Parameter
search is a p-hacking machine: run enough trials against one history and
something will look great by chance alone. The 2026-08-08 TV-rating
investigation (experiments/) is the in-repo cautionary tale -- an adaptively
chosen chain of tests inflated a lead that died at broad-market scale. The
2026-08-11 pre-registration exists because of it.

Safety machinery (all of it mandatory, none optional):

1. EVERY objective evaluation is appended to the registry as an
   evaluation="optimizer" row (statistic "opt_sharpe"), keyed
   input_name="<signal>@<params>". The honest trial set for deflated Sharpe
   now includes them: see dsr_with_opt_trials().
2. Existing signals' DSR numbers are untouched: population("sharpe")
   semantics are unchanged; opt trials live under their own statistic and
   are UNIONED IN only where this module computes DSR.
3. Search optimizes realized-daily-Sharpe only. Finalists (top-k by Sharpe)
   additionally get permutation-tested (pnl_p / win_rate_p) before anything
   is believed -- search cheap, verify expensive.
4. PBO (robustness.pbo) runs over the full trial return matrix: did picking
   by in-sample rank work at all?
5. Walk-forward mode re-optimizes per fold on TRAIN ONLY and reports stitched
   OOS against two references: default-params OOS (did optimizing help?) and
   full-sample-optimal whole-period performance (the in-sample fantasy --
   what overfitting would have looked like).
6. Nothing here writes to the strategy registry or promotes anything into a
   campaign. Output = JSON artifact + registry rows + a printed verdict.
   Promotion remains a human one-shot decision per the pre-registration.

Solvers: exhaustive grid + scipy differential_evolution (deterministic seed,
polish=False). CMA-ES would need the `cma` package -- deliberately not added;
DE covers the same role at these dimensionalities.
"""

import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from itertools import product

import numpy as np
import pandas as pd
from scipy.optimize import differential_evolution

import evaluation.execution as ev_execution
import evaluation.registry as ev_registry
import evaluation.stats as ev_stats
from evaluation.contracts import TradeRule
from evaluation.tearsheet import daily_returns_from_trades

ARTIFACT_DIR = os.path.join("storage", "eval_artifacts", "optimizer")

ANN = 252.0


def _ann_sharpe(returns) -> "float | None":
    """Annualized Sharpe from a daily return series, SD_FLOOR-guarded."""
    s = pd.Series(returns).dropna()
    if len(s) < 20:
        return None
    sd = float(s.std(ddof=1))
    if ev_stats._degenerate_sd(sd):
        return None
    return round(float(s.mean() / sd * math.sqrt(ANN)), 4)


# ---------------------------------------------------------------- parameters


@dataclass(frozen=True)
class Param:
    name: str
    lo: float = 0.0
    hi: float = 1.0
    default: float = 0.0
    kind: str = "float"          # "float" | "int" | "choice"
    choices: tuple = ()

    def grid_values(self, points: int):
        if self.kind == "choice":
            return list(self.choices)
        if self.kind == "int":
            lo, hi = int(round(self.lo)), int(round(self.hi))
            n = max(2, min(points, hi - lo + 1))
            return sorted({int(v) for v in np.linspace(lo, hi, n)})
        vals = np.linspace(self.lo, self.hi, points)
        return [round(float(v), 6) for v in vals]

    def coerce(self, raw) -> object:
        if self.kind == "int":
            return int(round(float(raw)))
        if self.kind == "choice":
            idx = int(round(float(raw)))
            return self.choices[max(0, min(idx, len(self.choices) - 1))]
        return round(float(np.clip(raw, self.lo, self.hi)), 6)


@dataclass(frozen=True)
class ParamSpace:
    signal_name: str             # base signal ("tv_threshold"); also the
                                 # input_name whose "sharpe" rows are excluded
                                 # from the combined DSR population
    family: str                  # "trade_rule" | "cross_sectional"
    params: tuple                # tuple[Param]
    builder: object = None       # family-specific callable

    def vector_to_params(self, vec) -> dict:
        return {p.name: p.coerce(v) for p, v in zip(self.params, vec)}

    def params_tag(self, params: dict) -> str:
        return ",".join(f"{k}={params[k]}" for k in sorted(params))

    def default_vector(self) -> "list[float]":
        out = []
        for p in self.params:
            if p.kind == "choice":
                out.append(float(p.choices.index(p.default)))
            else:
                out.append(float(p.default))
        return out

    def bounds(self):
        return [(p.lo, p.hi) for p in self.params]


# ---- trade-rule builders (mirror backtest_app.py; evaluation/ cannot import
# ---- the app -- the dependency arrow points the other way)


def _crossed_up(s, level):
    return (s > level) & (s.shift(1) <= level)


def _crossed_down(s, level):
    return (s < level) & (s.shift(1) >= level)


_TV_PARAMS = (
    Param("bull_min", 0.2, 0.9, 0.5),
    Param("exit_long_max", -0.3, 0.5, 0.1),
    Param("bear_max", -0.9, -0.2, -0.5),
    Param("exit_short_min", -0.5, 0.3, -0.1),
)


def _build_tv_threshold(bull_min, exit_long_max, bear_max, exit_short_min,
                        notional=10_000.0) -> TradeRule:
    return TradeRule(
        name="tv_threshold_opt",
        entries=lambda d: _crossed_up(d["rating_all"], bull_min),
        exits=lambda d: d["rating_all"] < exit_long_max,
        side="both",
        short_entries=lambda d: _crossed_down(d["rating_all"], bear_max),
        short_exits=lambda d: d["rating_all"] > exit_short_min,
        notional=notional)


def _build_tv_fade(bull_min, exit_long_max, bear_max, exit_short_min,
                   notional=10_000.0) -> TradeRule:
    # Sides swapped vs tv_threshold (fade the rating): LONG on the bear-entry
    # cross, SHORT on the bull-entry cross. See experiments/2026-08-08.
    return TradeRule(
        name="tv_fade_opt",
        entries=lambda d: _crossed_down(d["rating_all"], bear_max),
        exits=lambda d: d["rating_all"] > exit_short_min,
        side="both",
        short_entries=lambda d: _crossed_up(d["rating_all"], bull_min),
        short_exits=lambda d: d["rating_all"] < exit_long_max,
        notional=notional)


def _build_tv_fade_long(bull_min, exit_long_max, bear_max, exit_short_min,
                        notional=10_000.0) -> TradeRule:
    # Long-only half of the fade rule. bull_min/exit_long_max accepted but
    # unused (uniform signature); documented in backtest_app too.
    return TradeRule(
        name="tv_fade_long_opt",
        entries=lambda d: _crossed_down(d["rating_all"], bear_max),
        exits=lambda d: d["rating_all"] > exit_short_min,
        side="long",
        notional=notional)


#: Bounds mirror the interactive app's exploration region ([-.9,.9]-ish box,
#: tightened where the sliders' extremes are degenerate e.g. always-in).
BUILTIN_SPACES = {
    "tv_threshold": ParamSpace("tv_threshold", "trade_rule", _TV_PARAMS,
                               _build_tv_threshold),
    "tv_fade": ParamSpace("tv_fade", "trade_rule", _TV_PARAMS,
                          _build_tv_fade),
    "tv_fade_long": ParamSpace("tv_fade_long", "trade_rule", _TV_PARAMS,
                               _build_tv_fade_long),
}


def register_space(space: ParamSpace) -> None:
    """Extension point for custom parameter spaces (same pattern as
    backtest_app.register_trade_rule_signal). Replaces a builtin silently."""
    BUILTIN_SPACES[space.signal_name] = space


# ---------------------------------------------------------------- objectives


@dataclass
class TrialResult:
    params: dict
    sharpe: "float | None"
    n_trades: int = 0
    n_days: int = 0
    returns: object = None      # daily return Series, kept in memory only
    reason: "str | None" = None
    perm: "dict | None" = None  # finalist-only permutation results


class BudgetExhausted(Exception):
    pass


class Evaluator:
    """Wraps a param space + data into a countable objective.

    Counts every call (even failures -- a failed eval still spent budget),
    logs each completed trial through `trial_log`, and keeps the per-trial
    return series in memory for the PBO matrix.
    """

    def __init__(self, space: ParamSpace, data: dict, *, config=None,
                 min_trades: int = 20, trial_log=None):
        self.space = space
        self.data = data
        self.config = config
        self.min_trades = min_trades
        self.trial_log = trial_log
        self.n_evals = 0
        self.hard_budget = None     # de_search sets this for EXACT budget
        self.results = []          # list[TrialResult], index == trial order

    def __call__(self, vec) -> float:
        self.n_evals += 1
        res = self.evaluate(vec)
        self.results.append(res)
        if self.trial_log is not None:
            self.trial_log.add(res)
        if self.hard_budget is not None and self.n_evals >= self.hard_budget:
            # Raised AFTER the last allowed trial completed and logged, so
            # the budget is exact and nothing evaluated is lost.
            raise BudgetExhausted(f"budget {self.hard_budget} reached")
        if res.sharpe is None:
            return -1e9
        return res.sharpe

    def evaluate(self, vec) -> TrialResult:
        params = self.space.vector_to_params(vec)
        if self.space.family == "trade_rule":
            return self._eval_trade_rule(params)
        return self._eval_cross_sectional(params)

    def _eval_trade_rule(self, params) -> TrialResult:
        import evaluation.trades as ev_trades
        rule = self.space.builder(notional=self.data.get("notional", 10_000.0),
                                  **params)
        trades = ev_trades.simulate(rule, self.data["cache"],
                                    config=self.config)
        n_trades = 0 if trades.empty else int(len(trades))
        if n_trades < self.min_trades:
            return TrialResult(params, None, n_trades=n_trades,
                               reason=f"fewer than {self.min_trades} trades "
                                      f"(n={n_trades})")
        dr = daily_returns_from_trades(trades)
        rets = dr.get("returns")
        if rets is None:
            return TrialResult(params, None, n_trades=n_trades,
                               reason=dr.get("returns_reason"))
        return TrialResult(params, _ann_sharpe(rets), n_trades=n_trades,
                           n_days=int(len(rets)), returns=rets)

    def _eval_cross_sectional(self, params) -> TrialResult:
        import backtest as bt
        frame = self.data["frame"]
        try:
            res = bt.backtest(frame, score=self.data.get("score", "value"),
                              price_table=self.data.get("price_table"),
                              quantiles=params["quantiles"],
                              rebalance=params["rebalance"],
                              long_short=params["long_short"])
        except Exception as exc:            # engine raises on empty windows
            return TrialResult(params, None, reason=f"{type(exc).__name__}: "
                                                    f"{exc}")
        rets = res.returns
        if isinstance(rets, pd.DataFrame):
            col = ("long_short" if "long_short" in rets.columns
                   else rets.columns[0])
            rets = rets[col]
        rets = pd.Series(rets).dropna()
        if len(rets) < 50:
            return TrialResult(params, None,
                               reason=f"only {len(rets)} return days")
        return TrialResult(params, _ann_sharpe(rets), n_days=int(len(rets)),
                           returns=rets)


# ------------------------------------------------------------- trial logging


class TrialLog:
    """Buffers optimizer trial rows and flushes them into the registry.

    One row per trial: evaluation="optimizer", statistic="opt_sharpe",
    input_name="<signal>@<params>". Flushes every `flush_every` adds so a
    crash loses at most that many trials, plus finalize() at the end.
    """

    def __init__(self, space: ParamSpace, run_id: str, *,
                 universe_hash: str = "", date_range: str = "",
                 execution_hash: str = "unknown", flush_every: int = 100,
                 path: str = None):
        self.space = space
        self.run_id = run_id
        self.universe_hash = universe_hash
        self.date_range = date_range
        self.execution_hash = execution_hash
        self.flush_every = max(1, int(flush_every))
        self.path = path or ev_registry.REG_PATH
        self._pending = []
        self.n_logged = 0

    def add(self, res: TrialResult) -> None:
        tag = self.space.params_tag(res.params)
        row = {
            "run_id": self.run_id,
            "input_name": f"{self.space.signal_name}@{tag}",
            "input_type": "optimizer_trial",
            "evaluation": "optimizer",
            "horizon": -1,
            "statistic": "opt_sharpe",
            "value": res.sharpe if res.sharpe is not None else np.nan,
            "n": res.n_days or res.n_trades,
            "universe_hash": self.universe_hash,
            "date_range": self.date_range,
            "created_at": pd.Timestamp.utcnow().isoformat(),
            "execution_hash": self.execution_hash,
        }
        self._pending.append(row)
        self.n_logged += 1
        if len(self._pending) >= self.flush_every:
            self.flush()

    def add_stat(self, statistic: str, value, n: int, input_name=None) -> None:
        """A non-trial row (perm p-value, WFA aggregate) on the same run."""
        self._pending.append({
            "run_id": self.run_id,
            "input_name": input_name or self.space.signal_name,
            "input_type": "optimizer_result",
            "evaluation": "optimizer",
            "horizon": -1,
            "statistic": statistic,
            "value": value if value is not None else np.nan,
            "n": n,
            "universe_hash": self.universe_hash,
            "date_range": self.date_range,
            "created_at": pd.Timestamp.utcnow().isoformat(),
            "execution_hash": self.execution_hash,
        })

    def flush(self) -> int:
        if not self._pending:
            return 0
        rows = pd.DataFrame(self._pending)
        self._pending = []
        added = ev_registry.append(rows, path=self.path)
        return added

    def finalize(self) -> int:
        return self.flush()


def dsr_with_opt_trials(sharpe_ann, n_days: int, base_input_name: str,
                        extra_trials: "list | None" = None,
                        path: str = None) -> dict:
    """
    Deflated Sharpe against the HONEST trial set for this research program:
    every latest-per-input sharpe in the registry's main population (minus
    this signal's own baseline row), UNIONED with every distinct parameter
    vector ever optimized (statistic "opt_sharpe", all signals -- parameter
    shopping anywhere in the program inflates everyone's multiple-testing
    burden), plus any unflushed extras from the current run.
    """
    path = path or ev_registry.REG_PATH
    pop = ev_registry.population("sharpe", path=path,
                                 exclude_input_name=base_input_name)
    pop += ev_registry.population("opt_sharpe", path=path)
    pop += [float(v) for v in (extra_trials or [])
            if v is not None and np.isfinite(v)]
    return ev_stats.deflated_sharpe(sharpe_ann, n_days, pop)


def pbo_matrix(evaluator: Evaluator) -> "tuple[pd.DataFrame, str]":
    """T x N matrix of per-trial daily returns aligned on a common calendar.

    Trials without a return series (degenerate/failed) are dropped; PBO needs
    >= 2 columns. Returns (matrix_or_empty_df, reason).
    """
    series = [r.returns for r in evaluator.results
              if r.returns is not None and len(r.returns) > 0]
    if len(series) < 2:
        return pd.DataFrame(), (f"need >=2 trials with returns "
                                f"(got {len(series)})")
    df = pd.concat(series, axis=1, sort=True).dropna(how="any")
    if df.empty:
        return pd.DataFrame(), "no overlapping dates across trial returns"
    return df, ""


# ------------------------------------------------------------------ solvers


def grid_search(evaluator: Evaluator, points: int = 5,
                max_evals: "int | None" = None) -> "list[dict]":
    """
    Exhaustive grid. Raises ValueError with the exact count when the product
    exceeds max_evals rather than silently truncating (a truncated grid is a
    different experiment than the one requested).
    """
    axes = [p.grid_values(points) for p in evaluator.space.params]
    total = 1
    for a in axes:
        total *= len(a)
    if max_evals is not None and total > max_evals:
        raise ValueError(f"grid needs {total} evals > max_evals={max_evals}; "
                         f"raise --max-evals, lower --grid-points, or use "
                         f"--method de")
    for combo in product(*axes):
        evaluator(np.asarray(combo, dtype=float))
    return evaluator.results


def de_search(evaluator: Evaluator, max_evals: int = 300, seed: int = 0,
              popsize: int = 12) -> dict:
    """
    scipy differential_evolution over the space bounds, polish=False,
    deterministic seed. Budget is EXACT: the objective raises
    BudgetExhausted after completing trial #max_evals (the trial is kept and
    logged); differential_evolution propagates it and we return best-so-far.
    """
    neg = lambda vec: -evaluator(vec)

    choice_dims = [i for i, p in enumerate(evaluator.space.params)
                   if p.kind == "choice"]
    if choice_dims:
        raise ValueError("differential_evolution handles float/int params; "
                         "spaces with choice params should use grid_search")

    evaluator.hard_budget = max_evals
    try:
        differential_evolution(neg, evaluator.space.bounds(), seed=seed,
                               popsize=popsize,
                               maxiter=max(max_evals // max(popsize, 1), 1),
                               tol=-1.0, mutation=(0.5, 1.0),
                               recombination=0.7, polish=False,
                               init="latinhypercube", disp=False)
    except BudgetExhausted:
        pass
    finally:
        evaluator.hard_budget = None

    best_idx, best = None, None
    for i, r in enumerate(evaluator.results):
        if r.sharpe is not None and (best is None or r.sharpe > best):
            best, best_idx = r.sharpe, i
    return {"best_index": best_idx, "best_sharpe": best,
            "n_evals": evaluator.n_evals}


# ------------------------------------------------------------ single-split run


def _universe_hash_of(data: dict) -> str:
    if "cache" in data:
        syms = list(data["cache"].keys())
    else:
        syms = list(pd.unique(data["frame"]["symbol"]))
    return ev_registry.universe_hash(syms)


def _date_bounds(data: dict) -> "tuple[str, str]":
    if "cache" in data:
        lo, hi = None, None
        for df in data["cache"].values():
            if df.empty:
                continue
            d0, d1 = df.index.min(), df.index.max()
            lo = d0 if lo is None else min(lo, d0)
            hi = d1 if hi is None else max(hi, d1)
    else:
        d = pd.to_datetime(data["frame"]["date"])
        lo, hi = d.min(), d.max()
    return (str(pd.Timestamp(lo).date()), str(pd.Timestamp(hi).date()))


def run_search(space: ParamSpace, data: dict, *, method: str = "grid",
               points: int = 5, max_evals: int = 300, top_k: int = 5,
               n_perm: int = 200, seed: int = 0, config=None,
               min_trades: int = 20, registry_path: str = None,
               artifact_dir: str = None) -> dict:
    """
    Single-split optimization: search the whole period, permutation-test the
    finalists, DSR against the combined trial set, PBO over the trial matrix.
    Returns the artifact dict (also written to JSON).
    """
    run_id = uuid.uuid4().hex[:12]
    uhash = _universe_hash_of(data)
    lo, hi = _date_bounds(data)
    log = TrialLog(space, run_id, universe_hash=uhash,
                   date_range=f"{lo}..{hi}",
                   execution_hash=(ev_execution.config_hash(config)
                                   if config else "unknown"),
                   path=registry_path)
    ev = Evaluator(space, data, config=config, min_trades=min_trades,
                   trial_log=log)

    t0 = time.time()
    if method == "grid":
        grid_search(ev, points=points, max_evals=max_evals)
        method_note = f"grid {points} pts/param"
    elif method == "de":
        de_search(ev, max_evals=max_evals, seed=seed)
        method_note = f"differential_evolution budget={max_evals} seed={seed}"
    else:
        raise ValueError(f"unknown method '{method}' (grid|de)")
    elapsed = round(time.time() - t0, 1)

    scored = [r for r in ev.results if r.sharpe is not None]
    scored.sort(key=lambda r: r.sharpe, reverse=True)
    finalists = scored[:max(1, top_k)]

    # Expensive verification only for what survived the cheap search.
    perm_rows = []
    if n_perm and finalists:
        import evaluation.trades as ev_trades
        for r in finalists:
            if space.family != "trade_rule":
                break                       # permutation_trades is rule-based
            rule = space.builder(notional=data.get("notional", 10_000.0),
                                 **r.params)
            pt = ev_stats.permutation_trades(rule, data["cache"],
                                             n_perm=n_perm, seed=seed)
            r.perm = {"pnl_p": pt.get("pnl_p"),
                      "win_rate_p": pt.get("win_rate_p"),
                      "n_perm": n_perm}
            perm_rows.append(r.perm)
            log.add_stat("opt_perm_pnl_p", pt.get("pnl_p"),
                         int(n_perm),
                         input_name=f"{space.signal_name}"
                                    f"@{space.params_tag(r.params)}")
            log.add_stat("opt_perm_win_rate_p", pt.get("win_rate_p"),
                         int(n_perm),
                         input_name=f"{space.signal_name}"
                                    f"@{space.params_tag(r.params)}")

    best = scored[0] if scored else None
    dsr = {}
    pct = {}
    if best is not None and best.sharpe is not None:
        # Trials must be IN the registry before computing DSR -- the combined
        # population reads them back from there (no extra_trials here, or the
        # current run would be counted twice).
        log.finalize()
        dsr = dsr_with_opt_trials(best.sharpe, best.n_days,
                                  space.signal_name, path=registry_path)
        pop = ev_registry.population("opt_sharpe", path=registry_path)
        pct = ev_stats.registry_percentile(best.sharpe, pop)

    matrix, pbo_reason = pbo_matrix(ev)
    pbo_res = {"pbo": None, "pbo_reason": pbo_reason}
    if not matrix.empty:
        from evaluation.robustness import pbo as _pbo
        pbo_res = _pbo(matrix)

    artifact = {
        "run_id": run_id, "mode": "single_split",
        "space": space.signal_name, "family": space.family,
        "method": method_note,
        "params_schema": [{k: v for k, v in p.__dict__.items()}
                          for p in space.params],
        "universe_hash": uhash, "date_range": f"{lo}..{hi}",
        "config": config.as_dict() if config else None,
        "min_trades": min_trades, "seed": seed,
        "n_evals": ev.n_evals, "elapsed_s": elapsed,
        "n_scored": len(scored),
        "top": [{"params": r.params, "sharpe": r.sharpe,
                 "n_trades": r.n_trades, "n_days": r.n_days,
                 "reason": r.reason,
                 **getattr(r, "perm", {})} for r in finalists],
        "dsr": dsr, "registry_percentile": pct,
        "pbo": {"pbo": pbo_res.get("pbo"),
                "reason": pbo_res.get("pbo_reason",
                                      pbo_res.get("metric_reason"))},
        "verdict": _verdict(best, dsr, pbo_res),
    }
    log.finalize()
    _write_artifact(artifact, artifact_dir)
    return artifact


def _verdict(best, dsr: dict, pbo_res: dict) -> "list[str]":
    """ASCII verdict lines. Skepticism defaults: null results are normal."""
    out = []
    if best is None or best.sharpe is None:
        return ["NO SCORABLE TRIAL -- nothing beat the degenerate guards."]
    out.append(f"BEST: sharpe {best.sharpe} @ {best.params}")
    p = dsr.get("dsr_prob")
    if p is None:
        out.append(f"DSR unavailable ({dsr.get('dsr_reason', '?')})")
    elif p < 0.05:
        out.append(f"DSR prob {p} -- survives deflation (still: one split, "
                   f"verify OOS before believing)")
    else:
        out.append(f"DSR prob {p} -- consistent with selection noise; do NOT "
                   f"promote on this evidence")
    pb = pbo_res.get("pbo")
    if pb is None:
        out.append(f"PBO unavailable ({pbo_res.get('pbo_reason', '?')})")
    elif pb >= 0.4:
        out.append(f"PBO {pb} -- picking by IS rank barely generalizes; "
                   f"treat the winner as noise")
    else:
        out.append(f"PBO {pb} -- selection procedure generalizes acceptably")
    return out


def _write_artifact(artifact: dict, artifact_dir: str = None) -> str:
    d = artifact_dir or ARTIFACT_DIR
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{artifact['run_id']}.json")

    class _Enc(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, (np.floating, np.integer)):
                return o.item()
            if isinstance(o, pd.Timestamp):
                return o.isoformat()
            return super().default(o)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, indent=2, cls=_Enc)
    return path


# --------------------------------------------------- walk-forward optimization


def _slice_cache(cache: dict, d0, d1) -> dict:
    out = {}
    for sym, df in cache.items():
        if df.empty:
            continue
        sub = df.loc[(df.index >= d0) & (df.index <= d1)]
        if not sub.empty:
            out[sym] = sub
    return out


def _global_dates(cache: dict) -> "pd.DatetimeIndex":
    idx = None
    for df in cache.values():
        if df.empty:
            continue
        idx = df.index if idx is None else idx.union(df.index)
    return pd.DatetimeIndex([]) if idx is None else pd.DatetimeIndex(idx)


def _stitched_metrics(pieces: "list[pd.Series]") -> dict:
    s = pd.concat(pieces).sort_index()
    eq = (1.0 + s.fillna(0.0)).cumprod()
    total_ret = float(eq.iloc[-1] - 1.0)
    mdd = float((eq / eq.cummax() - 1.0).min())
    return {"sharpe": _ann_sharpe(s), "total_return_pct": round(total_ret * 100, 2),
            "max_drawdown_pct": round(mdd * 100, 2), "n_days": int(len(s))}


def walk_forward_optimize(space: ParamSpace, cache: dict, *,
                          method: str = "de", points: int = 4,
                          fold_budget: int = 120, n_folds: int = 4,
                          min_train_days: int = 252, top_k: int = 1,
                          n_perm: int = 0, seed: int = 0, config=None,
                          min_trades: int = 20, registry_path: str = None,
                          artifact_dir: str = None) -> dict:
    """
    Anchored walk-forward OPTIMIZATION (as opposed to stats.walk_forward's
    fixed-rule stability check): per fold, search on TRAIN only, freeze the
    argmax, evaluate on TEST; stitch test pieces into the honest OOS curve.

    Reported alongside:
      * default-param OOS -- same folds, no optimization (the bar to beat)
      * full-sample-optimal -- best-on-everything, evaluated on everything
        (the in-sample fantasy; what overfitting looks like; its own search
        trials are logged like any other)

    Fold-inner trials ARE logged -- they are real trials of this program.
    """
    run_id = uuid.uuid4().hex[:12]
    uhash = _universe_hash_of({"cache": cache})
    lo, hi = _date_bounds({"cache": cache})
    log = TrialLog(space, run_id, universe_hash=uhash,
                   date_range=f"{lo}..{hi}",
                   execution_hash=(ev_execution.config_hash(config)
                                   if config else "unknown"),
                   path=registry_path)

    dates = _global_dates(cache)
    need = min_train_days + n_folds * 21
    if len(dates) < need:
        return {"run_id": run_id, "mode": "walk_forward",
                "wf_reason": f"only {len(dates)} dates (< {need} needed)"}

    oos_dates = dates[min_train_days:]
    folds = []
    oos_pieces, default_pieces = [], []
    for i, chunk in enumerate(np.array_split(oos_dates, n_folds)):
        t_end = chunk[0]
        train_cache = _slice_cache(cache, dates[0], t_end)
        test_cache = _slice_cache(cache, chunk[0], chunk[-1])

        ev_train = Evaluator(space, {"cache": train_cache}, config=config,
                             min_trades=min_trades, trial_log=log)
        if method == "grid":
            grid_search(ev_train, points=points,
                        max_evals=fold_budget * n_folds)
        else:
            de_search(ev_train, max_evals=fold_budget, seed=seed + i)

        scored = [r for r in ev_train.results if r.sharpe is not None]
        if not scored:
            folds.append({"fold": i + 1, "date_range":
                          f"{pd.Timestamp(chunk[0]).date()}.."
                          f"{pd.Timestamp(chunk[-1]).date()}",
                          "chosen_params": None,
                          "reason": "no scorable trial in train window"})
            continue
        scored.sort(key=lambda r: r.sharpe, reverse=True)
        chosen = scored[0]

        # Frozen-champion eval on the test window (logged: it is a trial too).
        ev_test = Evaluator(space, {"cache": test_cache}, config=config,
                            min_trades=min_trades, trial_log=log)
        ev_test(_vec_of(space, chosen.params))
        test_res = ev_test.results[-1]
        ev_def = Evaluator(space, {"cache": test_cache}, config=config,
                           min_trades=min_trades, trial_log=None)
        ev_def(space.default_vector())
        default_res = ev_def.results[-1]

        folds.append({
            "fold": i + 1,
            "date_range": f"{pd.Timestamp(chunk[0]).date()}.."
                          f"{pd.Timestamp(chunk[-1]).date()}",
            "train_days": int(len(_global_dates(train_cache))),
            "chosen_params": chosen.params,
            "chosen_train_sharpe": chosen.sharpe,
            "test_sharpe": test_res.sharpe,
            "default_sharpe_on_test": default_res.sharpe,
        })
        if test_res.returns is not None:
            oos_pieces.append(test_res.returns)
        if default_res.returns is not None:
            default_pieces.append(default_res.returns)

    # Full-sample fantasy: unconstrained search on everything (logged).
    ev_full = Evaluator(space, {"cache": cache}, config=config,
                        min_trades=min_trades, trial_log=log)
    if method == "grid":
        grid_search(ev_full, points=points, max_evals=fold_budget * n_folds)
    else:
        de_search(ev_full, max_evals=fold_budget * 2, seed=seed + 999)
    full_scored = [r for r in ev_full.results if r.sharpe is not None]
    fantasy = None
    if full_scored:
        full_scored.sort(key=lambda r: r.sharpe, reverse=True)
        fb = full_scored[0]
        fantasy = {"params": fb.params, "whole_period_sharpe": fb.sharpe,
                   "note": "in-sample fantasy: picked AND scored on the "
                           "same full sample"}

    wfa_m = _stitched_metrics(oos_pieces) if oos_pieces else None
    def_m = _stitched_metrics(default_pieces) if default_pieces else None

    dsr = {}
    if wfa_m and wfa_m["sharpe"] is not None:
        log.finalize()
        all_opt = ev_registry.population("opt_sharpe", path=registry_path)
        dsr = ev_stats.deflated_sharpe(wfa_m["sharpe"], wfa_m["n_days"],
                                       all_opt)

    helped = None
    if wfa_m and def_m and wfa_m["sharpe"] is not None \
            and def_m["sharpe"] is not None:
        helped = bool(wfa_m["sharpe"] > def_m["sharpe"])

    artifact = {
        "run_id": run_id, "mode": "walk_forward",
        "space": space.signal_name, "family": space.family,
        "method": (f"grid {points}pts" if method == "grid"
                   else f"de budget={fold_budget}/fold seed={seed}"),
        "universe_hash": uhash, "date_range": f"{lo}..{hi}",
        "config": config.as_dict() if config else None,
        "n_folds": n_folds, "folds": folds,
        "wfa_oos": wfa_m, "default_oos": def_m,
        "optimization_helped_oos": helped,
        "full_sample_fantasy": fantasy,
        "dsr_wfa_oos": dsr,
        "verdict": _wfa_verdict(wfa_m, def_m, helped, dsr, fantasy),
    }
    log.finalize()
    _write_artifact(artifact, artifact_dir)
    return artifact


def _vec_of(space: ParamSpace, params: dict) -> "list[float]":
    """Inverse of vector_to_params: params dict -> raw vector."""
    out = []
    for p in space.params:
        v = params[p.name]
        if p.kind == "choice":
            out.append(float(p.choices.index(v)))
        else:
            out.append(float(v))
    return out


def _wfa_verdict(wfa_m, def_m, helped, dsr, fantasy) -> "list[str]":
    out = []
    if wfa_m is None:
        return ["WFA OOS UNAVAILABLE -- no fold produced a scorable champion."]
    out.append(f"WFA stitched OOS: sharpe {wfa_m['sharpe']}, "
               f"ret {wfa_m['total_return_pct']}%, "
               f"mdd {wfa_m['max_drawdown_pct']}%")
    if def_m and def_m["sharpe"] is not None:
        rel = "BEATS" if helped else ("ties" if helped is None else "does NOT beat")
        out.append(f"default-params OOS sharpe {def_m['sharpe']} -- "
                   f"optimization {rel} defaults OOS")
    if dsr.get("dsr_prob") is not None:
        out.append(f"DSR prob {dsr['dsr_prob']} on WFA OOS sharpe "
                   f"(trials N={dsr.get('n_trials')})")
    else:
        out.append(f"DSR unavailable ({dsr.get('dsr_reason', '?')})")
    if fantasy:
        gap = None
        if wfa_m["sharpe"] is not None:
            gap = round(fantasy["whole_period_sharpe"] - wfa_m["sharpe"], 2)
        out.append(f"in-sample fantasy sharpe "
                   f"{fantasy['whole_period_sharpe']} vs WFA OOS "
                   f"{wfa_m['sharpe']} (gap {gap}) -- the fantasy number is "
                   f"what overfitting would have sold you")
    out.append("Promotion is a human decision; nothing here touches the "
               "campaign.")
    return out
