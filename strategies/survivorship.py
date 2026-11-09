"""
strategies/survivorship.py -- re-score the TV catalog on a complete CRSP population.

`yfinance_universe_prices` holds only companies alive today, picked from today's Russell
3000, so a strategy that buys falling stocks is only ever shown the ones that recovered.
This module scores every tested strategy on all CRSP common stocks, alive or dead. Its rules
are set by the preregistration amendment on survivorship, and a real run is refused until
that amendment is in force. Every method here is chosen to measure the truth: where the
truth is unknown, the assumption must not favour the strategy.

Things the Stage 3 path gets wrong that this path corrects:

  1. evaluation.trades drops a position still open when a symbol's data ends. Here it is
     closed at the delisting value, or at the last price if the company did not delist. The
     close is added AFTER the signals are shifted, so every shifted copy faces the same end.
  2. Market impact falls to 0 where volume is zero or missing, so the least liquid stocks can
     be the cheapest. Here such an entry does not fill, and such an exit pays the most recent
     measured impact. No-trade days count as zero volume in the trailing average.
  3. The $5 floor read a split-adjusted price. Here it reads the real traded price.
  4. Fills could land on a quote nobody traded at. CRSP no-trade rows are removed here.
  5. Shorts paid no borrowing cost. Here they pay one, and get no credit for a delisting fall.
  6. clean_symbols() deletes real crashes. It is not used here, except in a reported-only run.

evaluate_group(corrected=False) keeps the Stage 3 scoring, only so a test can show this code
reproduces the registry on Stage 3 inputs. That checks the code, nothing more.

Output is plain data. CRSP lives under storage/licensed/, never storage/curated/, which is
published publicly. CRSP may not be redistributed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NamedTuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation import execution as ev_execution
from evaluation import registry as ev_registry
from evaluation import stats as ev_stats
from evaluation import trades as ev_trades
from strategies.stage3 import DEV_END, PRICE_FLOOR, SEED, _is_holdout_symbol

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRSP_ROOT = os.path.join("storage", "licensed", "crsp")
EVALUATION_NAME = "tv_strategy_catalog_survivorship_v1"
HOLDOUT_START = "2018-01-01"
#: Lowers the smallest possible p-value to about 0.001; 200 copies stop at about 0.005,
#: above the 0.10 / 35 a lone strategy needs to pass BH.
N_PERM = 1000
BH_Q = 0.10
SUCCESS_P = 0.05
EXCHANGES = (1, 2, 3)           # NYSE, AMEX, Nasdaq
NASDAQ_EXCHCD = 3
ACTIVE_CODE = 100
SCENARIOS =("missing_zero", "shumway", "missing_wipeout")
PRIMARY_SCENARIO = "shumway"
WIPEOUT_SCENARIO = "missing_wipeout"
#: Annual short-borrowing fee in bps (amendment change 9, approved 2026-11-08): the mean
#: borrower-side fee on anomaly short legs, 2.70%, CRSP common stocks 2006-2020, from
#: Muravyev, Pearson & Pollet, "Anomalies and Their Short-Sale Costs", J. Finance 2025.
#: crsp_config() still refuses to run if this is ever set back to None.
BORROW_FEE_BPS = 270.0
#: Same threshold as evaluation.universe.flag_price_jumps.
MAX_ABS_LOG_RETURN = 1.0986
#: Closer than this is the same company, further apart may be a recycled ticker.
OVERLAP_DAYS = 370
#: The engine refuses a non-positive exit, which would delete a -100% outcome.
_MIN_EXIT_FRACTION = 1e-6
ADV_WINDOW = ev_trades.ADV_WINDOW

PREREG_FILE = "2026-08-11_tv-strategy-catalog-preregistration.md"
DRAFT_FILE = "DRAFT_tv-catalog-survivorship-amendment.md"
AMENDMENT_MARKER = "all tested strategies re-scored on a complete CRSP population"

_CRSP_COLUMNS = ("date", "prc", "openprc", "askhi", "bidlo", "vol", "cfacpr", "cfacshr")
_COMPANY_COLUMNS = ("permno", "ticker", "begdt", "dlstdt", "dlstcd", "dlret", "exchcd")


def assert_amendment_in_force(root: str = _REPO_ROOT) -> None:
    """Refuse a real run until the rules are fixed where they cannot be changed quietly."""
    exp = os.path.join(root, "experiments")
    if os.path.exists(os.path.join(exp, DRAFT_FILE)):
        raise RuntimeError("the survivorship amendment is still a draft; commit it before scoring")
    path = os.path.join(exp, PREREG_FILE)
    with open(path, encoding="utf-8") as fh:
        if AMENDMENT_MARKER not in fh.read():
            raise RuntimeError(f"the survivorship amendment is not in {PREREG_FILE}; commit it before scoring")


def crsp_config(borrow_fee_bps=None) -> "ev_execution.ExecutionConfig":
    """The campaign's cost formula plus a borrowing fee on shorts."""
    fee = BORROW_FEE_BPS if borrow_fee_bps is None else borrow_fee_bps
    if fee is None:
        raise RuntimeError("no approved short-borrowing fee: set BORROW_FEE_BPS from a published "
                           "source and record it in the amendment before running")
    return ev_execution.ExecutionConfig(
        name="tv_campaign_crsp",
        costs=ev_execution.CostModel(commission_bps=1.0, impact_model="sqrt_law", impact_coeff=0.6,
                                     borrow_fee_bps=float(fee)))


# --------------------------------------------------------------- delisting returns

def _valid_return(dlret) -> bool:
    """Below -1 is one of CRSP's numeric missing codes."""
    return dlret is not None and not pd.isna(dlret) and bool(dlret >= -1.0)


