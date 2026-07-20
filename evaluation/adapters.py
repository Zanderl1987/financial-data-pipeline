"""
evaluation/adapters.py -- turn repo-native data sources into evaluation
contracts. Each adapter is tens of lines; adding a new signal to the
framework should never require more than a function like these.

All adapters return lag_days=0 / direction=1: the legacy harnesses enter at
the next close after the signal date with no extra lag, and the engine's
strictly-after entry reproduces that -- keeping registry baselines
comparable with the historical sentiment/TV numbers.
"""

import pandas as pd

from evaluation.contracts import EventSet, Signal, TradeRule


def from_signal_panel(factor: str = "composite", symbols=None,
                      start=None, end=None) -> Signal:
    """One factor column of analytics.signals.signal_panel as a Signal."""
    from analytics import signals as _signals   # local import: monkeypatchable
    panel = _signals.signal_panel(symbols=symbols, start=start, end=end)
    if panel.empty:
        raise ValueError("signal_panel returned no rows -- run pipelines / "
                         "curated.py first")
    if factor not in panel.columns:
        have = [c for c in panel.columns if c not in ("symbol", "date")]
        raise ValueError(f"unknown factor {factor!r}; available: {have}")
    frame = panel[["symbol", "date"]].copy()
    frame["value"] = panel[factor].to_numpy()   # works for factor="value" too
    frame = frame.dropna(subset=["value"])
    return Signal(name=f"factor_{factor}", frame=frame, lag_days=0,
                  direction=1, source="analytics.signals.signal_panel")


def from_sentiment(start=None, end=None, min_articles: int = 1) -> Signal:
    """Confidence-weighted daily news sentiment (sentiment_eval's exact
    aggregation, imported -- not reimplemented)."""
    import sentiment_eval as _se
    daily = _se.daily_signals(min_articles=min_articles, start=start, end=end)
    if daily.empty:
        raise ValueError("no news_sentiment rows -- run "
                         "news_sentiment_pipeline.py first")
    frame = daily.rename(columns={"sent_score": "value"})[
        ["symbol", "date", "value"]]
    return Signal(name="news_sentiment", frame=frame, lag_days=0,
                  direction=1, source="sentiment_eval.daily_signals")


def rating_cache(symbols=None, price_table=None, start=None,
                 end=None) -> dict:
    """tv_rating_eval's per-symbol rating_history cache (for TradeRules)."""
    import tv_rating_eval as _tv
    kw = {"start": start, "end": end}
    if price_table is not None:
        kw["price_table"] = price_table
    return _tv.build_signal_cache(symbols or _tv.universe(), **kw)


def from_rating_history(signal_col: str = "rating_all", cache=None,
                        symbols=None, price_table=None, start=None,
                        end=None) -> Signal:
    """A TV rating column across the universe as a continuous Signal."""
    if cache is None:
        cache = rating_cache(symbols=symbols, price_table=price_table,
                             start=start, end=end)
    frames = []
    for sym, d in cache.items():
        if signal_col not in d.columns:
            continue
        f = pd.DataFrame({"symbol": sym, "date": d.index,
                          "value": d[signal_col].to_numpy()})
        frames.append(f)
    if not frames:
        raise ValueError(f"no {signal_col!r} data in the rating cache")
    frame = pd.concat(frames, ignore_index=True).dropna(subset=["value"])
    return Signal(name=f"tv_{signal_col}", frame=frame, lag_days=0,
                  direction=1, source="tv_rating_eval.build_signal_cache")


def from_rating_changes(symbols=None, start: str = "2000-01-01", end=None,
                        min_step: int = 1, price_table=None,
                        min_events: int = 5) -> EventSet:
    """TA rating-bucket transitions as an EventSet (label = up / down)."""
    import event_backtest as _eb
    import tv_rating_eval as _tv
    changes = _eb.rating_changes(symbols or _tv.universe(),
                                 start=start, end=end, min_step=min_step,
                                 price_table=price_table)
    if changes.empty:
        raise ValueError("rating_changes returned no transitions")
    frame = changes.rename(columns={"direction": "label"})[
        ["symbol", "date", "label"]]
    return EventSet(name="tv_rating_changes", frame=frame, lag_days=0,
                    min_events=min_events)


def tv_threshold_rule() -> TradeRule:
    """The TV threshold strategy as a TradeRule. Thresholds are imported
    from tv_rating_eval (single source of truth), and the flag semantics
    replicate its simulate_trades: entries on the CROSS through the level,
    exits on the level condition itself; the engine adds next-close
    execution and one-position-at-a-time."""
    import tv_rating_eval as _tv

    def _crossed_up(s, level):
        return (s >= level) & (s.shift(1) < level)

    def _crossed_down(s, level):
        return (s <= level) & (s.shift(1) > level)

    return TradeRule(
        name="tv_threshold",
        entries=lambda d: _crossed_up(d["rating_all"], _tv.BULL_MIN),
        exits=lambda d: d["rating_all"] < _tv.EXIT_LONG_MAX,
        side="both",
        short_entries=lambda d: _crossed_down(d["rating_all"], _tv.BEAR_MAX),
        short_exits=lambda d: d["rating_all"] > _tv.EXIT_SHORT_MIN,
        notional=_tv.NOTIONAL)
