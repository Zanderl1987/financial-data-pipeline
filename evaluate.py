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


def _print_trades_summary(res):
    s = res["results"].get("summary", {})
    p = res["results"].get("permutation", {})
    print(f"== {res['name']} (trade rule) run {res['run_id']} ==")
    print(f"trades {s.get('n_trades', 0)} "
          f"(long {s.get('n_long', 0)} / short {s.get('n_short', 0)}) "
          f"win_rate {_fmt(s.get('win_rate_pct'))}% "
          f"pnl ${_fmt(s.get('total_pnl_dollars'))}")
    print(f"permutation null: pnl_p {_fmt(p.get('pnl_p'))} "
          f"win_rate_p {_fmt(p.get('win_rate_p'))} "
          f"(n_perm={p.get('n_perm')})")
    meta = res["results"].get("meta_filtered")
    if meta:
        if meta.get("meta_reason"):
            print(f"meta-label: {meta['meta_reason']}")
        else:
            u, f = meta["unfiltered"], meta["filtered"]
            print(f"meta-label @ {meta['threshold']}: kept "
                  f"{meta['n_kept']}/{meta['n_scored']} "
                  f"({100 * meta['kept_fraction']:.1f}%) "
                  f"win_rate {_fmt(u.get('win_rate_pct'))}% -> "
                  f"{_fmt(f.get('win_rate_pct'))}%")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Unified evaluation framework -- compute stage")
    ap.add_argument("--input-parquet", default=None,
                    help="parquet with [symbol, date, value] or "
                         "[symbol, date, label]")
    ap.add_argument("--adapter",
                    choices=["signal-panel", "sentiment", "rating",
                             "rating-changes", "tv-rule"], default=None,
                    help="evaluate a repo-native source instead of a parquet")
    ap.add_argument("--factor", default="composite",
                    help="signal-panel adapter: which factor column")
    ap.add_argument("--signal-col", default="rating_all",
                    help="rating adapter: which rating column")
    ap.add_argument("--min-step", type=int, default=1,
                    help="rating-changes adapter: minimum bucket jump")
    ap.add_argument("--input-type", choices=["signal", "events"],
                    default="signal")
    ap.add_argument("--name", default=None,
                    help="registry name (required with --input-parquet; "
                         "adapters name themselves)")
    ap.add_argument("--lag-days", type=int, default=0,
                    help="business days between data date and availability")
    ap.add_argument("--direction", type=int, choices=[1, -1, 0], default=1)
    ap.add_argument("--universe", nargs="*", default=None)
    ap.add_argument("--exclude-otc", action="store_true",
                    help="signal-panel adapter: restrict to symbol_universe.csv's "
                         "exchange-listed symbols (drops OTC Markets/Nasdaq OTCBB)")
    ap.add_argument("--min-dollar-volume", type=float, default=None,
                    help="signal-panel adapter: point-in-time trailing-21d dollar-"
                         "volume floor (no look-ahead) -- see evaluation/universe.py")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--price-table", default=None)
    ap.add_argument("--quantiles", type=int, default=5)
    ap.add_argument("--rebalance", default="M")
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--n-perm", type=int, default=200)
    ap.add_argument("--meta-label", action="store_true",
                    help="trade-rule/adapter=tv-rule only: fit a walk-forward "
                         "meta-label filter (evaluation/meta_label.py) on top "
                         "of the simulated trades and register it as its own "
                         "meta_filtered/meta_unfiltered evaluation")
    ap.add_argument("--meta-threshold", type=float, default=0.5,
                    help="meta-label keep-probability threshold")
    ap.add_argument("--regime-report", action="store_true",
                    help="tag this run with its IS/OOS regime composition "
                         "(evaluation/regime.py Statistical Jump Model on "
                         "--regime-benchmark, k=--regime-k) -- what fraction "
                         "of the evaluated window fell in each regime")
    ap.add_argument("--regime-benchmark", default="SPY")
    ap.add_argument("--regime-k", type=int, default=2)
    ap.add_argument("--out-root", default=None)
    ap.add_argument("--registry-path", default=None)
    ap.add_argument("--no-registry", action="store_true",
                    help="do not append this run to the results registry")
    args = ap.parse_args(argv)

    if bool(args.input_parquet) == bool(args.adapter):
        ap.error("pass exactly one of --input-parquet or --adapter")

    from evaluation import runner
    from evaluation.contracts import EventSet, Signal

    cache = None
    if args.adapter:
        from evaluation import adapters
        if args.adapter == "signal-panel":
            symbols = args.universe
            eligible = None
            if args.exclude_otc or args.min_dollar_volume is not None:
                from evaluation import universe as _universe
                # Route through the eligible= path (light feature_matrix, no
                # fundamentals/short-interest/insider/sentiment blocks) any
                # time OTC exclusion or a liquidity floor is requested -- both
                # can select thousands of symbols, and the default heavy panel
                # OOMs at that scale (backlog item S). A floor of 0 with only
                # --exclude-otc still applies via this path so nothing bypasses
                # the OOM-safe route.
                if not symbols:
                    symbols = _universe.exchange_listed_symbols(
                        exclude_otc=args.exclude_otc)
                floor = args.min_dollar_volume if args.min_dollar_volume is not None else 0.0
                eligible = _universe.point_in_time_eligible(
                    symbols, min_dollar_volume=floor,
                    start=args.start, end=args.end)
            obj = adapters.from_signal_panel(factor=args.factor,
                                             symbols=symbols,
                                             start=args.start, end=args.end,
                                             eligible=eligible)
        elif args.adapter == "sentiment":
            obj = adapters.from_sentiment(start=args.start, end=args.end)
        elif args.adapter == "rating":
            obj = adapters.from_rating_history(signal_col=args.signal_col,
                                               symbols=args.universe,
                                               price_table=args.price_table,
                                               start=args.start, end=args.end)
        elif args.adapter == "tv-rule":
            obj = adapters.tv_threshold_rule()
            cache = adapters.rating_cache(symbols=args.universe,
                                          price_table=args.price_table,
                                          start=args.start, end=args.end)
        else:                                   # rating-changes
            obj = adapters.from_rating_changes(symbols=args.universe,
                                               start=args.start or "2000-01-01",
                                               end=args.end,
                                               min_step=args.min_step,
                                               price_table=args.price_table)
        if args.name:
            obj.name = args.name
        from evaluation.contracts import TradeRule
        if isinstance(obj, TradeRule):
            args.input_type = "trades"
        elif isinstance(obj, EventSet):
            args.input_type = "events"
        else:
            args.input_type = "signal"
    else:
        if not args.name:
            ap.error("--name is required with --input-parquet")
        import pandas as pd
        frame = pd.read_parquet(args.input_parquet)
        if args.input_type == "signal":
            obj = Signal(name=args.name, frame=frame, lag_days=args.lag_days,
                         direction=args.direction, source=args.input_parquet)
        else:
            obj = EventSet(name=args.name, frame=frame,
                           lag_days=args.lag_days)

    kwargs = dict(universe=args.universe, start=args.start, end=args.end,
                  benchmark=args.benchmark, price_table=args.price_table,
                  quantiles=args.quantiles, rebalance=args.rebalance,
                  write_registry=not args.no_registry,
                  n_boot=args.n_boot, n_perm=args.n_perm,
                  meta_label=args.meta_label, meta_threshold=args.meta_threshold,
                  regime_report=args.regime_report,
                  regime_benchmark=args.regime_benchmark, regime_k=args.regime_k)
    if cache is not None:
        kwargs["cache"] = cache
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
    elif args.input_type == "events":
        _print_events_summary(res)
    else:
        _print_trades_summary(res)
    comp = res["results"].get("regime_composition")
    if comp:
        pct = ", ".join(f"regime {k.split('_')[-1]}: {100*v:.1f}%"
                        for k, v in comp.items() if k != "n_days")
        print(f"regime composition ({comp['n_days']} days): {pct}")
    elif res["results"].get("regime_reason"):
        print(f"regime composition: {res['results']['regime_reason']}")
    print(f"artifacts: {res['out_dir']}")
    print(f"registry rows written: {res['rows_written']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