def return_is_assumed(dlret, dlstcd) -> bool:
    """True when the terminal return depends on a scenario: no valid return, and not a
    merger or exchange, whose holders are paid. Codes under 400 are active, merged or exchanged."""
    return not _valid_return(dlret) and int(dlstcd) >= 400


def terminal_return(dlret, dlstcd, exchcd, scenario: str = PRIMARY_SCENARIO) -> float:
    """Return from the last real close to the delisting value. A real CRSP return always
    wins. `shumway` is the published estimate: -30% on NYSE/AMEX and -55% on Nasdaq."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")
    if _valid_return(dlret):
        return float(dlret)
    if not return_is_assumed(dlret, dlstcd) or scenario == "missing_zero":
        return 0.0
    if scenario == WIPEOUT_SCENARIO:
        return -1.0
    return -0.55 if int(exchcd) == NASDAQ_EXCHCD else -0.30


# --------------------------------------------------------------- liquidity, floor, costs

def _measured_rate(df: pd.DataFrame, notional: float) -> pd.Series:
    """Square-root-law impact on trailing measures, NaN wherever liquidity is unmeasured."""
    import event_backtest as eb
    vol = df["close"].pct_change().rolling(ADV_WINDOW, min_periods=ADV_WINDOW).std(ddof=1).shift(1) * 1e4
    adv = df["adv"].where(df["adv"] > 0)
    return eb.ADV_SQRT_LAW_K * vol * np.sqrt(notional / adv) / 1e4


def impact_rates(df: pd.DataFrame, notional: float) -> np.ndarray:
    """Per-bar impact. An unmeasured bar pays the most recent measured value, never 0."""
    return _measured_rate(df, notional).ffill().to_numpy(dtype=float)


def _entry_gate(df: pd.DataFrame, notional: float, floor: float):
    """(gate, floor_ok, liquid). gate[i] says a signal on bar i may enter: its real price is
    at the floor, and the bar it fills on has liquidity that is measured and can hold it."""
    n = len(df)
    real = df["real_close"].to_numpy(dtype=float)
    adv = df["adv"].to_numpy(dtype=float)
    rate = _measured_rate(df, notional).to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        floor_ok = real >= floor
        liquid = np.isfinite(rate) & (adv >= notional)
    gate = np.zeros(n, dtype=bool)
    gate[:-1] = floor_ok[:-1] & liquid[1:]
    return gate, floor_ok, liquid


# --------------------------------------------------------------- closing at the end of a company

def _extend_for_death(index, close, volume, le, lx, se, sx, ret: float):
    """Append two terminal-value bars and force every open position out onto them.

    Two bars because the engine fills an exit on the bar after its signal and only looks
    for exits after the entry bar: a position entered on the last real bar can only be
    closed by a signal on the first new bar, filled on the second. Entries on the last
    real bar are removed since they have no real close left to fill at.
    """
    n = len(close)
    last = float(close[-1])
    if not np.isfinite(last) or last <= 0:
        raise ValueError("a company's frame must end on a valid price")
    value = max(last * (1.0 + ret), last * _MIN_EXIT_FRACTION)
    new_dates = pd.DatetimeIndex([index[-1] + pd.offsets.BDay(k) for k in (1, 2)])

    def entries(a):
        out = np.concatenate([np.asarray(a, dtype=bool), [False, False]])
        out[n - 1] = False
        return out

    def exits(a):
        out = np.concatenate([np.asarray(a, dtype=bool), [True, True]])
        out[n - 1] = True
        return out

    new_volume = None if volume is None else np.concatenate(
        [np.asarray(volume, dtype=float), [np.nan, np.nan]])
    return (index.append(new_dates),
            np.concatenate([np.asarray(close, dtype=float), [value, value]]),
            new_volume, entries(le), exits(lx), entries(se), exits(sx))


def _settle_short_closes(rows: list, last_date, last: float, value: float, notional: float) -> list:
    """A short closed by a delisting gets no credit for the fall, and still pays a rise."""
    if value >= last:
        return rows
    for row in rows:
        if row["side"] == "short" and row["exit_date"] > last_date:
            delta = (value - last) / row["entry_price"]
            row["exit_price"] = last
            row["pnl_dollars"] = round(row["pnl_dollars"] + notional * delta, 2)
            row["pnl_pct"] = round(row["pnl_pct"] + 100 * delta, 3)
    return rows


class _Item(NamedTuple):
    idx: int
    sym: str
    index: pd.DatetimeIndex
    close: np.ndarray
    volume: "np.ndarray | None"
    le: np.ndarray
    lx: np.ndarray
    se: np.ndarray
    sx: np.ndarray
    ret: "float | None"
    gate: "np.ndarray | None"       # corrected path: the entry gate
    impact: "np.ndarray | None"     # corrected path: per-bar impact
    floor: "float | None"           # Stage 3 path: the shifted-entry floor on adjusted prices
    floor_changed: int
    blocked_liquidity: int


def _prepare(rule, df: pd.DataFrame, sym: str, ret, notional: float, floor, idx: int, *, corrected: bool) -> _Item:
    """Everything one company needs, computed once. The rule sees real bars only."""
    index = df.index
    close = df["close"].to_numpy(dtype=float)
    le, lx, se, sx = ev_trades.rule_flags(rule, df)
    if not corrected:
        volume = df["volume"].to_numpy(dtype=float) if "volume" in df.columns else None
        return _Item(idx, sym, index, close, volume, le, lx, se, sx, ret, None, None, floor, 0, 0)
    gate, floor_ok, liquid = _entry_gate(df, notional, floor)
    wanted = np.asarray(le, dtype=bool) | np.asarray(se, dtype=bool)
    with np.errstate(invalid="ignore"):
        changed = int((wanted & (floor_ok != (close >= floor))).sum())
    fill_liquid = np.append(liquid[1:], True)    # a last-bar signal has no fill bar to judge
    blocked = int((wanted & floor_ok & ~fill_liquid).sum())
    return _Item(idx, sym, index, close, None, le & gate, lx, se & gate, sx, ret, gate,
                 impact_rates(df, notional), None, changed, blocked)


def _simulate(item: _Item, le, lx, se, sx, notional: float, config) -> list:
    if item.ret is None:
        return ev_trades.simulate_symbol(item.index, item.close, le, lx, se, sx, item.sym, notional,
                                         config=config, volume=item.volume)
    index2, close2, volume2, le2, lx2, se2, sx2 = _extend_for_death(
        item.index, item.close, item.volume, le, lx, se, sx, item.ret)
    impact = None
    if item.impact is not None:
        extended = np.append(item.impact, [item.impact[-1], item.impact[-1]])
        impact = (extended, extended)
    rows = ev_trades.simulate_symbol(index2, close2, le2, lx2, se2, sx2, item.sym, notional,
                                     config=config, volume=volume2, impact=impact)
    return _settle_short_closes(rows, item.index[-1], float(item.close[-1]), float(close2[-1]), notional)


def simulate_with_death(rule, df: pd.DataFrame, ret, symbol: str, notional: float, *, config=None) -> list:
    """Real trades for one company. `ret` is its terminal return, or None for the plain engine."""
    item = _prepare(rule, df, symbol, ret, notional, None, 0, corrected=False)
    return _simulate(item, item.le, item.lx, item.se, item.sx, notional, config)


def _big_moves(rows: list, item: _Item) -> list:
    """Trades held across a one-day move beyond 3x, including a terminal one."""
    if item.ret is None or not rows:
        return []
    last = float(item.close[-1])
    path = np.append(item.close, max(last * (1.0 + item.ret), last * _MIN_EXIT_FRACTION))
    logs = np.abs(np.diff(np.log(path)))
    out = []
    for row in rows:
        i = int(item.index.searchsorted(row["entry_date"]))
        j = len(item.index) if row["exit_date"] > item.index[-1] else int(item.index.searchsorted(row["exit_date"]))
        if j > i and float(logs[i:j].max()) > MAX_ABS_LOG_RETURN:
            out.append({"symbol": item.sym, "side": row["side"], "entry_date": str(row["entry_date"].date()),
                        "exit_date": str(row["exit_date"].date()), "pnl_dollars": row["pnl_dollars"]})
    return out


# --------------------------------------------------------------- observed plus null

@dataclass
class GroupTotals:
    obs_pnl: float
    obs_n: int
    obs_wins: int
    obs_days: float
    forced_n: int
    forced_pnl: float
    n_symbols: int
    null: np.ndarray            # (n_perm, 4): pnl, trades, wins, days
    short_forced_n: int = 0
    short_forced_pnl: float = 0.0
    floor_changed: int = 0
    blocked_liquidity: int = 0
    big_moves: list = field(default_factory=list)


def _null_totals(chunk: list, perm_seed: int, notional: float, config) -> tuple:
    """One permutation. Same offset stream as ev_stats._shift_null_totals, so any sharding
    gives the same null and the Stage 3 path reproduces Stage 3 v2 exactly."""
    pnl = 0.0
    n_trades = n_wins = 0
    days = 0.0
    for item in chunk:
        n = len(item.index)
        if n < 2:
            continue
        rng = np.random.default_rng((int(perm_seed), int(item.idx)))
        offset = int(rng.integers(1, n))
        ple, plx, pse, psx = ev_stats._shift_flags(item.le, item.lx, item.se, item.sx, item.close, offset, item.floor)
        if item.gate is not None:
            ple = ple & item.gate
            pse = pse & item.gate
        for row in _simulate(item, ple, plx, pse, psx, notional, config):
            pnl += row["pnl_dollars"]
            n_trades += 1
            days += row["days_held"]
            if row["pnl_dollars"] > 0:
                n_wins += 1
    return pnl, n_trades, n_wins, days


def _null_worker(task) -> np.ndarray:
    chunk, seeds, notional, config = task
    return np.array([_null_totals(chunk, int(s), notional, config) for s in seeds], dtype=float)


def evaluate_group(rule, frames: dict, terminals: dict, *, first_idx: int = 0, n_perm: int, seed: int,
                   config, entry_floor, executor=None, n_workers: int = 2, corrected: bool = False,
                   indices: "dict | None" = None) -> GroupTotals:
    """Observed trades and null totals for some companies. `terminals` maps a symbol to its
    terminal return. A symbol's position seeds its offsets and must not depend on how symbols
    were batched: it is `indices[sym]` when given, otherwise a running count from `first_idx`.
    With corrected=True every symbol must have a terminal return; a gap is a bug, not an alive
    company."""
    totals = GroupTotals(0.0, 0, 0, 0.0, 0, 0.0, 0, np.zeros((n_perm, 4)))
    chunk = []
    for i, (sym, df) in enumerate(frames.items()):
        if df.empty or "close" not in df.columns:
            continue
        ret = terminals[sym] if corrected else terminals.get(sym)
        idx = indices[sym] if indices is not None else first_idx + i
        item = _prepare(rule, df, sym, ret, rule.notional, entry_floor, idx, corrected=corrected)
        rows = _simulate(item, item.le, item.lx, item.se, item.sx, rule.notional, config)
        for row in rows:
            totals.obs_pnl += row["pnl_dollars"]
            totals.obs_n += 1
            totals.obs_days += row["days_held"]
            totals.obs_wins += row["pnl_dollars"] > 0
            if ret is not None and row["exit_date"] > df.index[-1]:
                totals.forced_n += 1
                totals.forced_pnl += row["pnl_dollars"]
                if row["side"] == "short":
                    totals.short_forced_n += 1
                    totals.short_forced_pnl += row["pnl_dollars"]
        totals.floor_changed += item.floor_changed
        totals.blocked_liquidity += item.blocked_liquidity
        if corrected:
            totals.big_moves.extend(_big_moves(rows, item))
        chunk.append(item)

    totals.n_symbols = len(chunk)
    seeds = np.random.default_rng(seed).integers(0, 2**31 - 1, size=n_perm)
    if executor is None or len(chunk) < 2:
        totals.null = _null_worker((chunk, seeds, rule.notional, config))
    else:
        parts = [chunk[k::n_workers] for k in range(n_workers)]
        tasks = [(p, seeds, rule.notional, config) for p in parts if p]
        totals.null = sum(executor.map(_null_worker, tasks))
    return totals


def combine(groups: list) -> dict:
    """Pool groups and score them the way ev_stats.circular_shift_trades does."""
    obs_pnl = sum(g.obs_pnl for g in groups)
    obs_n = sum(g.obs_n for g in groups)
    obs_wins = sum(g.obs_wins for g in groups)
    obs_days = sum(g.obs_days for g in groups)
    big = [m for g in groups for m in g.big_moves]
    base = {"n_trades": obs_n, "n_symbols": sum(g.n_symbols for g in groups),
            "forced_closes": sum(g.forced_n for g in groups),
            "forced_close_pnl": round(sum(g.forced_pnl for g in groups), 2),
            "short_forced_closes": sum(g.short_forced_n for g in groups),
            "short_forced_close_pnl": round(sum(g.short_forced_pnl for g in groups), 2),
            "floor_changed": sum(g.floor_changed for g in groups),
            "blocked_liquidity": sum(g.blocked_liquidity for g in groups),
            "big_move_trades": sorted(big, key=lambda m: (m["symbol"], m["entry_date"]))}
    if obs_n == 0:
        return {**base, "obs_pnl_dollars": 0.0, "pnl_p": None, "exposure_mismatch": True,
                "perm_reason": "no realized trades"}
    obs_wr = obs_wins / obs_n
    null = sum(g.null for g in groups)
    n_perm = len(null)
    pnl_ge = wr_ge = n_done = 0
    ratios = []
    for pnl, nt, nw, days in null:
        if nt == 0:
            continue
        n_done += 1
        pnl_ge += pnl >= obs_pnl
        wr_ge += (nw / nt) >= obs_wr
        if obs_days > 0:
            ratios.append(days / obs_days)
    out = {**base, "obs_pnl_dollars": round(obs_pnl, 2), "obs_win_rate_pct": round(100 * obs_wr, 1)}
    if n_done < max(20, n_perm // 4):
        return {**out, "pnl_p": None, "exposure_mismatch": True,
                "perm_reason": f"only {n_done} permutations produced trades"}
    ratio = float(np.median(ratios)) if ratios else None
    lo, hi = ev_stats.SHIFT_EXPOSURE_BOUNDS
    return {**out, "pnl_p": round((1 + int(pnl_ge)) / (n_done + 1), 4),
            "win_rate_p": round((1 + int(wr_ge)) / (n_done + 1), 4), "n_perm": n_done,
            "null_exposure_ratio": None if ratio is None else round(ratio, 4),
            "exposure_mismatch": ratio is None or not (lo <= ratio <= hi)}


# --------------------------------------------------------------- the pass rule

def _sound(result: dict) -> bool:
    pnl = result.get("obs_pnl_dollars")
    return bool(result.get("pnl_p") is not None and pnl is not None and pnl > 0
                and result.get("exposure_mismatch") is False)


def _bh(results: dict, scenario: str, q: float) -> pd.DataFrame:
    """BH over every strategy. No p-value counts as p = 1, so it stays in the family;
    ev_stats.bh_fdr on its own would leave it out and make the correction easier."""
    records = [{"strategy_id": s, "p": 1.0 if r[scenario].get("pnl_p") is None else r[scenario]["pnl_p"]}
               for s, r in results.items()]
    return ev_stats.bh_fdr(records, alpha=q, p_key="p").set_index("strategy_id")


def family_verdict(results: dict, required, stage4_passed, q: float = BH_Q) -> dict:
    """Change 2 of the amendment. Refuses a partial family, because the correction must
    cover the whole search."""
    missing = sorted(set(required) - set(results))
    if missing:
        raise ValueError(f"family incomplete, missing {len(missing)}: {missing[:5]}")
    primary = _bh(results, PRIMARY_SCENARIO, q)
    wipeout = _bh(results, WIPEOUT_SCENARIO, q)
    out = {}
    for slug, res in results.items():
        bh_primary, bh_wipeout = bool(primary.loc[slug, "reject"]), bool(wipeout.loc[slug, "reject"])
        out[slug] = {"bh_primary": bh_primary, "bh_wipeout": bh_wipeout,
                     "bh_q": float(primary.loc[slug, "p_adj"]), "bh_q_wipeout": float(wipeout.loc[slug, "p_adj"]),
                     "stage4_pass": slug in stage4_passed,
                     "rescore_pass": bool(slug in stage4_passed and bh_primary and bh_wipeout
                                          and _sound(res[PRIMARY_SCENARIO]) and _sound(res[WIPEOUT_SCENARIO]))}
    return out


def holdout_success(result: dict) -> bool:
    """Stage 5's existing test, under both scenarios that decide the re-score."""
    def ok(r):
        p, pnl = r.get("pnl_p"), r.get("obs_pnl_dollars")
        return p is not None and p < SUCCESS_P and pnl is not None and pnl > 0
    return bool(ok(result[PRIMARY_SCENARIO]) and ok(result[WIPEOUT_SCENARIO]))


