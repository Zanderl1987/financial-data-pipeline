"""
Event-study backtester — "what happens after X?" for any event stream.

Complements backtest.py (cross-sectional quantile portfolios) with the other
half of backtesting: conditional analysis around discrete events. Feed it any
(date) or (symbol, date) event list and it measures the price path before and
after, with abnormal returns vs a benchmark, cross-event t-stats, and an
unconditional base rate so you can see whether the event actually carries
edge. A scenario tester turns the same events into a trade list (entry lag,
holding period, stop loss / take profit) with win rates and an equity curve.

Built-in event generators cover the questions this repo's data can answer:

  earnings_events()    — EPS beats/misses (earnings_calendar)
  filing_events()      — SEC filings by form type (sec_filings)
  drawdown_events()    — market fell X% over N days (any price series)
  price_move_events()  — any asset moved X% in N days (e.g. oil surge)
  threshold_events()   — series crossed a level (e.g. VIX > 30)
  technical_events()   — TA signal fired (golden cross, RSI, TV rating, …)

Usage
-----
    import event_backtest as eb

    # market reaction to earnings beats, 60 trading days forward
    ev = eb.earnings_events(min_surprise_pct=5)
    res = eb.event_study(ev, window=(-5, 60), benchmark="SPY")
    print(res.summary())

    # what happens to airlines after oil surges 15% in 10 days
    oil = eb.price_move_events("CL=F", pct=15, days=10)
    res = eb.event_study(oil, symbols=["DAL", "UAL", "LUV"], window=(-10, 40))

    # trade every "market down 5% in 5 days" event, hold 21 days
    dd = eb.drawdown_events("^GSPC", pct=5, days=5)
    trades = eb.scenario(dd, symbols="SPY", holding_days=21, stop_loss_pct=5)
    print(trades.summary())
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query as q

HORIZONS = (1, 3, 5, 10, 21, 63)

# price tables searched in order; (table, close preference)
_PRICE_TABLES = ("tiingo_prices", "prices", "market_history", "sector_etfs")


# ------------------------------------------------------------------ prices

def _table_has(table: str, symbol: str) -> bool:
    try:
        return not q.load(table, symbol=symbol, limit=1).empty
    except Exception:
        return False


def load_close(symbol: str, start: "str | None" = None,
               end: "str | None" = None,
               price_table: "str | None" = None) -> pd.Series:
    """
    Daily (adjusted-when-available) close series for one symbol. Every price
    table is checked and the longest series wins, so a deep source
    (market_history) beats a shallow one (a recent tiingo watchlist pull).
    Date-indexed float series, ascending.
    """
    tables = [price_table] if price_table else list(_PRICE_TABLES)
    best = pd.Series(dtype=float, name=symbol)
    for t in tables:
        try:
            df = q.load(t, symbol=symbol, start=start, end=end)
        except Exception:
            continue
        if df.empty:
            continue
        col = "adj_close" if "adj_close" in df.columns and df["adj_close"].notna().any() else "close"
        s = (df.assign(date=pd.to_datetime(df["date"]))
               .drop_duplicates("date").sort_values("date")
               .set_index("date")[col].astype(float).dropna())
        if len(s) > len(best):
            s.name = symbol
            best = s
    return best


def load_close_matrix(symbols, start=None, end=None,
                      price_table: "str | None" = None) -> pd.DataFrame:
    """Wide close matrix (date x symbol) from whichever tables carry each symbol."""
    out = {}
    for sym in dict.fromkeys(symbols):          # dedupe, keep order
        s = load_close(sym, start, end, price_table)
        if not s.empty:
            out[sym] = s
    return pd.DataFrame(out).sort_index()


# ------------------------------------------------------------ event studies

@dataclass
class EventStudyResult:
    """Cross-event aggregation of price paths around events."""
    car: pd.DataFrame            # per-event cumulative returns (event x rel_day)
    mean_car: pd.Series          # mean CAR curve across events
    horizons: pd.DataFrame       # stats table at fixed horizons
    events: pd.DataFrame         # the (aligned) events actually used
    baseline: pd.Series          # unconditional mean cum-return at same horizons
    params: dict = field(default_factory=dict)

    @property
    def n_events(self) -> int:
        return len(self.car)

    def summary(self) -> pd.DataFrame:
        """Horizon table: mean/median CAR, hit rate, t-stat, and edge vs base rate."""
        return self.horizons

    def __repr__(self) -> str:
        h = self.horizons
        peek = ""
        if not h.empty and 21 in h.index:
            r = h.loc[21]
            peek = (f" CAR21={r['mean_pct']}% hit={r['hit_rate_pct']}% "
                    f"t={r['t_stat']}")
        return f"<EventStudyResult n={self.n_events}{peek}>"


def _align_to_index(dates: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Snap each event date to the first trading day >= it (NaT if beyond data)."""
    pos = index.searchsorted(pd.DatetimeIndex(dates), side="left")
    ok = pos < len(index)
    out = pd.Series(pd.NaT, index=dates.index, dtype="datetime64[ns]")
    out[ok] = index[pos[ok]]
    return out


