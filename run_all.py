#!/usr/bin/env python3
"""
Unified Pipeline Runner — runs all financial data pipelines in dependency order.

Stages
------
  Stage 1  — Free/public sources (FRED, EIA, yfinance, Finnhub, SEC EDGAR, CFTC)
  Stage 2  — Schwab-authenticated (prices, ETFs, real-time quotes, options chains)
  Stage 3  — Derived (synthetic options uses Stage 2 prices; news sentiment uses Stage 1 news)

Usage
-----
  python run_all.py                        # incremental run (all stages)
  python run_all.py --backfill             # full available history
  python run_all.py --stage 1              # free/public sources only
  python run_all.py --only commodity_macro,gas_prices,finnhub
  python run_all.py --skip fundamentals,synthetic_options
  python run_all.py --dry-run              # print commands, don't execute
  python run_all.py --no-validate          # skip post-run validation
"""

import argparse
import datetime
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

from validate import validate_table

load_dotenv()


# ── Pipeline registry ─────────────────────────────────────────────────────────

@dataclass
class PipelineSpec:
    name:             str
    file:             str
    desc:             str
    stage:            int
    tables:           list = field(default_factory=list)
    requires_env:     list = field(default_factory=list)
    backfill_args:    list = field(default_factory=list)
    incremental_args: list = field(default_factory=list)
    timeout:          int  = 600   # seconds; override for slow pipelines