def registry_rows(results: dict, verdict: dict, *, run_id: str, universe_hash: str, date_range: str,
                  created_at: str, execution_hash: str) -> pd.DataFrame:
    recs = []
    for slug, res in results.items():
        primary, wipeout = res[PRIMARY_SCENARIO], res[WIPEOUT_SCENARIO]
        stats = {"pnl_p": primary.get("pnl_p"), "total_pnl_net": primary.get("obs_pnl_dollars"),
                 "n_trades": primary.get("n_trades"), "null_exposure_ratio": primary.get("null_exposure_ratio"),
                 "exposure_mismatch": float(bool(primary.get("exposure_mismatch"))),
                 "wipeout_pnl_p": wipeout.get("pnl_p"), "wipeout_total_pnl_net": wipeout.get("obs_pnl_dollars"),
                 "bh_q": verdict[slug]["bh_q"], "bh_q_wipeout": verdict[slug]["bh_q_wipeout"],
                 "rescore_pass": float(verdict[slug]["rescore_pass"])}
        for statistic, value in stats.items():
            if value is not None:
                recs.append({"run_id": run_id, "input_name": f"pine_{slug}", "input_type": "trade_rule",
                             "evaluation": EVALUATION_NAME, "horizon": -1, "statistic": statistic,
                             "value": float(value), "n": primary.get("n_trades", 0), "universe_hash": universe_hash,
                             "date_range": date_range, "created_at": created_at, "execution_hash": execution_hash})
    return pd.DataFrame(recs, columns=ev_registry.COLUMNS)