def _rel_path(close: pd.Series, t0_loc: int, pre: int, post: int) -> "np.ndarray | None":
    """Cumulative return path vs close[t0-1], for rel days pre..post."""
    lo, hi = t0_loc + pre - 1, t0_loc + post
    if lo < 0 or hi >= len(close):
        return None
    base = close.iloc[t0_loc - 1] if t0_loc > 0 else np.nan
    if not np.isfinite(base) or base <= 0:
        return None
    window = close.iloc[lo + 1: hi + 1].to_numpy()
    return window / base - 1.0


def event_study(
    events: pd.DataFrame,
    symbols: "list[str] | str | None" = None,
    window: "tuple[int, int]" = (-10, 63),
    benchmark: "str | None" = None,
    entry_lag: int = 0,
    price_table: "str | None" = None,
    min_gap_days: int = 0,
) -> EventStudyResult:
    """
    Measure the average price path around events.

    Parameters
    ----------
    events      : DataFrame with a 'date' column; a 'symbol' column makes it a
                  per-stock study. Extra columns are carried through.
    symbols     : measure these symbols' reaction instead of the event's own
                  symbol (e.g. how airlines react to oil shocks). With a list,
                  every event is crossed with every symbol.
    window      : (pre, post) in trading days around the event, e.g. (-10, 63).
    benchmark   : subtract this symbol's matching path (abnormal returns).
    entry_lag   : shift day 0 forward N trading days (use 1 when the event
                  timestamp is only known to be "sometime that day").
    price_table : force a specific price source.
    min_gap_days: drop events within N calendar days of the previous event for
                  the same symbol (de-clusters overlapping signals).

    Returns EventStudyResult. CAR(h) is the cumulative return from the close
    of the day before day 0 through the close of relative day h; day 0 itself
    is the event day's reaction.
    """
    if "date" not in events.columns:
        raise ValueError("events must have a 'date' column")
    ev = events.copy()
    ev["date"] = pd.to_datetime(ev["date"])
    ev = ev.dropna(subset=["date"]).sort_values("date")

    if symbols is not None:
        if isinstance(symbols, str):
            symbols = [symbols]
        ev = ev.drop(columns=[c for c in ("symbol",) if c in ev.columns])
        ev = ev.merge(pd.DataFrame({"symbol": symbols}), how="cross")
    if "symbol" not in ev.columns:
        raise ValueError("events need a 'symbol' column or pass symbols=...")
    if benchmark:
        # a symbol benchmarked against itself is identically zero and only
        # dilutes the cross-event stats
        ev = ev[ev["symbol"] != benchmark]

    if min_gap_days > 0:
        keep = []
        for _, grp in ev.groupby("symbol"):
            last = None
            for i, d in grp["date"].items():
                if last is None or (d - last).days >= min_gap_days:
                    keep.append(i)
                    last = d
        ev = ev.loc[sorted(keep)]

    pre, post = int(window[0]), int(window[1])
    if pre > 0:
        pre = -pre
    rel_days = np.arange(pre, post + 1)

    closes = load_close_matrix(ev["symbol"].unique(), price_table=price_table)
    if closes.empty:
        raise RuntimeError("No price data found for any event symbol.")
    bench = load_close(benchmark, price_table=price_table) if benchmark else None
    if benchmark and (bench is None or bench.empty):
        raise RuntimeError(f"No price data for benchmark '{benchmark}'.")

    rows, meta = [], []
    for _, e in ev.iterrows():
        sym = e["symbol"]
        if sym not in closes.columns:
            continue
        s = closes[sym].dropna()
        loc = s.index.searchsorted(e["date"], side="left") + entry_lag
        if loc >= len(s):
            continue
        # the snapped trading day must actually be near the event — otherwise
        # the symbol's history simply doesn't cover this event
        if abs((s.index[loc] - e["date"]).days) > 10 + entry_lag * 5:
            continue
        path = _rel_path(s, loc, pre, post)
        if path is None:
            continue
        if bench is not None:
            bloc = bench.index.searchsorted(s.index[loc], side="left")
            bpath = _rel_path(bench, bloc, pre, post)
            if bpath is None:
                continue
            path = path - bpath
        rows.append(path)
        meta.append({**e.to_dict(), "day0": s.index[loc]})

    if not rows:
        raise RuntimeError("No events had enough surrounding price history.")

    car = pd.DataFrame(rows, columns=rel_days)
    used = pd.DataFrame(meta)
    mean_car = car.mean(axis=0)

    # unconditional base rate: mean h-day forward return over all days/symbols
    daily = closes.pct_change()
    baseline = pd.Series(
        {h: float((daily.mean(axis=1, skipna=True) + 1).rolling(h).apply(np.prod).mean() - 1)
         for h in HORIZONS if h <= post}, name="baseline")

    hrows = {}
    for h in [h for h in HORIZONS if h <= post]:
        col = car[h].dropna()
        n = len(col)
        mean, sd = float(col.mean()), float(col.std(ddof=1))
        t = mean / (sd / math.sqrt(n)) if n > 1 and sd > 0 else float("nan")
        hrows[h] = {
            "n": n,
            "mean_pct": round(100 * mean, 2),
            "median_pct": round(100 * float(col.median()), 2),
            "hit_rate_pct": round(100 * float((col > 0).mean()), 1),
            "t_stat": round(t, 2),
            "baseline_pct": round(100 * baseline.get(h, np.nan), 2),
            "edge_pct": round(100 * (mean - baseline.get(h, np.nan)), 2)
                        if h in baseline.index else None,
        }
    horizons = pd.DataFrame.from_dict(hrows, orient="index")
    horizons.index.name = "horizon_days"

    params = {"window": (pre, post), "benchmark": benchmark,
              "entry_lag": entry_lag, "n_events_in": len(ev),
              "n_events_used": len(car), "min_gap_days": min_gap_days}
    return EventStudyResult(car=car, mean_car=mean_car, horizons=horizons,
                            events=used, baseline=baseline, params=params)


