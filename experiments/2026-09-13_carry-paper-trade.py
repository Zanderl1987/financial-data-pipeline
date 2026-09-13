#!/usr/bin/env python3
"""
Forward paper-trade driver for the published CARRY LONG-ONLY futures book
(2026-09-12_carry_futures.py: min_events=3, long-only half, per-instrument
ex-ante vol-target 0.40, roll-gap carry proxy, positions effective t0+1),
scaled to a 10% ANNUALIZED BOOK-VOL footprint -- the "10%-vol carry long-only
forward paper-trade" queued in TASKS.md.

DESIGN (fixed, stated):
  - Construction is the PUBLISHED code, imported by reference (carry module +
    _tsmom_core portfolio_returns / roll_mask / load_close_wide); nothing is
    copied. No tuning: min_events=3, long-only, VOL_TARGET=0.40, vol_floor=0.10.
  - The book's daily P&L = (portfolio_returns gross on roll-masked returns).
    10% VOL SCALING: apply the PIT scaler target_book_vol / rolling-252 vol
    (shifted 1 day, clipped >=5%, same construction as the VTSL vol_target()).
    This is the C1 paper-book convention: the FORWARD book trades at 10%
    book vol; the backtest numbers that got the signal admitted used 0.40
    per-instrument target and its own realized vol -- the 10% figure is the
    live-book footprint, not a search parameter.
  - WARM-UP vs FORWARD split (both reported, clearly labeled): the historical
    series since the first month with a complete score (warm-up, reproducibility
    bar) and the genuine FORWARD window from PAPER_START (default: the most
    recent complete month-end signal date, i.e. where real forward P&L begins
    to accrue). The state dir records PAPER_START and the running book.
  - Cadence: `--daily` (no arguments) refreshes the book from the LATEST
    data, recomputes the current position from the last complete month-end,
    and appends the daily forward-P&L rows since the last write (deterministic
    from the data, PIT). Scheduling that CLI daily (Task Scheduler) provides
    the "confirmed running cadence"; deciding to schedule it is an operational
    call (recorded in the writeup), running it is what this file does.
  - Registry hygiene: this book does NOT write to the shared evaluation
    registry -- futures-side results are family-2 (never pooled into the TV
    campaign's deflated-Sharpe population), same rule as every futures
    experiment. Forward state lives in storage/reports/eval/carry_paper/.
  - Costs: gross + net-of-10bps-one-way reported (carry's published cost note:
    net 0.33 vs gross 0.46). The forward book should be tracked NET.

Run:
  full historical repro + current position:
    C:\\ProgramData\\anaconda3\\python.exe experiments/2026-09-13_carry-paper-trade.py
  daily refresh (forward P&L append + state update):
    C:\\ProgramData\\anaconda3\\python.exe experiments/2026-09-13_carry-paper-trade.py --daily
"""
import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PAPER_DIR = os.path.join("storage", "reports", "eval", "carry_paper")
STATE_FILE = os.path.join(PAPER_DIR, "book_state.json")
EQ_FILE = os.path.join(PAPER_DIR, "equity_daily.parquet")
POS_FILE = os.path.join(PAPER_DIR, "position_current.parquet")
ANN = 252
VOL_TARGET = 0.40       # published per-instrument ex-ante vol target
BOOK_VOL = 0.10          # 10% annualized book vol footprint (forward book)
VOL_FLOOR = 0.05         # cap the scaling when rolling vol is collapsed
REPRO_FLOOR_GROSS = 0.43  # published carry long-only gross Sharpe (0.46) bar
REPRO_FLOOR_NET = 0.30    # published net-10bps Sharpe (0.33) bar


def load_carry_module():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo_root, "experiments"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "carry_0912", os.path.join(repo_root, "experiments",
                                   "2026-09-12_carry_futures.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def book_vol_scale(gross: pd.Series, target: float = BOOK_VOL,
                   floor: float = VOL_FLOOR) -> pd.Series:
    """PIT scaler: daily book return x target / rolling-252 vol (shifted 1d,
    clipped at floor) -- the same construction the VTSL module uses."""
    v = gross.rolling(ANN).std(ddof=1) * math.sqrt(ANN)
    v = v.clip(lower=floor)
    return gross * target / v.shift(1)