# --------------------------------------------------------------- CRSP frames

def crsp_symbol(permno) -> str:
    """Permanent id, because tickers get recycled. Also the input to the holdout hash."""
    return f"CRSP{int(permno)}"


def crsp_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """OHLCV frame from CRSP legacy daily rows, split-adjusted only, real traded days only.

    A negative price is a bid/ask midpoint on a no-trade day. Those rows are removed from the
    frame but still count toward trailing dollar volume, as zero unless volume was reported;
    otherwise a stock trading once a week would look liquid. A traded day with a missing volume
    leaves `adv` unmeasured. `adv` is real dollars from sessions strictly before the row's own.
    Open is only recorded from 1992, so a missing one falls back to close.
    """
    missing = [c for c in _CRSP_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError(f"not the CRSP legacy daily schema; missing columns: {missing}")
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates("date").set_index("date")

    traded = (df["prc"] > 0) & (df["cfacpr"] > 0)
    reported = df["vol"] >= 0
    dollars = (df["prc"].abs() * df["vol"]).where(reported)
    dollars = dollars.where(traded, dollars.where(reported, 0.0).fillna(0.0))
    adv = dollars.rolling(ADV_WINDOW, min_periods=ADV_WINDOW).mean().shift(1)

    t = df[traded]
    factor = t["cfacpr"]

    def price(col):
        return (t[col].abs() / factor).where(lambda s: s > 0)

    close = price("prc")
    open_ = price("openprc").fillna(close)
    high = pd.concat([price("askhi"), open_, close], axis=1).max(axis=1)
    low = pd.concat([price("bidlo"), open_, close], axis=1).min(axis=1)
    volume = (t["vol"] * t["cfacshr"]).where(t["vol"] >= 0)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume,
                         "real_close": t["prc"], "adv": adv[traded]})