# ---------------------------------------------------------- scenario trades

@dataclass
class ScenarioResult:
    """Trade-by-trade outcome of acting on every event."""
    trades: pd.DataFrame         # entry/exit/return per trade
    equity: pd.Series            # daily equity of an equal-weight overlay
    metrics: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)

    def summary(self) -> pd.DataFrame:
        return pd.DataFrame.from_dict({**self.params, **self.metrics},
                                      orient="index", columns=["value"])

    def __repr__(self) -> str:
        m = self.metrics
        return (f"<ScenarioResult trades={m.get('n_trades')} "
                f"win={m.get('win_rate_pct')}% avg={m.get('avg_return_pct')}% "
                f"pf={m.get('profit_factor')}>")


def scenario(
    events: pd.DataFrame,
    symbols: "list[str] | str | None" = None,
    holding_days: int = 21,
    side: str = "long",
    entry_lag: int = 1,
    stop_loss_pct: "float | None" = None,
    take_profit_pct: "float | None" = None,
    cost_bps: float = 0.0,
    price_table: "str | None" = None,
    min_gap_days: int = 0,
) -> ScenarioResult:
    """
    Simulate trading every event: enter at the close `entry_lag` trading days
    after the event, exit after `holding_days` closes or when a close breaches
    the stop-loss / take-profit level, whichever comes first.

    side='short' flips the position. cost_bps is charged round-trip.
    Overlapping trades are combined into an equal-weight daily overlay for the
    equity curve; per-trade stats are independent of overlap.
    """
    res = event_study(events, symbols=symbols, window=(0, holding_days),
                      entry_lag=entry_lag, price_table=price_table,
                      min_gap_days=min_gap_days)
    sign = -1.0 if side == "short" else 1.0
    cost = cost_bps / 1e4

    trades = []
    daily_rets: dict[pd.Timestamp, list] = {}
    closes = load_close_matrix(res.events["symbol"].unique(), price_table=price_table)

    for i, e in res.events.iterrows():
        path = res.car.loc[i]                      # cum return from entry close
        # exit day: stop/take-profit on closes, else final day
        exit_rel = holding_days
        reason = "time"
        for rel in range(1, holding_days + 1):
            r = sign * path[rel]
            if stop_loss_pct is not None and r <= -abs(stop_loss_pct) / 100:
                exit_rel, reason = rel, "stop"
                break
            if take_profit_pct is not None and r >= abs(take_profit_pct) / 100:
                exit_rel, reason = rel, "target"
                break
        gross = sign * path[exit_rel]
        net = gross - cost
        s = closes[e["symbol"]].dropna()
        loc = s.index.get_loc(e["day0"])
        exit_loc = min(loc + exit_rel, len(s) - 1)
        trades.append({
            "symbol": e["symbol"], "event_date": e.get("date"),
            "entry_date": e["day0"], "exit_date": s.index[exit_loc],
            "days_held": exit_rel, "exit_reason": reason,
            "return_pct": round(100 * net, 2),
        })
        # spread daily returns of this trade into the overlay
        seg = s.iloc[loc: exit_loc + 1].pct_change().dropna() * sign
        for d, r in seg.items():
            daily_rets.setdefault(d, []).append(r)

    tdf = pd.DataFrame(trades).sort_values("entry_date").reset_index(drop=True)
    overlay = pd.Series({d: np.mean(rs) for d, rs in daily_rets.items()}).sort_index()
    equity = (1 + overlay).cumprod()

    rets = tdf["return_pct"] / 100
    wins, losses = rets[rets > 0], rets[rets <= 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")
    metrics = {
        "n_trades": len(tdf),
        "win_rate_pct": round(100 * float((rets > 0).mean()), 1),
        "avg_return_pct": round(100 * float(rets.mean()), 2),
        "median_return_pct": round(100 * float(rets.median()), 2),
        "best_pct": round(float(tdf["return_pct"].max()), 2),
        "worst_pct": round(float(tdf["return_pct"].min()), 2),
        "profit_factor": round(pf, 2),
        "expectancy_pct": round(100 * float(rets.mean()), 2),
        "overlay_total_return_pct": round(100 * float(equity.iloc[-1] - 1), 2) if len(equity) else 0.0,
        "stops_hit": int((tdf["exit_reason"] == "stop").sum()),
        "targets_hit": int((tdf["exit_reason"] == "target").sum()),
    }
    params = {"holding_days": holding_days, "side": side, "entry_lag": entry_lag,
              "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct,
              "cost_bps": cost_bps}
    return ScenarioResult(trades=tdf, equity=equity, metrics=metrics, params=params)


# ------------------------------------------------------- event generators

def earnings_events(
    symbols: "list[str] | str | None" = None,
    beat: "bool | None" = None,
    min_surprise_pct: "float | None" = None,
    start: "str | None" = None,
) -> pd.DataFrame:
    """
    Earnings reports with real historical dates + EPS surprises, sourced from
    alpha_vantage_earnings (deep per-symbol history back to the 1990s, quota-
    gated coverage that grows daily via alpha_vantage_fundamentals_pipeline.py's
    rotating symbol subset). earnings_calendar (Finnhub) is NOT used here —
    verified live 2026-07-29 that its free tier only returns a rolling ~2-month
    window and never has real historical actuals (2015/2020/2022/2023 and even
    2026-01/04/05 all returned 0 rows).

    beat=True keeps beats, False keeps misses; min_surprise_pct filters by
    |EPS surprise %|. Columns: symbol, date, eps_estimate, eps_actual,
    surprise_pct. Note: use entry_lag=1 in event_study/scenario — report
    timing (BMO/AMC) isn't reliable enough to trade the same close.
    """
    df = q.load("alpha_vantage_earnings")
    if df.empty:
        return df
    df = df[df["report_type"] == "quarterly"].copy()
    df = df.rename(columns={
        "ticker": "symbol",
        "reportedDate": "date",
        "estimatedEPS": "eps_estimate",
        "reportedEPS": "eps_actual",
        "surprisePercentage": "surprise_pct",
    })
    for col in ("eps_estimate", "eps_actual", "surprise_pct"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "eps_actual", "eps_estimate", "surprise_pct"])
    if symbols is not None:
        symbols = [symbols] if isinstance(symbols, str) else list(symbols)
        df = df[df["symbol"].isin(symbols)]
    if start is not None:
        df = df[df["date"] >= pd.Timestamp(start)]
    if beat is True:
        df = df[df["surprise_pct"] > 0]
    elif beat is False:
        df = df[df["surprise_pct"] < 0]
    if min_surprise_pct is not None:
        df = df[df["surprise_pct"].abs() >= min_surprise_pct]
    return (df[["symbol", "date", "eps_estimate", "eps_actual", "surprise_pct"]]
            .sort_values("date").reset_index(drop=True))


