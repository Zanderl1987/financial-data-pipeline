"""Pure helpers for the Streamlit dashboard. No network, no streamlit."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from paper.accounts import LOSS_LIMIT_USD
from paper.decide import accrued_borrow_fee

HORIZON = 63
NY = ZoneInfo("America/New_York")
LIMIT_VERDICT = "HIT YOUR LOSS LIMIT"
# A day counts as done only if a run decided and placed (or had nothing to
# place): stale_data, error, missed_cutoff and dry_run days are MISSED.
TRADED = ("ok", "stopped")


def load_ranges(path: str | Path | None = None) -> dict:
    p = Path(path) if path else Path(__file__).with_name("expected_ranges.json")
    return json.loads(p.read_text(encoding="utf-8"))


def band_row(band: dict, day_index: int) -> tuple[float, float, float]:
    """(p5, p50, p95) at h = min(day_index, 63); day 0 uses h=1."""
    i = max(min(day_index, HORIZON), 1) - 1
    return band["p5"][i], band["p50"][i], band["p95"][i]


def account_status(day_index: int, ret: float, band: dict, stopped: bool) -> str:
    """Card status while running. `stopped` = the job's latch (a stop_all order
    exists), not the live distance to the limit."""
    if stopped:
        return "stopped (hit your loss limit)"
    p5, _, p95 = band_row(band, day_index)
    if ret < p5:
        return "behind"
    if ret > p95:
        return "above"
    return "on track"


def verdict(final_ret: float, band_63: tuple[float, float]) -> str:
    """Band verdict on the frozen day-63 return (loss-limit hits are judged
    first, by final_verdict)."""
    lo, hi = band_63
    if final_ret < lo:
        return "FAIL"
    if final_ret > hi:
        return "PASS (flagged: above range)"
    return "PASS"


def band_position(ret: float, lo: float, hi: float) -> str:
    if ret < lo:
        return "below range"
    if ret > hi:
        return "above range"
    return "inside range"


def frozen_fee(lots: list, day: date) -> float:
    """Borrow fee accrued as of `day`: lots opened later are ignored, closes
    after `day` are capped at `day`."""
    frozen = [(n, a, None if b is None or b > day else b)
              for _, n, a, b in lots if a <= day]
    return accrued_borrow_fee(frozen, day)


def equity_at(day: date, hist_t: list[date], hist_e: list[float]) -> float | None:
    pts = [(t, e) for t, e in zip(hist_t, hist_e) if t <= day]
    return max(pts, key=lambda x: x[0])[1] if pts else None


def loss_limit_hit(days: list[date], hist_t: list[date], hist_e: list[float],
                   lots: list, start_equity: float, stop_dates: list[date],
                   limit: float = LOSS_LIMIT_USD) -> date | None:
    """First date the account hit its loss limit within the 63-day test:
    a completed close (days[:63]) with fee-adjusted equity <= start - limit, or
    a stop_all order dated on/before day 63 (any date while day 63 is still
    ahead). None if neither."""
    window = days[:HORIZON]
    inwin = set(window)
    floor = start_equity - limit
    breaches = sorted(t for t, e in zip(hist_t, hist_e)
                      if t in inwin and e - frozen_fee(lots, t) <= floor)
    stops = sorted(stop_dates)
    if len(days) >= HORIZON:
        stops = [s for s in stops if s <= days[HORIZON - 1]]
    cands = breaches[:1] + stops[:1]
    return min(cands) if cands else None


def final_verdict(days: list[date], hist_t: list[date], hist_e: list[float],
                  lots: list, start_equity: float, band: dict,
                  stop_dates: list[date]) -> dict | None:
    """Spec 5 outcome. (a) loss limit hit by day 63 -> HIT YOUR LOSS LIMIT,
    with where the return stood against the band that day; (b) otherwise the
    frozen day-63 fee-adjusted return against the day-63 band. Nothing after
    day 63 changes it. None while pending. `days` = completed trading days
    since launch."""
    hit = loss_limit_hit(days, hist_t, hist_e, lots, start_equity, stop_dates)
    if hit is not None:
        h = min(sum(1 for d in days if d < hit) + 1, HORIZON)
        eq = equity_at(hit, hist_t, hist_e)
        ret = None if eq is None else             (eq - frozen_fee(lots, hit) - start_equity) / start_equity
        p5, _, p95 = band_row(band, h)
        return {"verdict": LIMIT_VERDICT, "date": hit, "day": h, "return": ret,
                "band": (p5, p95),
                "vs_band": None if ret is None else band_position(ret, p5, p95)}
    veq = verdict_equity(days, hist_t, hist_e, lots)
    if veq is None:
        return None
    ret = (veq - start_equity) / start_equity
    p5, p95 = band["p5"][HORIZON - 1], band["p95"][HORIZON - 1]
    return {"verdict": verdict(ret, (p5, p95)), "date": days[HORIZON - 1],
            "day": HORIZON, "return": ret, "band": (p5, p95),
            "vs_band": band_position(ret, p5, p95)}


def describe_verdict(v: dict | None) -> str:
    if not v:
        return ""
    lo, hi = v["band"]
    ret = "return n/a" if v["return"] is None else f"{v['return']:+.2%}"
    where = f", {v['vs_band']}" if v["vs_band"] else ""
    return f"day {v['day']} ({v['date']:%Y-%m-%d}): {ret}{where} [{lo:+.2%}, {hi:+.2%}]"


def history_dates(timestamps: list[int]) -> list[date]:
    """Alpaca 1D portfolio-history timestamps -> New York trading dates.
    Converted to America/New_York explicitly. A daily point is labeled at the
    start of its day: midnight New York, or midnight UTC (19:00/20:00 New York
    the evening before). No daily label for day D falls in New York's evening,
    so an evening time is read as the next day's UTC-midnight label."""
    out = []
    for t in timestamps:
        dt = datetime.fromtimestamp(int(t), timezone.utc).astimezone(NY)
        d = dt.date()
        if dt.hour >= 18:
            d += timedelta(days=1)
        out.append(d)
    return out


