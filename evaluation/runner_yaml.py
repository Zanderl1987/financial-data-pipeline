#!/usr/bin/env python
"""
evaluation/runner_yaml.py — YAML-driven evaluation runner (F1).

Run an evaluation spec from a YAML file:
    python -m evaluation.runner_yaml eval_spec.yaml

This is the F1 "YAML-configurable runner" entry point.
"""

import sys
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from evaluation.config import load_spec, spec_to_runner_kwargs, EXAMPLE_YAML


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Run evaluation from YAML spec")
    ap.add_argument("spec", nargs="?", help="Path to YAML evaluation spec")
    ap.add_argument("--print-example", action="store_true",
                    help="Print example YAML to stdout")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse spec and print runner kwargs without running")
    args = ap.parse_args()

    if args.print_example:
        print(EXAMPLE_YAML)
        return 0

    if not args.spec:
        ap.error("spec path required (or --print-example)")

    spec = load_spec(args.spec)
    kwargs = spec_to_runner_kwargs(spec)

    if args.dry_run:
        import json
        print(json.dumps(kwargs, indent=2, default=str))
        return 0

    # Build the evaluation object from spec
    from evaluation.runner import run
    from evaluation.contracts import Signal, EventSet, TradeRule
    from evaluation import adapters

    cache = None
    obj = None

    if spec.signal:
        if spec.signal.source == "parquet":
            import pandas as pd
            frame = pd.read_parquet(spec.signal.parquet_path)
            obj = Signal(name=spec.signal.name, frame=frame,
                         lag_days=spec.signal.lag_days,
                         direction=spec.signal.direction)
        elif spec.signal.source == "signal-panel":
            symbols = spec.universe.symbols
            eligible = None
            if spec.universe.exclude_otc or spec.universe.min_dollar_volume is not None or spec.universe.sp500_only:
                from evaluation import universe as _universe
                if not symbols:
                    symbols = _universe.exchange_listed_symbols(
                        exclude_otc=spec.universe.exclude_otc)
                floor = spec.universe.min_dollar_volume if spec.universe.min_dollar_volume is not None else 0.0
                eligible = _universe.point_in_time_eligible(
                    symbols, min_dollar_volume=floor,
                    start=spec.evaluation.start if hasattr(spec.evaluation, 'start') else None,
                    end=spec.evaluation.end if hasattr(spec.evaluation, 'end') else None)
                if spec.universe.sp500_only:
                    sp500 = _universe.sp500_eligible(
                        symbols, start=spec.evaluation.start if hasattr(spec.evaluation, 'start') else None,
                        end=spec.evaluation.end if hasattr(spec.evaluation, 'end') else None)
                    eligible = eligible.merge(
                        sp500.rename(columns={"eligible": "sp500_eligible"}),
                        on=["symbol", "date"], how="left")
                    eligible["sp500_eligible"] = eligible["sp500_eligible"].fillna(False)
                    eligible["eligible"] = (eligible["eligible"] & eligible["sp500_eligible"])
                    eligible = eligible.drop(columns=["sp500_eligible"])
            obj = adapters.from_signal_panel(factor=spec.signal.factor,
                                             symbols=symbols,
                                             start=getattr(spec.evaluation, 'start', None),
                                             end=getattr(spec.evaluation, 'end', None),
                                             eligible=eligible)
        elif spec.signal.source == "sentiment":
            obj = adapters.from_sentiment(start=getattr(spec.evaluation, 'start', None),
                                          end=getattr(spec.evaluation, 'end', None))
        elif spec.signal.source == "rating":
            obj = adapters.from_rating_history(signal_col=spec.signal.signal_col,
                                               symbols=spec.universe.symbols,
                                               price_table=spec.evaluation.price_table,
                                               start=getattr(spec.evaluation, 'start', None),
                                               end=getattr(spec.evaluation, 'end', None))
        elif spec.signal.source == "rating-changes":
            obj = adapters.from_rating_changes(symbols=spec.universe.symbols,
                                               start=getattr(spec.evaluation, 'start', None) or "2000-01-01",
                                               end=getattr(spec.evaluation, 'end', None),
                                               min_step=spec.signal.min_step,
                                               price_table=spec.evaluation.price_table)
        else:
            raise ValueError(f"Unknown signal source: {spec.signal.source}")

    elif spec.events:
        if spec.events.source == "parquet":
            import pandas as pd
            frame = pd.read_parquet(spec.events.parquet_path)
            obj = EventSet(name=spec.events.name, frame=frame,
                           lag_days=spec.events.lag_days,
                           min_events=spec.events.min_events)
        else:
            raise ValueError(f"Unknown events source: {spec.events.source}")

    elif spec.trade_rule:
        if spec.trade_rule.source == "tv-rule":
            obj = adapters.tv_threshold_rule()
            cache = adapters.rating_cache(symbols=spec.trade_rule.symbols,
                                          price_table=spec.trade_rule.price_table,
                                          start=spec.trade_rule.start,
                                          end=spec.trade_rule.end)
        else:
            raise ValueError(f"Unknown trade_rule source: {spec.trade_rule.source}")

    else:
        raise ValueError("Spec must contain one of: signal, events, trade_rule")

    # Run evaluation
    res = run(obj, cache=cache, **kwargs)

    # Print summary
    if spec.signal:
        from evaluate import _print_signal_summary
        _print_signal_summary(res)
    elif spec.events:
        from evaluate import _print_events_summary
        _print_events_summary(res)
    else:
        from evaluate import _print_trades_summary
        _print_trades_summary(res)

    print(f"artifacts: {res['out_dir']}")
    print(f"registry rows written: {res['rows_written']}")

    return 0 if res["n_evaluations"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())