def filing_events(
    forms: "list[str] | str" = "8-K",
    symbols: "list[str] | str | None" = None,
    start: "str | None" = None,
) -> pd.DataFrame:
    """
    SEC filings from the sec_filings table as events (date = filed date).
    Columns: symbol, date, form, company. Filings can land after hours —
    use entry_lag=1 to trade them.
    """
    if isinstance(forms, str):
        forms = [forms]
    df = q.load("sec_filings", symbol=symbols, start=start)
    if df.empty:
        return df
    df = df[df["form"].str.upper().isin([f.upper() for f in forms])]
    df = df.dropna(subset=["symbol"])
    return (df.rename(columns={"filed": "date"})
              [["symbol", "date", "form", "company"]]
              .sort_values("date").reset_index(drop=True))


def price_move_events(
    symbol: str,
    pct: float,
    days: int = 5,
    direction: "str | None" = None,
    price_table: "str | None" = None,
    start: "str | None" = None,
    min_gap_days: int = 10,
) -> pd.DataFrame:
    """
    Dates when `symbol` moved at least `pct` percent over `days` trading days.

    pct > 0 or direction='up' catches surges (oil +15%); pct < 0 or
    direction='down' catches slides. Consecutive qualifying days are
    de-clustered with min_gap_days (keep the first day of each episode).
    Returns date-only events (pass symbols=... to event_study to pick what
    reacts). Columns: date, trigger_symbol, move_pct.
    """
    s = load_close(symbol, start=start, price_table=price_table)
    if s.empty:
        raise RuntimeError(f"No price data for '{symbol}'.")
    move = s.pct_change(days) * 100
    if direction == "down" or (direction is None and pct < 0):
        hits = move[move <= -abs(pct)]
    else:
        hits = move[move >= abs(pct)]
    ev = pd.DataFrame({"date": hits.index, "trigger_symbol": symbol,
                       "move_pct": hits.round(2).values})
    if min_gap_days > 0 and not ev.empty:
        keep, last = [], None
        for i, d in ev["date"].items():
            if last is None or (d - last).days >= min_gap_days:
                keep.append(i)
                last = d
        ev = ev.loc[keep]
    return ev.reset_index(drop=True)


