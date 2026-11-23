"""Pure daily decision logic: which market-on-close orders to place today.

Mirrors the day loop in paper/sim.py. "Signal day" is the last bar of each
frame (must equal prev_trading_day). Nothing here touches the network.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import pandas as pd

from paper.accounts import (BORROW_FEE_BPS_YR, LOSS_LIMIT_USD, PRICE_FLOOR,
                            SLOT_USD,
                            AccountSpec)
from paper.strategies import StrategySpec

STALE_FRACTION = 0.05


@dataclass
class Held:
    symbol: str
    side: str                      # "long" | "short" (position side)
    qty: int
    avg_entry_price: float
    sig_date: date | None          # None = untracked
    opened: date | None = None


@dataclass
class OrderIntent:
    symbol: str
    side: str                      # order side: "buy" | "sell"
    qty: int
    action: str                    # entry_long|entry_short|exit|stop_all
    client_order_id: str


@dataclass
class Decision:
    orders: list[OrderIntent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stopped: bool = False
    status: str = "ok"


def client_order_id(account_key: str, today: date, action: str, symbol: str,
                    sig_date: date) -> str:
    return f"pt|{account_key}|{today:%Y%m%d}|{action}|{symbol}|{sig_date:%Y%m%d}"


def parse_client_order_id(s: str) -> dict | None:
    if not isinstance(s, str) or not s.startswith("pt|"):
        return None
    parts = s.split("|")
    if len(parts) != 6:
        return None
    try:
        today = pd.Timestamp(parts[2]).date()
        sig = pd.Timestamp(parts[5]).date()
    except Exception:
        return None
    return {"account_key": parts[1], "today": today, "action": parts[3],
            "symbol": parts[4], "sig_date": sig}


def accrued_borrow_fee(short_lots: list[tuple[float, date, date | None]],
                       today: date, bps_yr: float = BORROW_FEE_BPS_YR) -> float:
    total = 0.0
    for notional, opened, closed in short_lots:
        end = closed if closed is not None else today
        days = max((end - opened).days, 0)
        total += notional * bps_yr / 1e4 * days / 365.0
    return total


def _closing_side(pos_side: str) -> str:
    return "sell" if pos_side == "long" else "buy"


def _last_date(df: pd.DataFrame) -> date | None:
    if df is None or len(df) == 0:
        return None
    return pd.Timestamp(df.index[-1]).date()


def decide_orders(account: AccountSpec, spec: StrategySpec,
                  frames: dict[str, pd.DataFrame], held: list[Held],
                  today: date, prev_trading_day: date, fee_adj_equity: float,
                  shortable: Callable[[str], bool]) -> Decision:
    dec = Decision()

    def cid(action, sym, sig):
        return client_order_id(account.key, today, action, sym, sig or today)

    # 1. loss limit (first, even if data is stale)
    if fee_adj_equity <= account.start_equity - LOSS_LIMIT_USD:
        dec.stopped = True
        dec.status = "stopped"
        for h in held:
            dec.orders.append(OrderIntent(
                h.symbol, _closing_side(h.side), h.qty, "stop_all",
                cid("stop_all", h.symbol, h.sig_date)))
        return dec

    # 2. staleness
    stale = {s for s, df in frames.items() if _last_date(df) != prev_trading_day}
    if frames and len(stale) / len(frames) > STALE_FRACTION:
        dec.status = "stale_data"
        dec.notes.append(f"stale_count:{len(stale)}/{len(frames)}")
        return dec
    for s in sorted(stale):
        dec.notes.append(f"stale:{s}")

    # 3. exits
    exits: list[OrderIntent] = []
    for h in held:
        if h.sig_date is None:
            dec.notes.append(f"untracked:{h.symbol}")
            continue
        df = frames.get(h.symbol)
        if df is None:
            dec.notes.append(f"untracked:{h.symbol}")
            continue
        if h.symbol in stale:
            continue
        idx = pd.DatetimeIndex(df.index).normalize()
        loc = idx.get_indexer([pd.Timestamp(h.sig_date)])[0]
        if loc < 0:
            dec.notes.append(f"untracked:{h.symbol}")
            continue
        j = len(df) - 1
        if j >= loc + 2 and spec.held_exit(df)(j, int(loc), h.avg_entry_price, h.side):
            exits.append(OrderIntent(h.symbol, _closing_side(h.side), h.qty, "exit",
                                     cid("exit", h.symbol, h.sig_date)))
    dec.orders.extend(exits)

    # 4. entries
    free = account.slots - len(held) + len(exits)
    if free <= 0:
        return dec
    held_syms = {h.symbol for h in held}
    cands = []
    for sym, df in frames.items():
        if sym in stale or sym in held_syms:
            continue
        ent = spec.entries(df)
        is_long = bool(ent["long"].iloc[-1])
        is_short = bool(ent["short"].iloc[-1])
        side = "long" if is_long else ("short" if is_short else None)
        if side is None:
            continue
        px = float(df["close"].iloc[-1])
        if not math.isfinite(px) or px < PRICE_FLOOR:
            continue
        shares = math.floor(SLOT_USD / px)
        if shares < 1:
            continue
        vol = df["volume"].iloc[-1] if "volume" in df else 0.0
        dv = px * float(vol) if pd.notna(vol) else 0.0
        cands.append((-dv, sym, side, shares))
    cands.sort(key=lambda x: (x[0], x[1]))
    taken = 0
    for _, sym, side, shares in cands:
        if taken >= free:
            break
        if side == "short" and not shortable(sym):
            dec.notes.append(f"skipped_unshortable:{sym}")
            continue
        action = "entry_long" if side == "long" else "entry_short"
        dec.orders.append(OrderIntent(
            sym, "buy" if side == "long" else "sell", shares, action,
            cid(action, sym, prev_trading_day)))
        taken += 1
    return dec
