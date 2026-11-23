"""Pure helpers for the Streamlit dashboard. No network, no streamlit."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from paper.accounts import LOSS_LIMIT_USD
from paper.decide import accrued_borrow_fee

HORIZON = 63
TRADED = ("ok", "stale_data", "stopped")


def load_ranges(path: str | Path | None = None) -> dict:
    p = Path(path) if path else Path(__file__).with_name("expected_ranges.json")
    return json.loads(p.read_text(encoding="utf-8"))


def band_row(band: dict, day_index: int) -> tuple[float, float, float]:
    """(p5, p50, p95) at h = min(day_index, 63); day 0 uses h=1."""
    i = max(min(day_index, HORIZON), 1) - 1
    return band["p5"][i], band["p50"][i], band["p95"][i]


def account_status(day_index: int, ret: float, band: dict, stopped: bool) -> str:
    if stopped:
        return "stopped"
    p5, _, p95 = band_row(band, day_index)
    if ret < p5:
        return "behind"
    if ret > p95:
        return "above"
    return "on track"


def verdict(final_ret: float, band_63: tuple[float, float], stopped: bool) -> str:
    lo, hi = band_63
    if stopped or final_ret < lo:
        return "FAIL"
    if final_ret > hi:
        return "PASS (flagged: above range)"
    return "PASS"


def missed_days(trading_days: list[date], log_rows: list[dict],
                account: str) -> list[date]:
    done = {str(r.get("date"))[:10] for r in log_rows
            if r.get("account") == account and r.get("status") in TRADED}
    return [d for d in trading_days if d.isoformat() not in done]


def trade_costs(fills: pd.DataFrame, closes: pd.DataFrame) -> pd.DataFrame:
    """fills: symbol, side, date, price, qty. closes: wide, index=date, cols=symbol.
    Positive cost_bps = filled worse than that day's close."""
    out = fills.copy()
    cl = [closes.at[d, s] if (d in closes.index and s in closes.columns) else float("nan")
          for d, s in zip(out["date"], out["symbol"])]
    out["close"] = cl
    sign = out["side"].astype(str).str.lower().map({"buy": 1.0, "sell": -1.0})
    out["cost_bps"] = sign * (out["price"] - out["close"]) / out["close"] * 1e4
    return out


def parse_runlog(text: str) -> list[dict]:
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def parse_launch(s) -> date | None:
    try:
        return date.fromisoformat(str(s).strip())
    except (ValueError, TypeError):
        return None


def day_index(trading_days: list[date], launch: date | None, today: date) -> int:
    if launch is None:
        return 0
    return sum(1 for d in trading_days if launch <= d <= today)


def fee_adjusted(equity: float, short_lots: list, today: date) -> float:
    return equity - accrued_borrow_fee(short_lots, today)


def limit_distance(start_equity: float, adj_equity: float,
                   limit: float = LOSS_LIMIT_USD) -> float:
    """Dollars of room left before the loss limit (negative = breached)."""
    return adj_equity - (start_equity - limit)