def has_big_jump(frame: pd.DataFrame) -> bool:
    """The clean_symbols() test on one frame, for the reported-only screened run."""
    c = frame["close"].to_numpy(dtype=float)
    return len(c) > 1 and float(np.nanmax(np.abs(np.diff(np.log(c))))) > MAX_ABS_LOG_RETURN


# --------------------------------------------------------------- split and holdout protection

def assign_holdout(companies: pd.DataFrame, stage3_ranges: dict, window_days: int = OVERLAP_DAYS):
    """(permno, holdout, basis) per company, and Stage 3 symbols no company claimed.

    A company matching a Stage 3 symbol keeps that symbol's assignment, so a development
    company stays in development and a holdout company stays reserved. Everything uncertain
    goes to holdout, because a seen company in the holdout cannot be undone later.
    `stage3_ranges` maps a symbol to its (first date, last date).
    """
    rows, claims = [], defaultdict(list)
    for r in companies.itertuples():
        rng = stage3_ranges.get(r.ticker)
        if rng is None:
            rows.append([r.permno, _is_holdout_symbol(crsp_symbol(r.permno)), "permno_hash"])
            continue
        first, last = pd.Timestamp(rng[0]), pd.Timestamp(rng[1])
        alive = pd.isna(r.dlstdt)
        end = None if alive else pd.Timestamp(r.dlstdt)
        if alive or abs((last - end).days) <= window_days:
            claims[r.ticker].append(len(rows))
            rows.append([r.permno, _is_holdout_symbol(r.ticker), "stage3_match"])
        elif (first - end).days > window_days:
            rows.append([r.permno, _is_holdout_symbol(crsp_symbol(r.permno)), "recycled"])
        else:
            claims[r.ticker]            # the symbol is accounted for, but not by a clear match
            rows.append([r.permno, True, "ambiguous"])
    for positions in claims.values():
        if len(positions) > 1:
            for k in positions:
                rows[k][1:] = [True, "ambiguous"]
    out = pd.DataFrame(rows, columns=["permno", "holdout", "basis"])
    out["holdout"] = out["holdout"].astype(bool)
    return out, sorted(set(stage3_ranges) - set(claims))


