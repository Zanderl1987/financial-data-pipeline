"""
optimize.py -- W5 optimizer CLI: parameter search + walk-forward optimization
over registered parameter spaces (see evaluation/optimizer.py for the safety
machinery: every trial logged to the registry, DSR on the combined trial set,
PBO on the trial matrix, finalists permutation-tested).

DIAGNOSIS ONLY. Nothing this tool produces may be promoted into the strategy
campaign without a fresh one-shot pre-registered run -- that is the whole
point of the machinery around the search.

Outputs:
  storage/eval_artifacts/optimizer/<run_id>.json   full artifact
  storage/eval_registry/results.parquet            appended trial rows
  stdout                                           ASCII summary + verdict

Usage:
  C:\ProgramData\anaconda3\python.exe optimize.py --list-spaces
  C:\ProgramData\anaconda3\python.exe optimize.py --space tv_threshold ^
      --method grid --grid-points 3 --n-perm 200
  C:\ProgramData\anaconda3\python.exe optimize.py --space tv_fade_long ^
      --method de --max-evals 300 --mode wfa --n-folds 4
"""

import argparse
import sys

from evaluation.execution import (CostModel, ExecutionConfig, PortfolioLimits,
                                  RiskControls, Sizing)
import evaluation.optimizer as opt


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="W5 parameter optimizer (diagnosis only; every trial "
                    "logged to the eval registry)")
    p.add_argument("--list-spaces", action="store_true",
                   help="list registered parameter spaces and exit")
    p.add_argument("--space", default="tv_threshold",
                   help="parameter space name (see --list-spaces)")
    p.add_argument("--method", choices=("grid", "de"), default="grid")
    p.add_argument("--mode", choices=("single", "wfa"), default="single",
                   help="single = one split; wfa = per-fold refit walk-forward")
    p.add_argument("--grid-points", type=int, default=4)
    p.add_argument("--max-evals", type=int, default=300,
                   help="de budget total (single) or per fold (wfa)")
    p.add_argument("--n-folds", type=int, default=4)
    p.add_argument("--min-train-days", type=int, default=252)
    p.add_argument("--top-k", type=int, default=5,
                   help="finalists that get permutation-tested (single mode)")
    p.add_argument("--n-perm", type=int, default=200,
                   help="permutations per finalist; 0 skips")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--min-trades", type=int, default=20)
    p.add_argument("--universe", default=None,
                   help="comma-separated symbols; default = adapter's own "
                        "universe")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--cs-factor", default="composite",
                   help="cross-sectional space: analytics factor column to "
                        "load via adapters.from_signal_panel")
    # cost overrides -- any of these switches from LEGACY (config=None) to an
    # explicit ExecutionConfig
    p.add_argument("--commission-bps", type=float, default=None)
    p.add_argument("--spread-bps", type=float, default=None)
    p.add_argument("--borrow-fee-bps", type=float, default=None)
    p.add_argument("--registry-path", default=None,
                   help="override registry path (testing)")
    p.add_argument("--artifact-dir", default=None)
    return p


def _config_from_args(args):
    if (args.commission_bps is None and args.spread_bps is None
            and args.borrow_fee_bps is None):
        return None
    return ExecutionConfig(
        costs=CostModel(commission_bps=args.commission_bps or 0.0,
                        spread_bps=args.spread_bps or 0.0,
                        borrow_fee_bps=args.borrow_fee_bps or 0.0),
        risk=RiskControls(), sizing=Sizing(), limits=PortfolioLimits())