def drawdown_events(symbol: str = "^GSPC", pct: float = 5, days: int = 5,
                    **kwargs) -> pd.DataFrame:
    """'Market fell X% over N days' — sugar for price_move_events(down)."""
    return price_move_events(symbol, pct=-abs(pct), days=days,
                             direction="down", **kwargs)


def threshold_events(
    symbol: str,
    level: float,
    direction: str = "above",
    price_table: "str | None" = None,
    start: "str | None" = None,
    min_gap_days: int = 10,
) -> pd.DataFrame:
    """
    Dates when a series first crosses a level (e.g. VIX crosses above 30).
    Only the crossing day fires, then re-arms after it crosses back.
    Columns: date, trigger_symbol, value.
    """
    s = load_close(symbol, start=start, price_table=price_table)
    if s.empty:
        raise RuntimeError(f"No price data for '{symbol}'.")
    above = s > level if direction == "above" else s < level
    cross = above & ~above.shift(1, fill_value=False)
    hits = s[cross]
    ev = pd.DataFrame({"date": hits.index, "trigger_symbol": symbol,
                       "value": hits.round(2).values})
    if min_gap_days > 0 and not ev.empty:
        keep, last = [], None
        for i, d in ev["date"].items():
            if last is None or (d - last).days >= min_gap_days:
                keep.append(i)
                last = d
        ev = ev.loc[keep]
    return ev.reset_index(drop=True)