def pull_end(holdout: bool) -> "str | None":
    """Last date to request for a company. A holdout company's later prices stay unread."""
    return DEV_END if holdout else None


def assert_no_reserved_rows(prices: pd.DataFrame, holdout_permnos) -> None:
    """Refuse a panel holding reserved holdout prices, however they got there."""
    late = prices[pd.to_datetime(prices["date"]) > pd.Timestamp(DEV_END)]
    bad = set(late["permno"].unique()) & set(holdout_permnos)
    if bad:
        raise RuntimeError(f"panel holds holdout prices after {DEV_END} for {len(bad)} companies; "
                           f"remove them unread before running")


def dev_view(frame: pd.DataFrame, ret: float, holdout: bool, dlstdt):
    """(view, terminal return, died). A holdout company that died after DEV_END is capped
    there and shows no death: that the company later failed is itself reserved information."""
    died = not pd.isna(dlstdt)
    if not holdout:
        return frame, (ret if died else 0.0), died
    view = frame[frame.index <= pd.Timestamp(DEV_END)]
    if died and pd.Timestamp(dlstdt) <= pd.Timestamp(DEV_END):
        return view, ret, True
    return view, 0.0, False


def holdout_view(frame: pd.DataFrame, ret: float, dlstdt):
    """(view, terminal return, died) for Stage 5, from HOLDOUT_START on."""
    died = not pd.isna(dlstdt)
    return frame[frame.index >= pd.Timestamp(HOLDOUT_START)], (ret if died else 0.0), died


def first_common_open_date(first_opens: pd.DataFrame, companies: pd.DataFrame, exchanges=EXCHANGES) -> pd.Timestamp:
    """The first date on which every exchange has real open prices."""
    merged = first_opens.merge(companies[["permno", "exchcd"]], on="permno")
    firsts = merged.groupby("exchcd")["first_open"].min()
    absent = [e for e in exchanges if e not in firsts.index]
    if absent:
        raise ValueError(f"no open prices for exchange codes {absent}")
    return pd.Timestamp(firsts.loc[list(exchanges)].max())


# --------------------------------------------------------------- the panel

@dataclass
class CrspPanel:
    companies: pd.DataFrame      # sorted by permno, with `holdout` and `basis`
    prices_path: str
    view: str                    # "dev" or "holdout"
    start: "pd.Timestamp | None"
    report: dict

    def keys(self) -> list:
        return [crsp_symbol(p) for p in self.companies["permno"]]

    def load_batch(self, permnos) -> dict:
        """{permno: frame}, in the order given. A frame is built before any date cut, so
        trailing dollar volume can use sessions before the cut."""
        ids = [int(p) for p in permnos]
        prices = pd.read_parquet(self.prices_path, filters=[("permno", "in", ids)])
        if self.view == "dev":
            assert_no_reserved_rows(prices, set(self.companies.loc[self.companies["holdout"], "permno"]))
        groups = dict(tuple(prices.groupby("permno")))
        out = {}
        for p in ids:
            if p in groups:
                frame = crsp_frame(groups[p])
                out[p] = frame if self.start is None else frame[frame.index >= self.start]
        return out

    def view_of(self, frame: pd.DataFrame, ret: float, row):
        if self.view == "dev":
            return dev_view(frame, ret, bool(row.holdout), row.dlstdt)
        return holdout_view(frame, ret, row.dlstdt)


def _read_companies(root: str, stage3_ranges: dict):
    companies = pd.read_parquet(os.path.join(root, "companies.parquet"))
    missing = [c for c in _COMPANY_COLUMNS if c not in companies.columns]
    if missing:
        raise ValueError(f"companies.parquet is missing columns: {missing}")
    companies = companies.sort_values("permno").reset_index(drop=True)
    # CRSP can carry its last date on an active company's row; that is not a death.
    companies["dlstdt"] = pd.to_datetime(companies["dlstdt"]).where(companies["dlstcd"] != ACTIVE_CODE)
    assigned, unmatched = assign_holdout(companies, stage3_ranges)
    companies = companies.merge(assigned, on="permno")
    if "forced_holdout" in companies.columns:
        # Set by strategies/crsp_pull.py for companies that may be an unmatched holdout
        # Stage 3 symbol. These rules only ever add companies to the holdout.
        forced = companies["forced_holdout"].fillna(False).astype(bool)
        companies.loc[forced, "holdout"] = True
        companies.loc[forced, "basis"] = companies.loc[forced, "forced_basis"]
    report = {"n_companies": len(companies), "n_alive": int(companies["dlstdt"].isna().sum()),
              "n_delisted": int(companies["dlstdt"].notna().sum()),
              "n_missing_return": sum(return_is_assumed(r.dlret, r.dlstcd) for r in companies.itertuples()),
              "basis": companies["basis"].value_counts().to_dict(), "stage3_unmatched": unmatched}
    return companies, report


def _first_opens(prices_path: str) -> pd.DataFrame:
    import duckdb
    sql = ("select permno, min(date) as first_open from read_parquet(?) "
           "where openprc is not null group by permno")
    out = duckdb.execute(sql, [prices_path]).df()
    out["first_open"] = pd.to_datetime(out["first_open"])
    return out