def build_carry_positions_robust(scores: pd.DataFrame, close: pd.DataFrame,
                                 vol_floor: float = 0.10,
                                 long_short: bool = True,
                                 min_periods: int = 200) -> pd.DataFrame:
    """FORWARD-book sizing variant of the published build_carry_positions.

    The published estimator uses rolling(252).std(ddof=1) exact-window: any
    NaN inside the trailing 252 days makes that symbol's vol NaN and its
    position 0. That is harmless at true month-ends (dense history) but the
    live store carries cross-market calendar rows (e.g. 2026-09-06/07/08 rows
    where most foreign contracts are NaN), so EVERY anchor within ~252 days of
    such a row sees 0/44 finite vol -> a flat book. Identical to published in
    every respect except the vol denominator uses min_periods=200 (skips the
    calendar rows). On dense months the two are numerically identical. This is
    the ONLY construction deviation of the forward book; it is documented in
    the writeup and does NOT touch the warm-up reproduction (which uses the
    published function verbatim)."""
    ret = close.pct_change()
    vol = ret.rolling(252, min_periods=min_periods).std(ddof=1) * math.sqrt(ANN)
    vol = vol.clip(lower=vol_floor)
    pos = pd.DataFrame(0.0, index=scores.index, columns=close.columns)
    for t0 in scores.index:
        row = scores.loc[t0]
        good = row[np.isfinite(row)]
        if len(good) < 6:
            continue
        med = good.median()
        v = vol.loc[t0]
        for sym in good.index:
            scale = 0.5 * VOL_TARGET / v[sym] if v[sym] > 0 else 0.0
            if long_short:
                pos.at[t0, sym] = scale if good[sym] > med else -scale
            else:
                pos.at[t0, sym] = scale if good[sym] > med else 0.0
    return pos


def ann_sharpe(ret) -> float:
    r = pd.Series(ret).dropna()
    if len(r) < 3 or float(r.std(ddof=1)) == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1) * math.sqrt(ANN))