# named technical signals: fn(ind) -> boolean Series (ind = indicators frame)
_TECH_SIGNALS = {
    "golden_cross":  lambda d: (d["sma50"] > d["sma200"]) & (d["sma50"].shift(1) <= d["sma200"].shift(1)),
    "death_cross":   lambda d: (d["sma50"] < d["sma200"]) & (d["sma50"].shift(1) >= d["sma200"].shift(1)),
    "rsi_oversold":  lambda d: (d["rsi14"] < 30) & (d["rsi14"].shift(1) >= 30),
    "rsi_overbought": lambda d: (d["rsi14"] > 70) & (d["rsi14"].shift(1) <= 70),
    "macd_cross_up": lambda d: (d["macd"] > d["macd_signal"]) & (d["macd"].shift(1) <= d["macd_signal"].shift(1)),
    "macd_cross_down": lambda d: (d["macd"] < d["macd_signal"]) & (d["macd"].shift(1) >= d["macd_signal"].shift(1)),
    "tv_strong_buy": lambda d: (d["rating_all"] >= 0.5) & (d["rating_all"].shift(1) < 0.5),
    "tv_strong_sell": lambda d: (d["rating_all"] <= -0.5) & (d["rating_all"].shift(1) > -0.5),
    "tv_buy":        lambda d: (d["rating_all"] >= 0.1) & (d["rating_all"].shift(1) < 0.1),
    "tv_sell":       lambda d: (d["rating_all"] <= -0.1) & (d["rating_all"].shift(1) > -0.1),
}


def technical_events(
    symbols: "list[str] | str",
    signal="golden_cross",
    price_table: "str | None" = None,
    start: "str | None" = None,
) -> pd.DataFrame:
    """
    Dates when a technical signal fires for each symbol.

    signal: one of {names} or any callable(indicator_frame) -> boolean Series.
    The frame passed to callables has every analytics.technical indicator plus
    rating_all/rating_ma/rating_osc, so custom conditions can combine them.
    Columns: symbol, date, signal.
    """
    from analytics.technical import rating_history
    if isinstance(symbols, str):
        symbols = [symbols]
    fn = _TECH_SIGNALS[signal] if isinstance(signal, str) else signal
    name = signal if isinstance(signal, str) else getattr(signal, "__name__", "custom")

    frames = []
    for sym in symbols:
        d = rating_history(sym, price_table=price_table, start=start)
        if d.empty:
            continue
        fired = fn(d).fillna(False)
        if fired.any():
            frames.append(pd.DataFrame({"symbol": sym,
                                        "date": d.index[fired],
                                        "signal": name}))
    return (pd.concat(frames, ignore_index=True).sort_values("date")
              .reset_index(drop=True)
            if frames else pd.DataFrame(columns=["symbol", "date", "signal"]))