def load_dev_panel(stage3_ranges: dict, root: str = CRSP_ROOT, exchanges=EXCHANGES) -> CrspPanel:
    """Every company, development prices only, from the first date with real opens everywhere."""
    companies, report = _read_companies(root, stage3_ranges)
    path = os.path.join(root, "prices.parquet")
    start = first_common_open_date(_first_opens(path), companies, exchanges)
    return CrspPanel(companies, path, "dev", start, {**report, "start": str(start.date())})


def load_holdout_panel(stage3_ranges: dict, root: str = CRSP_ROOT) -> CrspPanel:
    """Stage 5: holdout companies from HOLDOUT_START. Requested only when Stage 5 runs."""
    companies, report = _read_companies(root, stage3_ranges)
    companies = companies[companies["holdout"]].reset_index(drop=True)
    return CrspPanel(companies, os.path.join(root, "holdout_prices.parquet"), "holdout",
                     pd.Timestamp(HOLDOUT_START), {**report, "n_holdout": len(companies)})


# --------------------------------------------------------------- scoring

def _assemble(groups: dict) -> dict:
    """Totals per scenario from class pieces. Only companies with a missing return differ
    by scenario, and null totals add up, so each company is simulated as few times as possible."""
    def pick(scenario, screened=False):
        return [g for (_cls, jump, s), gs in groups.items() for g in gs
                if s in (None, scenario) and not (screened and jump)]

    out = {s: combine(pick(s)) for s in SCENARIOS}
    out["shumway_with_jump_screen"] = combine(pick(PRIMARY_SCENARIO, screened=True))
    classes = {}
    for cls in ("alive", "real_return", "assumed"):
        members = [g for (c, _jump, s), gs in groups.items() for g in gs if c == cls and s in (None, PRIMARY_SCENARIO)]
        classes[cls] = {"n_symbols": sum(g.n_symbols for g in members), "n_trades": sum(g.obs_n for g in members),
                        "pnl": round(sum(g.obs_pnl for g in members), 2),
                        "forced_closes": sum(g.forced_n for g in members),
                        "forced_close_pnl": round(sum(g.forced_pnl for g in members), 2)}
    out["classes"] = classes
    return out


def score_panel(rules: dict, panel: CrspPanel, *, n_perm: int = N_PERM, seed: int = SEED, config,
                executor=None, n_workers: int = 2, batch: int = 200, log=print,
                floor: float = PRICE_FLOOR) -> dict:
    """{slug: results} over the whole panel. A company's position in the sorted list seeds its
    offsets, so batch size and the class pieces do not change any number. Rules are unfloored:
    the floor is applied here, on the real price."""
    companies = panel.companies
    groups = {slug: defaultdict(list) for slug in rules}
    t0 = time.time()
    for b in range(0, len(companies), batch):
        sub = companies.iloc[b:b + batch]
        frames = panel.load_batch(sub["permno"])
        pieces: dict = defaultdict(lambda: ({}, {}, {}))
        for pos, row in zip(range(b, b + len(sub)), sub.itertuples()):
            frame = frames.get(int(row.permno))
            if frame is None or frame.empty:
                continue
            view, _ret, died = panel.view_of(frame, 0.0, row)
            if view.empty:
                continue
            sym = crsp_symbol(row.permno)
            assumed = died and return_is_assumed(row.dlret, row.dlstcd)
            cls = "assumed" if assumed else ("real_return" if died else "alive")
            jump = has_big_jump(view)
            for scenario in (SCENARIOS if assumed else (None,)):
                ret = terminal_return(row.dlret, row.dlstcd, row.exchcd, scenario or PRIMARY_SCENARIO) if died else 0.0
                fr, terms, idx = pieces[(cls, jump, scenario)]
                fr[sym], terms[sym], idx[sym] = view, ret, pos
        for slug, rule in rules.items():
            for key, (fr, terms, idx) in pieces.items():
                groups[slug][key].append(evaluate_group(
                    rule, fr, terms, n_perm=n_perm, seed=seed, config=config, entry_floor=floor,
                    executor=executor, n_workers=n_workers, corrected=True, indices=idx))
        log(f"{min(b + batch, len(companies))}/{len(companies)} companies {time.time() - t0:.0f}s")
    return {slug: _assemble(g) for slug, g in groups.items()}


def run_rescore(rules: dict, panel: CrspPanel, *, required, stage4_passed, n_perm: int = N_PERM, seed: int = SEED,
                config, executor=None, n_workers: int = 2, log=print) -> dict:
    """Score and, only if the whole family is there, decide."""
    results = score_panel(rules, panel, n_perm=n_perm, seed=seed, config=config, executor=executor,
                          n_workers=n_workers, log=log)
    verdict = None if set(required) - set(results) else family_verdict(results, required, stage4_passed)
    return {"results": results, "verdict": verdict, "panel": panel.report}


# --------------------------------------------------------------- data check

def compare_with_yahoo(crsp_close: pd.Series, yahoo_close: pd.Series, max_gap: float = 0.02) -> dict:
    """How far apart two split-adjusted close series are, on the days both have."""
    both = pd.concat([crsp_close, yahoo_close], axis=1, join="inner").dropna()
    if both.empty:
        return {"n_days": 0, "median_gap": None, "worst_gap": None, "large": True}
    gap = (both.iloc[:, 1] / both.iloc[:, 0] - 1.0).abs()
    median = float(gap.median())
    return {"n_days": len(both), "median_gap": median, "worst_gap": float(gap.max()), "large": bool(median > max_gap)}


