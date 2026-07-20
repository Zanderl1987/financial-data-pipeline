r"""
evaluate.py -- compute-stage CLI for the unified evaluation framework.

Evaluates a Signal or EventSet parquet against forward returns with the
three-tier significance battery, writes artifacts under
storage/reports/eval/, and appends baselines to the results registry.

Usage:
  C:\ProgramData\anaconda3\python.exe evaluate.py --input-parquet sig.parquet
      --input-type signal --name my_signal --lag-days 1 --direction 1
  C:\ProgramData\anaconda3\python.exe evaluate.py --input-parquet ev.parquet
      --input-type events --name fda_approvals

Input parquet layout: signal = [symbol, date, value]; events =
[symbol, date, label]. Adapter flags for repo-native sources (factor panel,
sentiment, TV ratings) are added by evaluation/adapters.py (Task 10).
"""

import argparse
import sys


def _fmt(v):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _print_signal_summary(res):
    ic = res["results"].get("ic", {})
    print(f"== {res['name']} (signal) run {res['run_id']} ==")
    print(f"{'h':>3} {'pooled_ic':>10} {'daily_ic':>9} {'t':>6} "
          f"{'spread_pct':>10}")
    for h in sorted(ic):
        d = ic[h]
        print(f"{h:>3} {_fmt(d.get('pooled_ic')):>10} "
              f"{_fmt(d.get('mean_daily_ic')):>9} "
              f"{_fmt(d.get('ic_t_stat')):>6} {_fmt(d.get('spread_pct')):>10}")
    port = res["results"].get("portfolio") or {}
    boot = port.get("sharpe_bootstrap") or {}
    if boot.get("sharpe") is not None:
        print(f"portfolio sharpe {_fmt(boot['sharpe'])} "
              f"[{_fmt(boot.get('sharpe_ci_lo'))}, "
              f"{_fmt(boot.get('sharpe_ci_hi'))}]")
    t3 = res["results"].get("tier3", {})
    dsr = t3.get("deflated_sharpe") or {}
    if dsr.get("dsr_prob") is not None:
        print(f"deflated sharpe prob {_fmt(dsr['dsr_prob'])} "
              f"(n_trials={dsr.get('n_trials')})")


def _print_events_summary(res):
    ev = res["results"].get("events", {})
    print(f"== {res['name']} (events) run {res['run_id']} ==")
    for label, d in ev.get("labels", {}).items():
        print(f"label {label}: n_events={d.get('n_events')}")
        for h, row in sorted(d.get("horizons", {}).items()):
            print(f"  h={h:>3} mean={_fmt(row.get('mean_pct'))}% "
                  f"t={_fmt(row.get('t_stat'))} n={row.get('n')}")
    for label, why in ev.get("skipped", {}).items():
        print(f"skipped {label}: {why}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Unified evaluation framework -- compute stage")
    ap.add_argument("--input-parquet", required=True,
                    help="parquet with [symbol, date, value] or "
                         "[symbol, date, label]")
    ap.add_argument("--input-type", choices=["signal", "events"],
                    default="signal")
    ap.add_argument("--name", required=True,
                    help="registry name for this input")
    ap.add_argument("--lag-days", type=int, default=0,
                    help="business days between data date and availability")
    ap.add_argument("--direction", type=int, choices=[1, -1, 0], default=1)
    ap.add_argument("--universe", nargs="*", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--price-table", default=None)
    ap.add_argument("--quantiles", type=int, default=5)
    ap.add_argument("--rebalance", default="M")
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--n-perm", type=int, default=200)
    ap.add_argument("--out-root", default=None)
    ap.add_argument("--registry-path", default=None)
    ap.add_argument("--no-registry", action="store_true",
                    help="do not append this run to the results registry")
    args = ap.parse_args(argv)

    import pandas as pd

    from evaluation import runner
    from evaluation.contracts import EventSet, Signal

    frame = pd.read_parquet(args.input_parquet)
    if args.input_type == "signal":
        obj = Signal(name=args.name, frame=frame, lag_days=args.lag_days,
                     direction=args.direction, source=args.input_parquet)
    else:
        obj = EventSet(name=args.name, frame=frame, lag_days=args.lag_days)

    kwargs = dict(universe=args.universe, start=args.start, end=args.end,
                  benchmark=args.benchmark, price_table=args.price_table,
                  quantiles=args.quantiles, rebalance=args.rebalance,
                  write_registry=not args.no_registry,
                  n_boot=args.n_boot, n_perm=args.n_perm)
    if args.out_root:
        kwargs["out_root"] = args.out_root
    if args.registry_path:
        kwargs["registry_path"] = args.registry_path
    res = runner.run(obj, **kwargs)

    if res["n_evaluations"] == 0:
        print(f"X no evaluations produced for {args.name} -- every symbol "
              "was dropped (no prices / history too short). See run_meta.json"
              f" in {res['out_dir']}")
        return 1
    if args.input_type == "signal":
        _print_signal_summary(res)
    else:
        _print_events_summary(res)
    print(f"artifacts: {res['out_dir']}")
    print(f"registry rows written: {res['rows_written']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