def missed_days(trading_days: list[date], log_rows: list[dict],
                account: str) -> list[tuple[date, str]]:
    """(day, reason) for each trading day with no "ok"/"stopped" record for
    the account. Reason = the status of that day's last record (stale_data,
    error, missed_cutoff, dry_run, ...), or "no record"."""
    done, last = set(), {}
    for r in log_rows:
        if r.get("account") != account:
            continue
        day = str(r.get("date"))[:10]
        last[day] = str(r.get("status"))
        if r.get("status") in TRADED:
            done.add(day)
    return [(d, last.get(d.isoformat(), "no record")) for d in trading_days
            if d.isoformat() not in done]


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


def strip_symbols(lots: list) -> list:
    """Broker lots (symbol, notional, opened, closed) -> decide's (notional, opened, closed)."""
    return [(n, a, b) for _, n, a, b in lots]


def fee_adjusted(equity: float, lots: list, today: date) -> float:
    return equity - accrued_borrow_fee(strip_symbols(lots), today)


def complete_days(days: list[date], now_et: datetime,
                  cutoff: time = time(15, 40)) -> list[date]:
    """Drop today's unfinished day until after the cutoff (NY time)."""
    if days and days[-1] == now_et.date() and now_et.time() < cutoff:
        return days[:-1]
    return days


def verdict_equity(days: list[date], hist_t: list[date], hist_e: list[float],
                   lots: list) -> float | None:
    """Fee-adjusted equity at the close of the 63rd trading day since launch,
    frozen: later values are never used. None until that day is complete.
    `days` = completed trading days since launch."""
    if len(days) < HORIZON:
        return None
    d63 = days[HORIZON - 1]
    eq = equity_at(d63, hist_t, hist_e)
    if eq is None:
        return None
    return eq - frozen_fee(lots, d63)


def net_position_pnl(positions: list[dict], lots: list, today: date) -> list[dict]:
    """Add pnl_net: shorts net of the accrued borrow fee on their open lot."""
    out = []
    for p in positions:
        q = dict(p)
        fee = 0.0
        if str(p.get("side", "")).lower() == "short":
            open_lots = [(n, a, b) for sym, n, a, b in lots
                         if sym == p["symbol"] and b is None]
            fee = accrued_borrow_fee(open_lots, today)
        q["pnl_net"] = p["pnl"] - fee
        out.append(q)
    return out


def scale_band(band: dict, start_equity: float, n: int) -> dict:
    """Fraction band -> dollar band for the first n horizons."""
    return {k: [start_equity * (1 + v) for v in band[k][:n]] for k in ("p5", "p50", "p95")}


def limit_distance(start_equity: float, adj_equity: float,
                   limit: float = LOSS_LIMIT_USD) -> float:
    """Dollars of room left before the loss limit (negative = breached)."""
    return adj_equity - (start_equity - limit)