def run_data_check(panel: CrspPanel, load_yahoo, *, n: int = 50, seed: int = SEED) -> list:
    """Compare a fixed sample of Stage 3-matched companies, within what each may be read for.
    `load_yahoo(symbol, end)` returns that symbol's close series. Scores nothing."""
    matched = panel.companies[panel.companies["basis"] == "stage3_match"]
    if len(matched) > n:
        matched = matched.sample(n, random_state=seed).sort_values("permno")
    out = []
    for row in matched.itertuples():
        frame = panel.load_batch([row.permno]).get(int(row.permno))
        if frame is None or frame.empty:
            continue
        view, _ret, _died = panel.view_of(frame, 0.0, row)
        if view.empty:
            continue
        cmp = compare_with_yahoo(view["close"], load_yahoo(row.ticker, str(view.index.max().date())))
        out.append({"symbol": row.ticker, "permno": int(row.permno), **cmp})
    return out


# --------------------------------------------------------------- real-data driver

def tested_strategies(reg: pd.DataFrame) -> list:
    """Every strategy with a Stage 3 v2 result: the family the correction must cover."""
    sub = reg[(reg["evaluation"] == "tv_strategy_catalog_stage3_v2") & (reg["statistic"] == "pnl_p")]
    return sorted(s.removeprefix("pine_") for s in sub["input_name"].unique())


def stage3_ranges(query, price_table: str) -> dict:
    last = query.sql(f"select symbol, min(date) a, max(date) b from {price_table} group by symbol")
    return dict(zip(last["symbol"], zip(pd.to_datetime(last["a"]), pd.to_datetime(last["b"]))))


def _stage3_check(stage3, query, rules: dict, *, n_perm: int, seed: int, executor, n_workers: int,
                  batch: int = 200, log=print) -> dict:
    """Stage 3 inputs and rules through this code. Reproducing the registry checks the code only."""
    cfg = stage3.cost_config(stage3.PRIMARY_COST_BPS)
    symbols = stage3.clean_symbols(query.symbols(stage3.PRICE_TABLE), price_table=stage3.PRICE_TABLE)
    dev, holdout = stage3.dev_holdout_symbols(symbols)
    plan = [(s, None) for s in dev] + [(s, stage3.DEV_END) for s in holdout]
    out = {slug: [] for slug in rules}
    idx = 0
    t0 = time.time()
    for b in range(0, len(plan), batch):
        frames = {}
        for sym, end in plan[b:b + batch]:
            df = stage3._load_ohlcv(sym, stage3.PRICE_TABLE, start=None, end=end)
            if df is not None and not df.empty:
                frames[sym] = df
        for slug, rule in rules.items():
            out[slug].append(evaluate_group(rule, frames, {}, first_idx=idx, n_perm=n_perm, seed=seed, config=cfg,
                                            entry_floor=stage3.PRICE_FLOOR, executor=executor, n_workers=n_workers))
        idx += len(frames)
        log(f"stage3 check {min(b + batch, len(plan))}/{len(plan)} {time.time() - t0:.0f}s")
    return {slug: {"stage3_check": combine(gs)} for slug, gs in out.items()}


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o))


def main(argv=None) -> int:
    from concurrent.futures import ProcessPoolExecutor

    import query
    from strategies import stage3

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--slugs", help="comma-separated strategies; default is the whole tested family")
    ap.add_argument("--out", required=True, help="JSON results path")
    ap.add_argument("--n-perm", type=int)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--stage3-check", action="store_true",
                    help="Stage 3 inputs and rules, to check this code; needs no CRSP data, needs --slugs")
    ap.add_argument("--check-data", type=int, metavar="N",
                    help="compare N matched companies with Yahoo and stop; scores nothing")
    ap.add_argument("--write-registry", action="store_true", help="log results; needs the whole family")
    args = ap.parse_args(argv)

    if args.stage3_check:
        if not args.slugs:
            ap.error("--stage3-check needs --slugs")
        rules ={s: stage3.with_price_floor(stage3.load_rule_for(s)[0]) for s in args.slugs.split(",")}
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            out = _stage3_check(stage3, query, rules, n_perm=args.n_perm or stage3.N_PERM, seed=args.seed,
                                executor=executor, n_workers=args.workers)
    else:
        ranges = stage3_ranges(query, stage3.PRICE_TABLE)
        panel = load_dev_panel(ranges)
        if args.check_data:
            def yahoo(sym, end):
                df = stage3._load_ohlcv(sym, stage3.PRICE_TABLE, start=None, end=end)
                return pd.Series(dtype=float) if df is None or df.empty else df["close"]
            out = {"panel": panel.report, "check": run_data_check(panel, yahoo, n=args.check_data, seed=args.seed)}
        else:
            from strategies.stage5 import stage4_survivors
            assert_amendment_in_force()
            config = crsp_config()
            required = tested_strategies(ev_registry.load())
            slugs = args.slugs.split(",") if args.slugs else required
            rules = {s: stage3.load_rule_for(s)[0] for s in slugs}
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                out = run_rescore(rules, panel, required=required, stage4_passed=set(stage4_survivors()),
                                  n_perm=args.n_perm or N_PERM, seed=args.seed, config=config,
                                  executor=executor, n_workers=args.workers)
            if args.write_registry:
                if out["verdict"] is None:
                    raise SystemExit("--write-registry needs the whole family; nothing was written")
                ev_registry.append(registry_rows(
                    out["results"], out["verdict"], run_id=ev_registry.new_run_id(),
                    universe_hash=ev_registry.universe_hash(panel.keys()), date_range=f"{panel.report['start']}..",
                    created_at=datetime.now(timezone.utc).isoformat(),
                    execution_hash=ev_execution.config_hash(config)))
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(_dump(out))
    print(_dump(out))
    return 0


if __name__ == "__main__":
    import multiprocessing as _mp
    _mp.freeze_support()
    raise SystemExit(main())
