"""
strategies/stage3.py -- Stage 3 (development test) runner for the TV strategy
catalog campaign. See experiments/2026-08-11_tv-strategy-catalog-preregistration.md
section 5. For each admitted, Stage-2-translated strategy (strategies.ports.load_rule
where a hand port exists -- translation_verified="unit_tested" -- else
strategies.pine_bridge.load_pine_script_rule -- "unverified"), runs the TradeRule
on the DEVELOPMENT split only and computes the primary endpoint pnl_p via
evaluation.stats.permutation_trades, net of transaction costs.

Costs are not modeled anywhere in evaluation/trades.py or evaluation/stats.py
(confirmed by inspection: no cost_bps/spread_bps parameter in either module).
cost_adjusted() below monkeypatches evaluation.trades.simulate_symbol for the
duration of a call so every realized trade carries a constant round-trip
deduction on its own notional. Both evaluation.trades.simulate() (calls
simulate_symbol as a bare module-global name, resolved at call time) and
evaluation.stats.permutation_trades() (does `from evaluation import trades as tr`
then `tr.simulate_symbol(...)`, also resolved at call time) see the patched
version without either module being edited. Signal generation is untouched --
rule_flags() runs on the unmodified close series, so entries/exits are unaffected;
only realized P&L is.

Results are provisional=True and stage="stage3" for every row: Stage 4 (BH-FDR)
is computed campaign-wide, once, at campaign close -- never per batch.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import query as q
from analytics.technical import _load_ohlcv
from evaluation import registry as ev_registry
from evaluation import stats as ev_stats
from evaluation import trades as ev_trades
from evaluation.universe import clean_symbols
from strategies import pine_bridge
from strategies import ports as strategy_ports
from strategies.screen import screen_source

TV_SCRIPTS_DIR = "storage/tv_scripts"
ROSTER_GLOB = os.path.join(TV_SCRIPTS_DIR, "_roster_*.txt")
PRICE_TABLE = "yfinance_universe_prices"
DEV_END = "2017-12-31"          # dev-split boundary for the 25% holdout symbols
PRICE_FLOOR = 5.0                # entry-side gate, mirrors the 2026-08-08 precedent
N_PERM = 200
SEED = 0
PRIMARY_COST_BPS = 10.0
SENSITIVITY_COST_BPS = (5.0, 20.0)


# --------------------------------------------------------------- roster / admission

# Roster-declared slugs don't always match the saved .pine filename (some were
# renamed during collection, e.g. njrv2enc_supertrend_with_entry_tp1_tp2_and_tp3
# -> supertrend_entry_tp123.pine) -- so admission is determined by re-running
# strategies.screen.screen_source() directly against each collected file, not
# by parsing roster slugs. The automated screener's only known false positives
# are these 3 unconfirmed_htf hand-overrides, confirmed safe and logged in
# SESSION_NOTES_2026-08-12_tv-catalog.md sessions 3-4 (verified again here by
# grepping the roster notes verbatim -- each says "manual override... no
# repaint risk"). Every other screen_source() exclusion matches a roster note
# that says "screened OUT" for the same reason, so it is not relitigated here.
MANUAL_OVERRIDE_ADMIT = {
    "ras16l2w_bvol_early_entry",
    "xslyyowi_sector_rotation_momentum_framework",
    "jcysz6ni_mtf_sma_crossover_strategy",
}


def admitted_slugs() -> "dict[str, str]":
    """{slug: note} for every collected .pine file that is Stage-1 admitted,
    either automatically (screen_source().admitted) or via MANUAL_OVERRIDE_ADMIT."""
    out: "dict[str, str]" = {}
    for path in sorted(glob.glob(os.path.join(TV_SCRIPTS_DIR, "*.pine"))):
        slug = os.path.splitext(os.path.basename(path))[0]
        with open(path, "r", encoding="utf-8") as f:
            result = screen_source(f.read(), script_name=slug)
        if result.admitted:
            out[slug] = "admitted (auto)"
        elif slug in MANUAL_OVERRIDE_ADMIT:
            out[slug] = f"admitted (manual override of {result.excluded_reason})"
    return out


# --------------------------------------------------------------- translation (Stage 2)

def load_rule_for(slug: str):
    """Hand-verified port when one exists, else the generic pine_bridge fallback."""
    ported = {info.slug for info in strategy_ports.all_ports()}
    if slug in ported:
        return strategy_ports.load_rule(slug), "unit_tested"
    return pine_bridge.load_pine_script_rule(slug, tv_scripts_dir=TV_SCRIPTS_DIR), "unverified"


def with_price_floor(rule, floor: float = PRICE_FLOOR):
    """Entry-side `close >= floor` gate (preregistration section 2)."""
    orig_entries = rule.entries
    gated = replace(rule, entries=lambda d: orig_entries(d) & (d["close"] >= floor))
    if rule.side == "both":
        orig_short = rule.short_entries
        gated = replace(gated, short_entries=lambda d: orig_short(d) & (d["close"] >= floor))
    return gated


# --------------------------------------------------------------- dev/holdout split

def _is_holdout_symbol(symbol: str) -> bool:
    """sha256(symbol) % 4 == 0, per the preregistration's split rule (~25% of
    symbols). int(hexdigest, 16) % 4 is the concrete reading of "sha256(symbol) % 4"
    -- there's no prior-art modulo-on-hash code in this repo to copy verbatim, so
    this is made explicit here rather than left implicit."""
    digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()
    return int(digest, 16) % 4 == 0


def dev_holdout_symbols(symbols: "list[str]"):
    holdout = sorted(s for s in symbols if _is_holdout_symbol(s))
    holdout_set = set(holdout)
    dev = sorted(s for s in symbols if s not in holdout_set)
    return dev, holdout


def build_dev_cache(symbols: "list[str]", price_table: str = PRICE_TABLE) -> dict:
    """DEVELOPMENT split: full history for the 75% dev symbols, capped at
    DEV_END for the 25% holdout symbols -- their post-2018 data is reserved for
    the one-shot Stage 5 holdout test and must not be touched here."""
    dev_symbols, holdout_symbols = dev_holdout_symbols(symbols)
    cache = {}
    for sym in dev_symbols:
        df = _load_ohlcv(sym, price_table, start=None, end=None)
        if df is not None and not df.empty:
            cache[sym] = df
    for sym in holdout_symbols:
        df = _load_ohlcv(sym, price_table, start=None, end=DEV_END)
        if df is not None and not df.empty:
            cache[sym] = df
    return cache


_CACHE_SINGLETON: dict = {}   # price_table -> cache, so N strategies share one load


def dev_cache(price_table: str = PRICE_TABLE) -> dict:
    if price_table not in _CACHE_SINGLETON:
        symbols = q.symbols(price_table)
        symbols = clean_symbols(symbols, price_table=price_table)
        _CACHE_SINGLETON[price_table] = build_dev_cache(symbols, price_table)
    return _CACHE_SINGLETON[price_table]


# --------------------------------------------------------------- cost model

@contextmanager
def cost_adjusted(cost_bps_side: float):
    orig = ev_trades.simulate_symbol
    sig = inspect.signature(orig)
    round_trip_rate = 2.0 * cost_bps_side / 1e4

    def patched(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        notional = bound.arguments["notional"]
        rows = orig(*args, **kwargs)
        cost_dollars = notional * round_trip_rate
        for r in rows:
            r["pnl_dollars"] = round(r["pnl_dollars"] - cost_dollars, 2)
            r["pnl_pct"] = round(r["pnl_pct"] - 100 * round_trip_rate, 3)
        return rows

    ev_trades.simulate_symbol = patched
    try:
        yield
    finally:
        ev_trades.simulate_symbol = orig


# --------------------------------------------------------------- descriptive stats
# Preregistration section 4: these are descriptive only, never trigger promotion.

def _profit_factor(trades: pd.DataFrame):
    if trades.empty:
        return None
    gains = trades.loc[trades["pnl_dollars"] > 0, "pnl_dollars"].sum()
    losses = -trades.loc[trades["pnl_dollars"] < 0, "pnl_dollars"].sum()
    if losses == 0:
        return None
    return round(float(gains / losses), 3)


def _trade_sharpe(trades: pd.DataFrame):
    """Per-trade (not annualized) Sharpe -- descriptive only."""
    if len(trades) < 2:
        return None
    pct = trades["pnl_pct"].to_numpy()
    sd = pct.std(ddof=1)
    if sd == 0:
        return None
    return round(float(pct.mean() / sd), 3)


def _max_drawdown_pct(trades: pd.DataFrame):
    """Max drawdown of a naive trade-return-chained equity curve (not
    portfolio-level, since the engine doesn't track concurrent capital across
    symbols) -- descriptive only. Meaningless above a few thousand trades:
    chaining tens of thousands of unrelated per-symbol returns multiplicatively
    numerically underflows toward -100% even with a tiny average edge decay,
    so it's suppressed rather than reported as a real number."""
    if trades.empty or len(trades) > 5000:
        return None
    ordered = trades.sort_values("exit_date")
    equity = (1 + ordered["pnl_pct"].to_numpy() / 100).cumprod()
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1
    return round(float(dd.min() * 100), 2)


