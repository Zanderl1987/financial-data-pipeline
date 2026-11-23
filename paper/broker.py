"""Alpaca paper-trading adapter. Paper only: the client is built with paper=True
and nothing here accepts an endpoint. alpaca-py is imported lazily so unit tests
can inject fakes without the package."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from paper.decide import Held, OrderIntent, parse_client_order_id

NY = ZoneInfo("America/New_York")
_HISTORY_DAYS = 400


def _val(x):
    return str(getattr(x, "value", x)).lower()


def _ny(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=NY)
    return dt.astimezone(NY)


def _qty(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def fill_date(o, parsed: dict) -> date:
    """NY date an order (partly) filled. Alpaca leaves filled_at empty on a
    partially filled cls order that then expires or is canceled; a cls order
    can only fill at the close of the day it was placed, which is the `today`
    field of our client_order_id."""
    if o.filled_at is not None:
        return _ny(o.filled_at).date()
    return parsed["today"]


class Broker:
    def __init__(self, key: str, secret: str):
        from alpaca.trading.client import TradingClient
        self._c = TradingClient(key, secret, paper=True)

    # ---- calendar / clock -------------------------------------------------
    def _calendar(self, start: date, end: date):
        from alpaca.trading.requests import GetCalendarRequest
        return self._c.get_calendar(GetCalendarRequest(start=start, end=end))

    def clock(self) -> tuple[datetime, bool]:
        now_et = _ny(self._c.get_clock().timestamp)
        d = now_et.date()
        is_open_day = any(c.date == d for c in self._calendar(d, d))
        return now_et, is_open_day

    def close_et(self, d: date) -> datetime | None:
        for c in self._calendar(d, d):
            if c.date == d:
                return _ny(c.close)
        return None

    def prev_trading_day(self, d: date) -> date:
        days = [c.date for c in self._calendar(d - timedelta(days=14),
                                               d - timedelta(days=1))]
        return max(days)

    # ---- orders -----------------------------------------------------------
    def _all_orders(self, days: int = _HISTORY_DAYS) -> list:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        after = datetime.now(NY) - timedelta(days=days)
        until = None
        seen: dict[str, object] = {}
        while True:
            kw = dict(status=QueryOrderStatus.ALL, after=after, limit=500)
            if until is not None:
                kw["until"] = until
            batch = self._c.get_orders(GetOrdersRequest(**kw))
            fresh = [o for o in batch if str(o.client_order_id) not in seen]
            for o in fresh:
                seen[str(o.client_order_id)] = o
            if len(batch) < 500 or not fresh:
                break
            until = min(o.submitted_at for o in batch)
        return list(seen.values())

    @staticmethod
    def _filled(orders) -> list:
        """(order, parsed id) for our orders with any filled quantity, whatever
        the final status (filled, partially_filled, expired, canceled), oldest
        fill first. Quantities and prices come from filled_qty/filled_avg_price."""
        out = []
        for o in orders:
            if _qty(o.filled_qty) <= 0:
                continue
            p = parse_client_order_id(str(o.client_order_id))
            if p is None:
                continue
            out.append((o, p))
        out.sort(key=lambda t: (fill_date(*t), t[0].filled_at or t[0].submitted_at))
        return out

    def orders_today(self, prefix: str) -> list[str]:
        return [str(o.client_order_id) for o in self._all_orders(days=3)
                if str(o.client_order_id).startswith(prefix)]

    def recently_exited(self, prev_trading_day: date) -> set[str]:
        out = set()
        for o, p in self._filled(self._all_orders(days=10)):
            if p["action"] in ("exit", "stop_all") and                     fill_date(o, p) == prev_trading_day:
                out.add(str(o.symbol))
        return out

    def stop_dates(self) -> list[date]:
        """Dates of our stop_all orders, filled or not, oldest first. Any one
        latches the account's loss-limit stop (spec 5)."""
        out = []
        for o in self._all_orders():
            p = parse_client_order_id(str(o.client_order_id))
            if p is not None and p["action"] == "stop_all":
                out.append(p["today"])
        return sorted(out)

    def fee_inputs(self) -> tuple[float, list[tuple[str, float, date, date | None]]]:
        """Lots are (symbol, notional, opened, closed_or_None). A short entry
        opens a lot of its filled qty; a buy-to-cover (exit/stop_all) closes
        open lots of that symbol first-in-first-out, splitting a lot when it
        covers only part of it."""
        equity = float(self._c.get_account().equity)
        lots: list[list] = []          # [symbol, qty, price, opened, closed]
        for o, p in self._filled(self._all_orders()):
            fdate = fill_date(o, p)
            sym, fq = str(o.symbol), _qty(o.filled_qty)
            if p["action"] == "entry_short":
                lots.append([sym, fq, float(o.filled_avg_price), fdate, None])
            elif p["action"] in ("exit", "stop_all") and _val(o.side) == "buy":
                left = fq
                for lot in list(lots):
                    if left <= 0:
                        break
                    if lot[0] != sym or lot[4] is not None:
                        continue
                    take = min(left, lot[1])
                    if take < lot[1]:      # split: closed part + still-open rest
                        lots.insert(lots.index(lot),
                                    [sym, take, lot[2], lot[3], fdate])
                        lot[1] -= take
                    else:
                        lot[4] = fdate
                    left -= take
        return equity, [(s, q * px, a, b) for s, q, px, a, b in lots]

    def held(self) -> list[Held]:
        entries: dict[str, tuple] = {}
        for o, p in self._filled(self._all_orders()):
            if p["action"] in ("entry_long", "entry_short"):
                entries[str(o.symbol)] = (p["sig_date"], fill_date(o, p))
        out = []
        for pos in self._c.get_all_positions():
            sym = str(pos.symbol)
            sig, opened = entries.get(sym, (None, None))
            out.append(Held(sym, _val(pos.side), int(abs(float(pos.qty))),
                            float(pos.avg_entry_price), sig, opened))
        return out

    # ---- public read helpers (dashboard) ---------------------------------
    def calendar_days(self, start: date, end: date) -> list[date]:
        return [c.date for c in self._calendar(start, end)]

    def filled_orders(self, days: int = 60) -> list[tuple]:
        """(order, parsed_client_order_id) for our orders with any fill
        (including partial fills); date them with fill_date()."""
        return self._filled(self._all_orders(days=days))

    def portfolio_history(self, start: date, end: date | None = None):
        """Daily equity history over explicit start/end dates (not a rolling period)."""
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        kw = dict(timeframe="1D", start=datetime(start.year, start.month, start.day, tzinfo=NY))
        if end is not None:
            kw["end"] = datetime(end.year, end.month, end.day, 23, 59, tzinfo=NY)
        return self._c.get_portfolio_history(GetPortfolioHistoryRequest(**kw))

    def positions(self) -> list:
        return list(self._c.get_all_positions())

    def shortable(self, symbol: str) -> bool:
        a = self._c.get_asset(symbol)
        return bool(a.shortable and a.easy_to_borrow)

    def submit(self, intent: OrderIntent) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        side = OrderSide.BUY if intent.side == "buy" else OrderSide.SELL
        o = self._c.submit_order(MarketOrderRequest(
            symbol=intent.symbol, qty=intent.qty, side=side,
            time_in_force=TimeInForce.CLS,
            client_order_id=intent.client_order_id))
        return str(o.id)
