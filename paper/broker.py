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
        out = []
        for o in orders:
            if _val(o.status) != "filled" or o.filled_at is None:
                continue
            p = parse_client_order_id(str(o.client_order_id))
            if p is None:
                continue
            out.append((o, p))
        out.sort(key=lambda t: t[0].filled_at)
        return out

    def orders_today(self, prefix: str) -> list[str]:
        return [str(o.client_order_id) for o in self._all_orders(days=3)
                if str(o.client_order_id).startswith(prefix)]

    def recently_exited(self, prev_trading_day: date) -> set[str]:
        out = set()
        for o, p in self._filled(self._all_orders(days=10)):
            if p["action"] in ("exit", "stop_all") and \
                    _ny(o.filled_at).date() == prev_trading_day:
                out.add(str(o.symbol))
        return out

    def fee_inputs(self) -> tuple[float, list[tuple[float, date, date | None]]]:
        equity = float(self._c.get_account().equity)
        lots: list[list] = []
        for o, p in self._filled(self._all_orders()):
            fdate = _ny(o.filled_at).date()
            if p["action"] == "entry_short":
                notional = float(o.filled_qty) * float(o.filled_avg_price)
                lots.append([notional, fdate, None, str(o.symbol)])
            elif p["action"] in ("exit", "stop_all"):
                for lot in lots:
                    if lot[3] == str(o.symbol) and lot[2] is None:
                        lot[2] = fdate
                        break
        return equity, [(n, a, b) for n, a, b, _ in lots]

    def held(self) -> list[Held]:
        entries: dict[str, tuple] = {}
        for o, p in self._filled(self._all_orders()):
            if p["action"] in ("entry_long", "entry_short"):
                entries[str(o.symbol)] = (p["sig_date"], _ny(o.filled_at).date())
        out = []
        for pos in self._c.get_all_positions():
            sym = str(pos.symbol)
            sig, opened = entries.get(sym, (None, None))
            out.append(Held(sym, _val(pos.side), int(abs(float(pos.qty))),
                            float(pos.avg_entry_price), sig, opened))
        return out

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