# --------------------------------------------------------------- per-strategy run

def run_strategy(slug: str, meta: dict, roster_note: str, cache: dict,
                 n_perm: int = N_PERM, seed: int = SEED) -> dict:
    rule, translation_verified = load_rule_for(slug)
    rule = with_price_floor(rule)

    pine_path = os.path.join(TV_SCRIPTS_DIR, f"{slug}.pine")
    with open(pine_path, "r", encoding="utf-8") as f:
        screen = screen_source(f.read(), script_name=slug)

    with cost_adjusted(PRIMARY_COST_BPS):
        trades_df = ev_trades.simulate(rule, cache)
        summary = ev_trades.trade_summary(trades_df)
        perm = ev_stats.permutation_trades(rule, cache, n_perm=n_perm, seed=seed)

    perm_sens = {}
    for bps in SENSITIVITY_COST_BPS:
        with cost_adjusted(bps):
            perm_sens[bps] = ev_stats.permutation_trades(rule, cache, n_perm=n_perm, seed=seed)

    pnl_p = perm.get("pnl_p")
    pnl_p_5 = perm_sens[5.0].get("pnl_p")
    pnl_p_20 = perm_sens[20.0].get("pnl_p")
    cost_fragile = (
        None if None in (pnl_p_5, pnl_p_20)
        else bool((pnl_p_5 < 0.05) != (pnl_p_20 < 0.05))
    )

    return {
        "strategy_id": slug,
        "tv_url": meta.get("tv_url"),
        "tv_author": meta.get("tv_author"),
        "tv_script_name": meta.get("tv_script_name"),
        "tv_boosts": meta.get("tv_boosts"),
        "tv_views": meta.get("tv_views"),
        "license": meta.get("license"),
        "collected_at": meta.get("collected_at"),
        "mechanism_family": screen.mechanism_family,
        "param_count": screen.param_count,
        "screen_status": "admitted" if screen.admitted else f"excluded:{screen.excluded_reason}",
        "roster_note": roster_note,
        "translation_verified": translation_verified,
        "n_trades": summary.get("n_trades", 0),
        "win_rate": summary.get("win_rate_pct"),
        "profit_factor": _profit_factor(trades_df),
        "sharpe": _trade_sharpe(trades_df),
        "max_dd": _max_drawdown_pct(trades_df),
        "median_hold": summary.get("median_days_held"),
        "total_pnl_net": summary.get("total_pnl_dollars"),
        "pnl_p": pnl_p,
        "pnl_p_5bps": pnl_p_5,
        "pnl_p_20bps": pnl_p_20,
        "cost_fragile": cost_fragile,
        "bh_q": None,             # Stage 4: computed campaign-wide at close
        "fdr_pass": None,
        "holdout_pnl_p": None,    # Stage 5: one-shot, not touched here
        "holdout_run_ts": None,
        "provisional": True,
        "stage": "stage3",
        "n_symbols_dev": summary.get("n_symbols"),
    }