def main() -> None:
    carry = load_carry_module()
    core = sys.modules.get("_tsmom_core") or __import__("_tsmom_core")
    close = core.load_close_wide()
    open_ = core.load_open_wide().reindex(close.index)
    rm = core.roll_mask()
    scores = carry.build_carry(close, open_, rm, min_events=3)
    pos_hist = carry.build_carry_positions(scores, close, long_short=False)
    pos_fwd = build_carry_positions_robust(scores, close, long_short=False)
    ret_clean = close.pct_change()[~rm]
    pr_hist = core.portfolio_returns(pos_hist, close, ret=ret_clean)
    gross, net10 = pr_hist["gross"], pr_hist["net_10bps"]

    # reproducibility bar (published carry long-only, full series, UNTOUCHED
    # published construction -- the robust-vol variant lives only in the
    # forward book below and must not move this number)
    sh_g, sh_n = ann_sharpe(gross), ann_sharpe(net10)
    print(f"[guard] carry long-only gross Sharpe {sh_g:.2f} (floor {REPRO_FLOOR_GROSS}) | "
          f"net-10bps {sh_n:.2f} (floor {REPRO_FLOOR_NET})")
    assert sh_g >= REPRO_FLOOR_GROSS, "carry gross repro failed"
    assert sh_n >= REPRO_FLOOR_NET, "carry net repro failed"

    # 10% book-vol footprint series (whole history, warm-up view)
    vol10 = book_vol_scale(gross)
    eq10 = (1 + vol10).cumprod()

    # --- FORWARD book: completed-month anchors ONLY ------------------------------
    # month_end_anchors labels each month's anchor with its LAST ACTUAL trading
    # day. In the backtest that day is always a true month end (history complete),
    # so shift(1) puts the position live on the first trading day of the next
    # month. In real-time the CURRENT month's anchor is computed from partial
    # data and shift(1) would deploy it MID-MONTH -- a convention break. The
    # forward book therefore holds only anchors whose calendar month has fully
    # elapsed; the provisional anchor is reported as pending and deploys once
    # that month completes (identical to how the backtest ever deployed).
    data_end = close.index.max()
    m_end = pd.to_datetime([pd.Timestamp(a) + pd.offsets.MonthEnd(0) for a in scores.index])
    complete = scores.index[m_end <= data_end]
    provisional = scores.index[m_end > data_end]
    pr_fwd = core.portfolio_returns(pos_fwd.loc[complete], close, ret=ret_clean)
    gross_fwd, vol10_fwd = pr_fwd["gross"], book_vol_scale(pr_fwd["gross"])
    held_fwd = pos_fwd.loc[complete].reindex(close.index, method="ffill").shift(1).fillna(0.0)

    cur = held_fwd.loc[data_end]                       # book in force NOW
    top = cur[cur != 0].sort_values()
    n_active = int((top != 0).sum())
    print(f"\nbook in force through {data_end.date()} (signal = last completed month "
          f"{complete[-1].date()}) -> {n_active} instruments (robust-vol sizing; "
          f"published sizing at this anchor: "
          f"{int((pos_hist.reindex(close.index, method='ffill').shift(1).fillna(0).loc[data_end] != 0).sum())})")
    print("  top 8 long holdings (by weight):")
    for sym, w in top.sort_values(ascending=False).head(8).items():
        print(f"    {sym:6s} {w:+.3f}")
    if len(provisional) > 0:
        prov = pos_fwd.loc[provisional[-1]]
        n_prov = int((prov != 0).sum())
        deploy = (pd.Timestamp(provisional[-1]) + pd.offsets.MonthBegin(1)).date()
        print(f"  pending: {n_prov}-instrument rebalance from provisional anchor "
              f"{provisional[-1].date()} (partial-month data), deploys {deploy} at the "
              f"month end per published convention")

    # warm-up stats (historical repro on the published construction)
    print(f"\nwarm-up (full history, {gross.first_valid_index().date()} -> "
          f"{gross.last_valid_index().date()}):")
    print(f"  gross Sharpe {sh_g:.2f} | net-10bps {sh_n:.2f} | book is the "
          f"10%-scaled footprint (equity series saved)")

    os.makedirs(PAPER_DIR, exist_ok=True)

    # arm the book: official live start = first trading day AFTER today's data
    # (kept forever in state; later --daily runs keep it). P&L rows at/after
    # paper_start are the genuine forward book; earlier completed-month rows are
    # the deterministic pre-arm backfill shown as the book's own reference.
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
    else:
        arm_day = (data_end + pd.offsets.BusinessDay(1)).date()
        state = {"paper_start": str(arm_day),
                 "paper_vol_target": BOOK_VOL,
                 "construction": "published carry long-only (signal imported), "
                                 "min_events=3, per-instr vol-target 0.40, floor 0.10, "
                                 "long-only; forward sizing robust-vol min_periods=200",
                 "registry": "excluded (family-2 rule)",
                 "completed_months_only": True}
    state["last_refresh"] = str(data_end.date())
    state["live_signal_date"] = str(complete[-1].date())
    state["pending_rebalance_signal_date"] = str(provisional[-1].date()) \
        if len(provisional) > 0 else None
    state["n_active"] = n_active
    state["warmup_gross_sharpe"] = sh_g
    state["warmup_net10bps_sharpe"] = sh_n
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)

    paper_ts = pd.Timestamp(state["paper_start"])
    out = pd.DataFrame({"gross_hist": gross, "net_10bps_hist": net10,
                        "equity_10pct_vol_hist": eq10,
                        "gross_forward": gross_fwd,
                        "return_10pct_vol_forward": vol10_fwd})
    out["pre_arm_backfill"] = out.index < paper_ts
    out["fwd_return"] = out["return_10pct_vol_forward"].where(out.index >= paper_ts)
    out["fwd_equity"] = out["fwd_return"].fillna(0.0).cumprod()
    out.to_parquet(EQ_FILE)
    pd.DataFrame(cur).to_parquet(POS_FILE)
    print(f"\nwrote {STATE_FILE}\nwrote {EQ_FILE}\nwrote {POS_FILE}")

    fwd = out["fwd_return"].dropna()
    print(f"\npaper book armed: live start {state['paper_start']} "
          f"(next trading day after {data_end.date()}), book 10% vol target, "
          f"{len(fwd)} forward day(s) so far")
    if len(fwd):
        print(f"  forward cumret {float((1 + fwd).prod() - 1)*100:.2f}% | "
              f"Sharpe {ann_sharpe(fwd) if len(fwd) > 2 else float('nan')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--daily", action="store_true",
                    help="refresh on latest data (default full repro + state)")
    args = ap.parse_args()
    main()