technical_events.__doc__ = technical_events.__doc__.format(
    names=sorted(_TECH_SIGNALS))


# ------------------------------------------------- rating-change scanning

_RATING_ORDER = ["strong_sell", "sell", "neutral", "buy", "strong_buy"]
_RATING_ORD = {lab: i for i, lab in enumerate(_RATING_ORDER)}

_CHANGE_COLS = ["symbol", "date", "from_label", "to_label",
                "from_score", "to_score", "step", "direction"]


def _apply_change_filters(ev: pd.DataFrame, direction: "str | None",
                          min_step: int) -> pd.DataFrame:
    if ev.empty:
        return ev.reset_index(drop=True)
    if min_step > 1:
        ev = ev[ev["step"] >= min_step]
    if direction == "up":
        ev = ev[ev["direction"] == "upgrade"]
    elif direction == "down":
        ev = ev[ev["direction"] == "downgrade"]
    return ev.sort_values(["date", "symbol"]).reset_index(drop=True)


def rating_changes(
    symbols: "list[str] | str",
    date: "str | None" = None,
    start: "str | None" = None,
    end: "str | None" = None,
    direction: "str | None" = None,
    min_step: int = 1,
    price_table: "str | None" = None,
) -> pd.DataFrame:
    """
    Cross-sectional scan: which symbols changed their TA rating bucket?

    Compares each day's rating_label (strong_sell..strong_buy, computed
    locally by analytics.technical from stored OHLCV) with the previous
    trading day's and reports the transitions.

    Modes
    -----
      date="2026-06-20"   changes landing exactly on that trading day
      start / end         every transition in the range — output has
                          symbol + date columns, so it feeds straight into
                          event_study() / scenario()
      (neither)           each symbol's latest bar vs the one before it

    direction : "up" (upgrades only) / "down" (downgrades only) / None
    min_step  : minimum bucket jump (1 = adjacent, 2 = e.g. neutral -> strong_buy)

    Columns: symbol, date, from_label, to_label, from_score, to_score,
             step (bucket distance, positive int), direction.
    """
    from analytics.technical import rating_history
    if isinstance(symbols, str):
        symbols = [symbols]

    target = pd.to_datetime(date) if date else None
    # the 200-day SMA needs ~200 trading days of warm-up before the first
    # valid rating — load ~320 calendar days ahead of the earliest scan date
    warmup = pd.Timedelta(days=320)
    if target is not None:
        load_start = (target - warmup).strftime("%Y-%m-%d")
        load_end = target.strftime("%Y-%m-%d")
    elif start is not None:
        load_start = (pd.to_datetime(start) - warmup).strftime("%Y-%m-%d")
        load_end = end
    elif end is not None:
        load_start, load_end = None, end
    else:
        load_start = (pd.Timestamp.today().normalize() - warmup).strftime("%Y-%m-%d")
        load_end = None
    latest_mode = target is None and start is None and end is None

    frames = []
    for sym in dict.fromkeys(symbols):
        d = rating_history(sym, price_table=price_table,
                           start=load_start, end=load_end)
        if d.empty or "rating_label" not in d.columns:
            continue
        lab = d["rating_label"].astype(object)
        ordv = lab.map(_RATING_ORD)
        sub = pd.DataFrame({
            "symbol": sym,
            "date": d.index,
            "from_label": lab.shift(1),
            "to_label": lab,
            "from_score": d["rating_all"].shift(1).round(3),
            "to_score": d["rating_all"].round(3),
            "step": ordv - ordv.shift(1),
        })
        sub = sub[sub["step"].notna() & (sub["step"] != 0)]
        if target is not None:
            sub = sub[sub["date"] == target]
        elif latest_mode:
            sub = sub[sub["date"] == d.index.max()]
        else:
            if start is not None:
                sub = sub[sub["date"] >= pd.to_datetime(start)]
            if end is not None:
                sub = sub[sub["date"] <= pd.to_datetime(end)]
        if not sub.empty:
            frames.append(sub)

    if not frames:
        return pd.DataFrame(columns=_CHANGE_COLS)
    ev = pd.concat(frames, ignore_index=True)
    ev["direction"] = np.where(ev["step"] > 0, "upgrade", "downgrade")
    ev["step"] = ev["step"].abs().astype(int)
    return _apply_change_filters(ev[_CHANGE_COLS], direction, min_step)