# --------------------------------------------------------------- registry rows

def registry_rows_for(row: dict, run_id: str, universe_hash: str, date_range: str,
                      created_at: str) -> pd.DataFrame:
    """Flatten one catalog row into evaluation/registry.py's (run, evaluation,
    horizon, statistic) shape, mirroring evaluate.py's runner._stat_rows pattern
    so this campaign's rows count toward the registry-wide trial population."""
    stats = {
        "n_trades": row["n_trades"], "win_rate": row["win_rate"],
        "profit_factor": row["profit_factor"], "sharpe": row["sharpe"],
        "max_dd": row["max_dd"], "median_hold": row["median_hold"],
        "total_pnl_net": row["total_pnl_net"], "pnl_p": row["pnl_p"],
        "pnl_p_5bps": row["pnl_p_5bps"], "pnl_p_20bps": row["pnl_p_20bps"],
    }
    recs = []
    for statistic, value in stats.items():
        if value is None:
            continue
        recs.append({
            "run_id": run_id, "input_name": f"pine_{row['strategy_id']}",
            "input_type": "trade_rule", "evaluation": "tv_strategy_catalog_stage3",
            "horizon": -1, "statistic": statistic, "value": value,
            "n": row["n_trades"], "universe_hash": universe_hash,
            "date_range": date_range, "created_at": created_at,
        })
    return pd.DataFrame(recs, columns=ev_registry.COLUMNS)


