"""Capital-limited account simulator for the paper-trading forward test.

Timing mirrors evaluation/trades.py `_next_candidate`: a signal on bar t fills
at the close of t+1; exits are searched from entry_bar+1 and fill at the close
of j+1; a symbol cannot re-enter from a signal on or before its exit fill bar
(engine next_free = exit_signal_day + 2). With slots=None every
non-overlapping signal is taken, which is what the engine does.

Each symbol uses its own bar index, so frames may have different dates.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from paper.accounts import BORROW_FEE_BPS_YR, SLOT_USD
from paper.strategies import StrategySpec

TRADE_COLS = ["symbol", "side", "sig_date", "entry_date", "entry_price", "shares",
              "exit_sig_date", "exit_date", "exit_price", "gross_pnl", "cost_usd",
              "borrow_usd", "net_pnl", "cost_bps"]
OPEN_COLS = ["symbol", "side", "sig_date", "entry_date", "entry_price", "shares"]


@dataclass
class SimResult:
    trades: pd.DataFrame
    equity: pd.Series
    open_positions: pd.DataFrame


class _Sym:
    def __init__(self, name, df, spec, slot_usd):
        from evaluation.trades import _compute_adv_impact_arrays
        import event_backtest as eb
        if not df.index.is_monotonic_increasing:
            df = df.sort_index()
        self.name = name
        self.dates = df.index
        self.n = len(df)
        self.close = df["close"].to_numpy(float)
        vol = df["volume"].to_numpy(float) if "volume" in df else np.zeros(self.n)
        self.dv = np.nan_to_num(self.close * vol, nan=0.0)
        ent = spec.entries(df)
        self.long = ent["long"].fillna(False).to_numpy(bool)
        self.short = ent["short"].fillna(False).to_numpy(bool)
        self.exit_fn = spec.held_exit(df)
        imp, _ = _compute_adv_impact_arrays(
            pd.Series(self.close, index=self.dates),
            pd.Series(vol, index=self.dates), slot_usd,
            eb.ADV_SQRT_LAW_K, self.n)
        self.imp = np.nan_to_num(imp, nan=0.0, posinf=0.0, neginf=0.0)
        self.pos = {d: i for i, d in enumerate(self.dates)}
        self.blocked_to = -1          # signals at idx <= blocked_to are ignored


def simulate_account(frames: dict[str, pd.DataFrame], spec: StrategySpec, *,
                     start_equity: float, slots: int | None,
                     slot_usd: float = SLOT_USD, start: str = "2010-01-01",
                     end: str = "2026-09-11", commission_bps: float = 1.0,
                     borrow_bps_yr: float = BORROW_FEE_BPS_YR) -> SimResult:
    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    c = commission_bps / 1e4
    syms = {s: _Sym(s, df, spec, slot_usd) for s, df in frames.items()}
    all_dates = sorted({d for s in syms.values() for d in s.dates if t0 <= d <= t1})

    held: dict[str, dict] = {}            # filled positions
    pend_entry: dict[str, dict] = {}      # reserved, fills at its fill date
    fills: dict[pd.Timestamp, list] = {}  # date -> [("entry"|"exit", sym)]
    trades: list[dict] = []

    for d in all_dates:
        # fills at this close, from earlier decisions: exits first, then entries
        todo = fills.pop(d, [])
        for kind, name in sorted(todo, key=lambda x: (x[0] != "exit", x[1])):
            s = syms[name]
            if kind == "exit":
                p = held.get(name)
                if p is None:
                    continue
                j = s.pos[d]
                px = s.close[j]
                if not np.isfinite(px) or px <= 0:
                    p["pending_exit"] = None    # bad price: stay open
                    continue
                trades.append(_close(p, s, j, px, c, borrow_bps_yr))
                s.blocked_to = j
                del held[name]
            else:
                p = pend_entry.pop(name, None)
                if p is None:
                    continue
                j = s.pos[d]
                px = s.close[j]
                if not np.isfinite(px) or px <= 0:
                    continue                    # bad price: slot released
                p.update(entry_i=j, entry_date=d, entry_price=float(px),
                         pending_exit=None)
                held[name] = p

        # (1) exits decided at the close of d, from entry_bar + 1 on
        n_pending_exit = 0
        for name, p in held.items():
            s = syms[name]
            i = s.pos.get(d)
            if i is not None and p["pending_exit"] is None and \
                    i >= p["entry_i"] + 1 and \
                    s.exit_fn(i, p["sig_i"], p["entry_price"], p["side"]):
                if i + 1 < s.n and s.dates[i + 1] <= t1:
                    p["pending_exit"] = i
                    fills.setdefault(s.dates[i + 1], []).append(("exit", name))
            if p["pending_exit"] is not None:
                n_pending_exit += 1

        # (2) free slots, counted after pending exits
        if slots is None:
            free = math.inf
        else:
            free = slots - len(held) - len(pend_entry) + n_pending_exit
        if free <= 0:
            continue

        # (3) candidates ranked by close*volume desc, symbol asc
        cands = []
        for name, s in syms.items():
            i = s.pos.get(d)
            if i is None or name in held or name in pend_entry:
                continue
            if i <= s.blocked_to or i + 1 >= s.n or s.dates[i + 1] > t1:
                continue
            side = "long" if s.long[i] else ("short" if s.short[i] else None)
            if side is None:
                continue
            px = s.close[i]
            if not np.isfinite(px) or px <= 0:
                continue
            shares = math.floor(slot_usd / px)
            if shares < 1:
                continue
            cands.append((-s.dv[i], name, i, side, shares))
        cands.sort(key=lambda x: (x[0], x[1]))
        take = cands if free == math.inf else cands[: int(free)]
        for _, name, i, side, shares in take:
            s = syms[name]
            pend_entry[name] = dict(symbol=name, side=side, sig_i=i, sig_date=d,
                                    shares=shares, pending_exit=None)
            fills.setdefault(s.dates[i + 1], []).append(("entry", name))

    open_rows = [dict(symbol=p["symbol"], side=p["side"], sig_date=p["sig_date"],
                      entry_date=p["entry_date"], entry_price=p["entry_price"],
                      shares=p["shares"]) for p in held.values()]
    tr = pd.DataFrame(trades, columns=TRADE_COLS)
    op = pd.DataFrame(open_rows, columns=OPEN_COLS)
    equity = _equity(tr, held, syms, all_dates, start_equity, borrow_bps_yr)
    return SimResult(tr, equity, op)


def _close(p, s, j, px, c, borrow_bps_yr):
    sign = 1.0 if p["side"] == "long" else -1.0
    ep, sh = p["entry_price"], p["shares"]
    gross = sign * sh * (px - ep)
    cost = sh * ep * (c + s.imp[p["entry_i"]]) + sh * px * (c + s.imp[j])
    days = (s.dates[j] - p["entry_date"]).days
    borrow = (sh * ep * borrow_bps_yr / 1e4 * days / 365.0
              if p["side"] == "short" else 0.0)
    return dict(symbol=p["symbol"], side=p["side"], sig_date=p["sig_date"],
                entry_date=p["entry_date"], entry_price=ep, shares=sh,
                exit_sig_date=s.dates[p["pending_exit"]], exit_date=s.dates[j],
                exit_price=float(px), gross_pnl=gross, cost_usd=cost,
                borrow_usd=borrow, net_pnl=gross - cost - borrow,
                cost_bps=cost / (sh * ep) * 1e4)


def _equity(tr, held, syms, all_dates, start_equity, borrow_bps_yr):
    idx = pd.DatetimeIndex(all_dates)
    eq = np.full(len(idx), float(start_equity))
    if len(idx) == 0:
        return pd.Series(eq, index=idx)
    ff = {}

    def closes(name):
        if name not in ff:
            s = syms[name]
            ff[name] = (pd.Series(s.close, index=s.dates)
                        .reindex(s.dates.union(idx)).ffill().reindex(idx)
                        .to_numpy(float))
        return ff[name]

    def mtm(name, side, sh, ep, entry_date, end_date):
        lo = idx.searchsorted(entry_date, side="left")
        hi = len(idx) if end_date is None else idx.searchsorted(end_date, side="left")
        if hi <= lo:
            return
        cl = closes(name)[lo:hi]
        sign = 1.0 if side == "long" else -1.0
        val = np.nan_to_num(sign * sh * (cl - ep), nan=0.0)
        if side == "short":
            days = np.asarray((idx[lo:hi] - entry_date).days, dtype=float)
            val = val - sh * ep * borrow_bps_yr / 1e4 * days / 365.0
        eq[lo:hi] += val

    for r in tr.itertuples():
        mtm(r.symbol, r.side, r.shares, r.entry_price, r.entry_date, r.exit_date)
        eq[idx.searchsorted(r.exit_date, side="left"):] += r.net_pnl
    for p in held.values():
        mtm(p["symbol"], p["side"], p["shares"], p["entry_price"],
            p["entry_date"], None)
    return pd.Series(eq, index=idx)