def tv_snapshot_changes(
    date: "str | None" = None,
    prev_date: "str | None" = None,
    direction: "str | None" = None,
    min_step: int = 1,
) -> pd.DataFrame:
    """
    Rating changes from TradingView's own published ratings (the tv_ratings
    daily snapshots) — diffs the two most recent snapshot dates by default,
    or date vs prev_date when given. Wider universe than the local price
    store (top-500 stocks + ETFs) but needs >= 2 accumulated snapshots.

    Columns: as rating_changes() plus source="tv_snapshot".
    """
    df = q.load("tv_ratings")
    if df.empty:
        raise RuntimeError("tv_ratings is empty — run tradingview_pipeline.py first.")
    df["date"] = pd.to_datetime(df["date"])
    dates = sorted(pd.unique(df["date"]))
    if (date is None or prev_date is None) and len(dates) < 2:
        have = ", ".join(str(pd.Timestamp(d).date()) for d in dates)
        raise RuntimeError(
            f"Need >= 2 tv_ratings snapshot dates to diff; have {len(dates)} "
            f"({have}). Run tradingview_pipeline.py daily to accumulate history.")
    d1 = pd.to_datetime(date) if date else dates[-1]
    d0 = (pd.to_datetime(prev_date) if prev_date
          else max([d for d in dates if d < d1], default=None))
    if d0 is None:
        raise RuntimeError(f"No tv_ratings snapshot earlier than {pd.Timestamp(d1).date()}.")

    keep = ["symbol", "rating_label", "rating_all"]
    cur = (df[df["date"] == d1][keep].dropna(subset=["symbol"])
           .drop_duplicates("symbol").set_index("symbol"))
    prv = (df[df["date"] == d0][keep].dropna(subset=["symbol"])
           .drop_duplicates("symbol").set_index("symbol"))
    both = cur.join(prv, lsuffix="_to", rsuffix="_from", how="inner")

    # 'unknown' labels map to NaN and drop out with the step filter
    step = (both["rating_label_to"].map(_RATING_ORD)
            - both["rating_label_from"].map(_RATING_ORD))
    changed = step.notna() & (step != 0)
    ev = pd.DataFrame({
        "symbol": both.index[changed],
        "date": d1,
        "from_label": both.loc[changed, "rating_label_from"].values,
        "to_label": both.loc[changed, "rating_label_to"].values,
        "from_score": both.loc[changed, "rating_all_from"].round(3).values,
        "to_score": both.loc[changed, "rating_all_to"].round(3).values,
        "step": step[changed].values,
    })
    if ev.empty:
        return pd.DataFrame(columns=_CHANGE_COLS + ["source"])
    ev["direction"] = np.where(ev["step"] > 0, "upgrade", "downgrade")
    ev["step"] = ev["step"].abs().astype(int)
    ev["source"] = "tv_snapshot"
    return _apply_change_filters(ev[_CHANGE_COLS + ["source"]],
                                 direction, min_step)