# --------------------------------------------------------------- main

def run_all(n_perm: int = N_PERM, seed: int = SEED, write_registry: bool = True,
           limit: "int | None" = None) -> pd.DataFrame:
    slugs = admitted_slugs()
    cache = dev_cache()
    uhash = ev_registry.universe_hash(cache.keys())
    date_range = f"..{DEV_END} (+75% symbols full history)"
    created_at = datetime.now(timezone.utc).isoformat()
    run_id = ev_registry.new_run_id()

    ordered = sorted(slugs.items())
    total = len(ordered) if limit is None else min(limit, len(ordered))
    rows = []
    for i, (slug, note) in enumerate(ordered):
        if limit is not None and i >= limit:
            break
        t0 = datetime.now(timezone.utc)
        print(f"[{i + 1}/{total}] {slug} -- starting", flush=True)
        meta_path = os.path.join(TV_SCRIPTS_DIR, f"{slug}.meta.json")
        meta = {}
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        try:
            row = run_strategy(slug, meta, note, cache, n_perm=n_perm, seed=seed)
        except Exception as e:
            row = {"strategy_id": slug, "stage": "stage3", "error": f"{type(e).__name__}: {e}"}
        rows.append(row)
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        if "error" in row:
            print(f"[{i + 1}/{total}] {slug} -- ERROR after {elapsed:.0f}s: {row['error']}", flush=True)
        else:
            print(f"[{i + 1}/{total}] {slug} -- done in {elapsed:.0f}s: "
                  f"n_trades={row['n_trades']} pnl_p={row['pnl_p']}", flush=True)
            if write_registry:
                reg_rows = registry_rows_for(row, run_id, uhash, date_range, created_at)
                if not reg_rows.empty:
                    ev_registry.append(reg_rows)

    return pd.DataFrame(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-registry", action="store_true")
    ap.add_argument("--n-perm", type=int, default=N_PERM)
    args = ap.parse_args()
    out = run_all(n_perm=args.n_perm, write_registry=not args.no_registry, limit=args.limit)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    cols = [c for c in ["strategy_id", "translation_verified", "n_trades",
                        "total_pnl_net", "pnl_p", "cost_fragile", "error"]
            if c in out.columns]
    print(out[cols])