PIPELINES: list[PipelineSpec] = [
    # ── Stage 1 — Free / public sources ────────────────────────────────────────
    PipelineSpec(
        name="commodity_macro",
        file="commodity_macro_pipeline.py",
        desc="FRED commodities, macro indicators, credit spreads",
        stage=1,
        tables=["commodities", "macro"],
        requires_env=["FRED_API_KEY"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="gas_prices",
        file="gas_price_pipeline.py",
        desc="EIA spot and retail gas/diesel prices",
        stage=1,
        tables=["gas_spot", "gas_retail"],
        requires_env=["EIA_API_KEY"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="futures",
        file="futures_pipeline.py",
        desc="yfinance futures OHLCV + CFTC COT positions",
        stage=1,
        tables=["futures", "cot"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="short_interest",
        file="short_interest_pipeline.py",
        desc="yfinance + FINRA Reg SHO + SEC fails-to-deliver",
        stage=1,
        tables=["short_interest", "finra_short_interest", "sec_ftd"],
        backfill_args=["--source", "all"],
        incremental_args=["--source", "all"],
    ),
    PipelineSpec(
        name="finnhub",
        file="finnhub_pipeline.py",
        desc="Finnhub profile, quotes, metrics, recommendations, news",
        stage=1,
        tables=[
            "finnhub_profile", "finnhub_quotes", "finnhub_metrics",
            "finnhub_recommendations", "finnhub_price_targets",
            "finnhub_upgrades", "finnhub_news",
        ],
        requires_env=["FINNHUB_API_KEY"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="finnhub_events",
        file="finnhub_events_pipeline.py",
        desc="Finnhub earnings calendar + insider transactions",
        stage=1,
        tables=["earnings_calendar", "insider_transactions"],
        requires_env=["FINNHUB_API_KEY"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="dividends",
        file="dividend_pipeline.py",
        desc="Finnhub per-symbol cash dividend history",
        stage=1,
        tables=["dividends"],
        requires_env=["FINNHUB_API_KEY"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="fundamentals",
        file="fundamentals_pipeline.py",
        desc="SEC EDGAR company fundamentals (DJI components)",
        stage=1,
        tables=["fundamentals_annual", "fundamentals_quarterly"],
        requires_env=["EDGAR_USER_AGENT"],
        backfill_args=["--quarters", "40"],   # ~10 years of quarterly data
        timeout=1800,                          # large download; allow 30 min
    ),
    # ── Stage 2 — Schwab-authenticated ─────────────────────────────────────────
    PipelineSpec(
        name="prices",
        file="price_history_pipeline.py",
        desc="Schwab daily OHLCV for DJI components",
        stage=2,
        tables=["prices"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="sector_etfs",
        file="sector_etf_pipeline.py",
        desc="Schwab daily OHLCV for SPDR sector ETFs + broad indexes",
        stage=2,
        tables=["sector_etfs"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
        backfill_args=["--backfill"],
    ),
    PipelineSpec(
        name="schwab_quotes",
        file="schwab_quotes_pipeline.py",
        desc="Schwab real-time quote snapshot (DJI + sector ETFs)",
        stage=2,
        tables=["schwab_quotes"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
    ),
    PipelineSpec(
        name="schwab_options",
        file="schwab_options_pipeline.py",
        desc="Schwab options chains with full greeks (delta/gamma/theta/vega/rho)",
        stage=2,
        tables=["schwab_options"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
    ),
    PipelineSpec(
        name="options_chain",
        file="options_chain_pipeline.py",
        desc="Schwab options metrics and chain snapshot",
        stage=2,
        tables=["options_metrics", "options_chain"],
        requires_env=["SCHWAB_API_KEY", "SCHWAB_APP_SECRET"],
    ),
    # ── Stage 3 — Derived (depends on Stage 1/2 output) ────────────────────────
    PipelineSpec(
        name="synthetic_options",
        file="synthetic_options_pipeline.py",
        desc="BSM/BS2002 synthetic option pricing (requires prices table)",
        stage=3,
        tables=["synthetic_options"],
        backfill_args=["--backfill"],
        timeout=1200,
    ),
    PipelineSpec(
        name="news_sentiment",
        file="news_sentiment_pipeline.py",
        desc="Claude API sentiment scoring of Finnhub news (requires finnhub table)",
        stage=3,
        tables=["news_sentiment"],
        requires_env=["ANTHROPIC_API_KEY"],
        backfill_args=["--backfill"],
    ),
]


# ── Run result ─────────────────────────────────────────────────────────────────

@dataclass
class RunResult:
    name:     str
    status:   str    # PASS | FAIL | SKIP | DRY RUN
    duration: float  # seconds
    note:     str    # skip reason or error context
    val_warnings: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_env(spec: PipelineSpec) -> str | None:
    """Return a skip reason if any required env var is missing, else None."""
    missing = [v for v in spec.requires_env if not os.environ.get(v)]
    if missing:
        return f"missing env: {', '.join(missing)}"
    return None


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def run_pipeline(
    spec: PipelineSpec,
    backfill: bool,
    dry_run: bool,
    validate: bool,
) -> RunResult:
    skip_reason = _check_env(spec)
    if skip_reason:
        print(f"  SKIP -- {skip_reason}")
        return RunResult(spec.name, "SKIP", 0.0, skip_reason)

    script = os.path.join(REPO_ROOT, spec.file)
    if not os.path.exists(script):
        reason = f"{spec.file} not found"
        print(f"  SKIP -- {reason}")
        return RunResult(spec.name, "SKIP", 0.0, reason)

    cmd = [sys.executable, script]
    cmd += spec.backfill_args if backfill else spec.incremental_args

    if dry_run:
        cmd_str = " ".join(os.path.basename(c) if i < 2 else c for i, c in enumerate(cmd))
        print(f"  DRY RUN: {cmd_str}")
        return RunResult(spec.name, "DRY RUN", 0.0, cmd_str)

    t0 = time.time()
    try:
        result = subprocess.run(cmd, timeout=spec.timeout)
        duration = time.time() - t0
        if result.returncode != 0:
            return RunResult(spec.name, "FAIL", duration, f"exit {result.returncode}")
    except subprocess.TimeoutExpired:
        duration = time.time() - t0
        return RunResult(spec.name, "FAIL", duration, f"timed out after {spec.timeout}s")
    except Exception as exc:
        duration = time.time() - t0
        return RunResult(spec.name, "FAIL", duration, str(exc))

    # Post-run validation
    val_warnings = 0
    if validate and spec.tables:
        for table in spec.tables:
            vr = validate_table(table)
            if not vr.passed:
                print(f"\n  [VALIDATE] {table}: {len(vr.errors)} error(s)")
                for c in vr.errors:
                    print(f"    {c}")
            elif vr.warnings:
                val_warnings += len(vr.warnings)

    return RunResult(spec.name, "PASS", duration, "", val_warnings)


# ── Summary ────────────────────────────────────────────────────────────────────

def _print_summary(results: list[RunResult], backfill: bool, start_time: float) -> None:
    mode      = "BACKFILL" if backfill else "INCREMENTAL"
    wall_time = _fmt_duration(time.time() - start_time)
    now       = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    icons = {"PASS": "+", "FAIL": "!", "SKIP": "-", "DRY RUN": "?"}

    print(f"\n{'=' * 62}")
    print(f"  Run Summary -- {mode} -- {now}  ({wall_time} total)")
    print(f"{'=' * 62}")

    for r in results:
        icon = icons.get(r.status, "?")
        dur  = _fmt_duration(r.duration) if r.duration else "-"
        warn = f"  [{r.val_warnings} val warn]" if r.val_warnings else ""
        note = f"  {r.note}" if r.note and r.status not in ("PASS",) else ""
        print(f"  {icon} {r.status:8s}  {r.name:28s}  {dur:>6s}{warn}{note}")

    pass_n  = sum(1 for r in results if r.status == "PASS")
    fail_n  = sum(1 for r in results if r.status == "FAIL")
    skip_n  = sum(1 for r in results if r.status == "SKIP")
    total_w = sum(r.val_warnings for r in results)

    print(f"\n  {pass_n} PASS  |  {fail_n} FAIL  |  {skip_n} SKIP", end="")
    if total_w:
        print(f"  |  {total_w} validation warning(s)", end="")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run all financial data pipelines in dependency order.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Pass --backfill to every pipeline that supports it.",
    )
    parser.add_argument(
        "--stage", type=int, choices=[1, 2, 3],
        help="Run only pipelines in the given stage (1=free, 2=Schwab, 3=derived).",
    )
    parser.add_argument(
        "--only",
        help="Comma-separated pipeline names to run (e.g. commodity_macro,finnhub).",
    )
    parser.add_argument(
        "--skip",
        help="Comma-separated pipeline names to skip.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would run without executing.",
    )
    parser.add_argument(
        "--no-validate", action="store_true",
        help="Skip post-run validation checks.",
    )
    args = parser.parse_args()

    # Build filtered pipeline list
    pipelines = list(PIPELINES)
    if args.stage:
        pipelines = [p for p in pipelines if p.stage == args.stage]
    if args.only:
        only_set  = {n.strip() for n in args.only.split(",")}
        pipelines = [p for p in pipelines if p.name in only_set]
        unknown   = only_set - {p.name for p in PIPELINES}
        if unknown:
            print(f"Warning: unknown pipeline names in --only: {sorted(unknown)}")
    if args.skip:
        skip_set  = {n.strip() for n in args.skip.split(",")}
        pipelines = [p for p in pipelines if p.name not in skip_set]
        unknown   = skip_set - {p.name for p in PIPELINES}
        if unknown:
            print(f"Warning: unknown pipeline names in --skip: {sorted(unknown)}")

    if not pipelines:
        print("No pipelines selected. Check --stage / --only / --skip arguments.")
        return 1

    mode = "BACKFILL" if args.backfill else "INCREMENTAL"
    validate = not args.no_validate
    start_time = time.time()

    print(f"\n{'=' * 62}")
    print(f"  Financial Data Pipeline Runner")
    print(f"  Mode: {mode}  |  Pipelines: {len(pipelines)}  |  Validate: {validate}")
    print(f"{'=' * 62}")

    # Stage-grouped run
    current_stage = 0
    results: list[RunResult] = []

    for spec in pipelines:
        if spec.stage != current_stage:
            current_stage = spec.stage
            labels = {1: "Free / Public Sources", 2: "Schwab Authenticated", 3: "Derived Pipelines"}
            print(f"\n-- Stage {current_stage}: {labels.get(current_stage, '')} --")

        print(f"\n>>  {spec.name}  --  {spec.desc}")
        result = run_pipeline(spec, args.backfill, args.dry_run, validate)
        results.append(result)

    _print_summary(results, args.backfill, start_time)

    return 0 if all(r.status in ("PASS", "SKIP", "DRY RUN") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