def _load_data(space, args):
    """Build the data bundle once; every trial reuses it."""
    if space.family == "trade_rule":
        import evaluation.adapters as ev_adapters
        symbols = ([s.strip().upper() for s in args.universe.split(",")]
                   if args.universe else None)
        cache = ev_adapters.rating_cache(symbols=symbols, start=args.start,
                                         end=args.end)
        if not cache:
            raise SystemExit("rating_cache returned no data -- check "
                             "--universe/--start/--end")
        return {"cache": cache, "notional": 10_000.0}
    import evaluation.adapters as ev_adapters
    import evaluation.data as ev_data
    obj = ev_adapters.from_signal_panel(factor=args.cs_factor,
                                        start=args.start, end=args.end)
    frame = ev_data.apply_lag(obj.frame, obj.lag_days)
    return {"frame": frame, "score": "value"}


def _print_artifact(a: dict) -> None:
    print("=" * 64)
    print(f"W5 OPTIMIZER  run {a.get('run_id')}  mode={a.get('mode')}"
          f"  space={a.get('space')}")
    print(f"  {a.get('method')}   universe {a.get('universe_hash')}   "
          f"{a.get('date_range')}")
    print("=" * 64)
    if a.get("wf_reason"):
        print(f"  !! {a['wf_reason']}")
    for line in a.get("verdict", []):
        print(f"  >> {line}")
    if a.get("mode") == "walk_forward":
        for f in a.get("folds", []):
            if f.get("chosen_params") is None:
                print(f"  fold {f['fold']}: {f.get('reason')}")
                continue
            print(f"  fold {f['fold']} [{f['date_range']}] "
                  f"train_sharpe={f.get('chosen_train_sharpe')} -> "
                  f"test={f.get('test_sharpe')} "
                  f"(default-on-test {f.get('default_sharpe_on_test')})")
            print(f"     chosen: {f.get('chosen_params')}")
    else:
        for t in a.get("top", []):
            perm = (f"  pnl_p={t['pnl_p']} win_p={t['win_rate_p']}"
                    if "pnl_p" in t else "")
            print(f"  sharpe={t.get('sharpe')} n_trades={t.get('n_trades')}"
                  f"{perm}")
            print(f"     {t.get('params')}{' | ' + t['reason'] if t.get('reason') else ''}")
    print("-" * 64)


def main() -> int:
    args = _build_arg_parser().parse_args()
    if args.list_spaces:
        print("registered parameter spaces:")
        for name, sp in sorted(opt.BUILTIN_SPACES.items()):
            ps = ", ".join(f"{q.name}[{q.kind} {q.lo}..{q.hi}]"
                           if q.kind != "choice"
                           else f"{q.name}[choice {list(q.choices)}]"
                           for q in sp.params)
            print(f"  {name} ({sp.family}): {ps}")
        return 0

    space = opt.BUILTIN_SPACES.get(args.space)
    if space is None:
        print(f"unknown space '{args.space}' -- try --list-spaces")
        return 2
    config = _config_from_args(args)

    if args.mode == "wfa":
        if space.family != "trade_rule":
            print("wfa mode currently supports trade_rule spaces only")
            return 2
        import evaluation.adapters as ev_adapters
        symbols = ([s.strip().upper() for s in args.universe.split(",")]
                   if args.universe else None)
        cache = ev_adapters.rating_cache(symbols=symbols, start=args.start,
                                         end=args.end)
        artifact = opt.walk_forward_optimize(
            space, cache, method=args.method, points=args.grid_points,
            fold_budget=args.max_evals, n_folds=args.n_folds,
            min_train_days=args.min_train_days, top_k=args.top_k,
            n_perm=args.n_perm, seed=args.seed, config=config,
            min_trades=args.min_trades, registry_path=args.registry_path,
            artifact_dir=args.artifact_dir)
    else:
        data = _load_data(space, args)
        artifact = opt.run_search(
            space, data, method=args.method, points=args.grid_points,
            max_evals=args.max_evals, top_k=args.top_k, n_perm=args.n_perm,
            seed=args.seed, config=config, min_trades=args.min_trades,
            registry_path=args.registry_path, artifact_dir=args.artifact_dir)

    _print_artifact(artifact)
    return 0


if __name__ == "__main__":
    sys.exit(main())
