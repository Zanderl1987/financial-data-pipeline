"""
Technical indicators + a local replica of TradingView's Technical Rating.

TradingView's aggregate gauge (Strong Buy … Strong Sell) is the arithmetic
mean of two group ratings, each averaging per-indicator votes of +1 / 0 / -1:

  MA group (15):   SMA & EMA (10,20,30,50,100,200), Hull MA 9, VWMA 20,
                   Ichimoku baseline logic — vote +1 when MA < close.
  Oscillators (11): RSI 14, Stoch 14-3-3, CCI 20, ADX 14, Awesome Osc,
                   Momentum 10, MACD 12-26-9, Stoch RSI, Williams %R,
                   Bull/Bear Power 13, Ultimate Osc 7-14-28.

TradingView only serves current ratings (see tradingview_pipeline.py), so
this module recomputes the same formula from any OHLCV history, making the
rating fully backtestable. Verified against the live scanner: Recommend.All
== mean(Recommend.MA, Recommend.Other), and per-indicator votes match the
published logic (tradingview-ta / TV's open-source TechnicalRating library).

Usage
-----
    from analytics.technical import indicators, tv_rating, rating_history

    hist = rating_history("AAPL")            # date-indexed ratings from stored prices
    df   = tv_rating(ohlcv_df)               # ratings from your own OHLCV frame
    ind  = indicators(ohlcv_df)              # just the indicator columns
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REQUIRED = ("open", "high", "low", "close")


# ---------------------------------------------------------------- indicators

def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (TradingView ta.rma)."""
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _hull(s: pd.Series, n: int = 9) -> pd.Series:
    def wma(x: pd.Series, m: int) -> pd.Series:
        w = np.arange(1, m + 1, dtype=float)
        return x.rolling(m).apply(lambda a: np.dot(a, w) / w.sum(), raw=True)
    return wma(2 * wma(s, n // 2) - wma(s, n), int(np.sqrt(n)))


def _vwma(close: pd.Series, volume: pd.Series, n: int = 20) -> pd.Series:
    return (close * volume).rolling(n).sum() / volume.rolling(n).sum()


def _donchian(high: pd.Series, low: pd.Series, n: int) -> pd.Series:
    return (high.rolling(n).max() + low.rolling(n).min()) / 2.0


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = _rma(delta.clip(lower=0), n)
    dn = _rma((-delta).clip(lower=0), n)
    rs = up / dn
    return 100 - 100 / (1 + rs)


def _stoch(high, low, close, n: int = 14, k_smooth: int = 3, d_smooth: int = 3):
    ll = low.rolling(n).min()
    hh = high.rolling(n).max()
    raw = 100 * (close - ll) / (hh - ll)
    k = _sma(raw, k_smooth)
    d = _sma(k, d_smooth)
    return k, d


def _cci(high, low, close, n: int = 20) -> pd.Series:
    tp = (high + low + close) / 3.0
    ma = _sma(tp, n)
    md = tp.rolling(n).apply(lambda a: np.abs(a - a.mean()).mean(), raw=True)
    return (tp - ma) / (0.015 * md)


def _adx(high, low, close, n: int = 14):
    up = high.diff()
    dn = -low.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=high.index)
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = _rma(tr, n)
    plus_di = 100 * _rma(plus_dm, n) / atr
    minus_di = 100 * _rma(minus_dm, n) / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = _rma(dx, n)
    return adx, plus_di, minus_di


def _uo(high, low, close, n1=7, n2=14, n3=28) -> pd.Series:
    prev_close = close.shift()
    bp = close - pd.concat([low, prev_close], axis=1).min(axis=1)
    tr = pd.concat([high, prev_close], axis=1).max(axis=1) - \
        pd.concat([low, prev_close], axis=1).min(axis=1)
    avg = lambda n: bp.rolling(n).sum() / tr.rolling(n).sum()
    return 100 * (4 * avg(n1) + 2 * avg(n2) + avg(n3)) / 7.0


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Append the full indicator set to a single-symbol OHLCV frame.

    df needs columns open/high/low/close (volume optional, enables VWMA),
    one row per trading day, sorted ascending.
    """
    missing = [c for c in _REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV frame missing columns: {missing}")

    out = df.copy()
    h, l, c = out["high"], out["low"], out["close"]

    for n in (10, 20, 30, 50, 100, 200):
        out[f"sma{n}"] = _sma(c, n)
        out[f"ema{n}"] = _ema(c, n)
    out["hull9"] = _hull(c, 9)
    if "volume" in out.columns and out["volume"].notna().any():
        out["vwma20"] = _vwma(c, out["volume"], 20)
    else:
        out["vwma20"] = np.nan

    out["ich_conv"] = _donchian(h, l, 9)
    out["ich_base"] = _donchian(h, l, 26)
    out["ich_lead1"] = (out["ich_conv"] + out["ich_base"]) / 2.0
    out["ich_lead2"] = _donchian(h, l, 52)

    out["rsi14"] = _rsi(c, 14)
    out["stoch_k"], out["stoch_d"] = _stoch(h, l, c)
    out["cci20"] = _cci(h, l, c, 20)
    out["adx14"], out["plus_di"], out["minus_di"] = _adx(h, l, c, 14)
    out["ao"] = _sma((h + l) / 2, 5) - _sma((h + l) / 2, 34)
    out["mom10"] = c - c.shift(10)

    macd_line = _ema(c, 12) - _ema(c, 26)
    out["macd"] = macd_line
    out["macd_signal"] = _ema(macd_line, 9)

    rsi = out["rsi14"]
    srsi_ll = rsi.rolling(14).min()
    srsi_hh = rsi.rolling(14).max()
    srsi_raw = 100 * (rsi - srsi_ll) / (srsi_hh - srsi_ll)
    out["srsi_k"] = _sma(srsi_raw, 3)
    out["srsi_d"] = _sma(out["srsi_k"], 3)

    out["willr14"] = -100 * (h.rolling(14).max() - c) / \
        (h.rolling(14).max() - l.rolling(14).min())

    ema13 = _ema(c, 13)
    out["bull_power"] = h - ema13
    out["bear_power"] = l - ema13

    out["uo"] = _uo(h, l, c)
    out["atr14"] = _rma(pd.concat([h - l, (h - c.shift()).abs(),
                                   (l - c.shift()).abs()], axis=1).max(axis=1), 14)
    return out


# ------------------------------------------------------------------- ratings

def _vote(buy: pd.Series, sell: pd.Series, valid: pd.Series) -> pd.Series:
    """+1 / -1 / 0 vote series, NaN where the indicator isn't defined yet."""
    v = pd.Series(0.0, index=buy.index)
    v[buy.fillna(False)] = 1.0
    v[sell.fillna(False)] = -1.0
    return v.where(valid)


def tv_rating(df: pd.DataFrame) -> pd.DataFrame:
    """
    TradingView-style technical rating history for one symbol.

    Input: single-symbol OHLCV frame (open/high/low/close [+ volume]),
    ascending. Returns the input index with columns:

      rating_ma, rating_osc, rating_all  — group and overall scores in [-1, 1]
      rating_label                       — strong_buy/buy/neutral/sell/strong_sell
      sig_*                              — the 26 individual votes (+1/0/-1)
    """
    d = indicators(df)
    c, o = d["close"], d["open"]
    prev = lambda s: s.shift(1)

    votes: dict[str, pd.Series] = {}

    # --- MA group: vote +1 when MA below price, -1 above ---
    ma_cols = [f"sma{n}" for n in (10, 20, 30, 50, 100, 200)] + \
              [f"ema{n}" for n in (10, 20, 30, 50, 100, 200)] + \
              ["hull9", "vwma20"]
    for col in ma_cols:
        ma = d[col]
        votes[f"sig_{col}"] = _vote(ma < c, ma > c, ma.notna())

    # Ichimoku (TV rating logic: cloud direction + price/conversion/base position)
    ic1, ic2 = d["ich_conv"], d["ich_base"]
    ic3, ic4 = d["ich_lead1"], d["ich_lead2"]
    votes["sig_ichimoku"] = _vote(
        (ic3 > ic4) & (c > ic3) & (c < ic2) & (o > ic1) & (c > ic1),
        (ic3 < ic4) & (c < ic3) & (c > ic2) & (o < ic1) & (c < ic1),
        ic4.notna())

    # --- Oscillator group ---
    up_trend = c > d["ema50"]
    down_trend = c < d["ema50"]

    rsi = d["rsi14"]
    votes["sig_rsi"] = _vote((rsi < 30) & (prev(rsi) < rsi),
                             (rsi > 70) & (prev(rsi) > rsi), rsi.notna())

    k, dd = d["stoch_k"], d["stoch_d"]
    votes["sig_stoch"] = _vote(
        (k < 20) & (dd < 20) & (k > dd) & (prev(k) < prev(dd)),
        (k > 80) & (dd > 80) & (k < dd) & (prev(k) > prev(dd)), dd.notna())

    cci = d["cci20"]
    votes["sig_cci"] = _vote((cci < -100) & (cci > prev(cci)),
                             (cci > 100) & (cci < prev(cci)), cci.notna())

    adx, pdi, ndi = d["adx14"], d["plus_di"], d["minus_di"]
    votes["sig_adx"] = _vote(
        (adx > 20) & (prev(pdi) < prev(ndi)) & (pdi > ndi),
        (adx > 20) & (prev(pdi) > prev(ndi)) & (pdi < ndi), adx.notna())

    ao = d["ao"]
    votes["sig_ao"] = _vote(
        ((ao > 0) & (prev(ao) < 0)) |
        ((ao > 0) & (prev(ao) > 0) & (ao > prev(ao)) & (ao.shift(2) > prev(ao))),
        ((ao < 0) & (prev(ao) > 0)) |
        ((ao < 0) & (prev(ao) < 0) & (ao < prev(ao)) & (ao.shift(2) < prev(ao))),
        ao.notna())

    mom = d["mom10"]
    votes["sig_mom"] = _vote(mom > prev(mom), mom < prev(mom), prev(mom).notna())

    votes["sig_macd"] = _vote(d["macd"] > d["macd_signal"],
                              d["macd"] < d["macd_signal"], d["macd_signal"].notna())

    sk, sd = d["srsi_k"], d["srsi_d"]
    votes["sig_srsi"] = _vote(
        down_trend & (sk < 20) & (sd < 20) & (sk > sd) & (prev(sk) < prev(sd)),
        up_trend & (sk > 80) & (sd > 80) & (sk < sd) & (prev(sk) > prev(sd)),
        sd.notna())

    wr = d["willr14"]
    votes["sig_wr"] = _vote((wr < -80) & (wr > prev(wr)),
                            (wr > -20) & (wr < prev(wr)), wr.notna())

    bull, bear = d["bull_power"], d["bear_power"]
    votes["sig_bbp"] = _vote(
        up_trend & (bear < 0) & (bear > prev(bear)),
        down_trend & (bull > 0) & (bull < prev(bull)), d["ema50"].notna())

    uo = d["uo"]
    votes["sig_uo"] = _vote(uo > 70, uo < 30, uo.notna())

    sig = pd.DataFrame(votes, index=d.index)
    ma_sigs = [f"sig_{c_}" for c_ in ma_cols] + ["sig_ichimoku"]
    osc_sigs = [s for s in sig.columns if s not in ma_sigs]

    out = d.copy()
    out["rating_ma"] = sig[ma_sigs].mean(axis=1, skipna=True)
    out["rating_osc"] = sig[osc_sigs].mean(axis=1, skipna=True)
    out["rating_all"] = (out["rating_ma"] + out["rating_osc"]) / 2.0
    out["rating_label"] = pd.cut(
        out["rating_all"],
        bins=[-1.01, -0.5, -0.1, 0.1, 0.5, 1.01],
        labels=["strong_sell", "sell", "neutral", "buy", "strong_buy"])
    return pd.concat([out, sig], axis=1)


# ----------------------------------------------------------- stored-data API

def _split_only_adjust(df: pd.DataFrame) -> pd.DataFrame:
    """Split-adjust open/high/low/close/volume from a raw close + a
    per-row split ratio (Tiingo's split_factor convention: recorded on the
    split's effective date, e.g. 4.0 for a 4:1 split); requires `df`
    already sorted ascending by date.

    Deliberately does NOT use a source's dividend-adjusted column
    (adj_close/adj_open/...). Dividend adjustment compounds backward
    over a stock's whole history and can deflate decades-old prices to a
    fraction of what actually traded that day (DUK: 1990 adj_close is
    18.8% of that day's real close) -- harmless for a total-return chart,
    but it manufactures a fake long-run "uptrend" when fed into a
    technical indicator or a discrete trade-rule backtest that doesn't
    model dividend reinvestment (this pipeline's evaluation/trades.py
    engine doesn't). Discovered 2026-08-08 when a Russell 3000 TV-rating
    backtest flipped from a decisive null (pnl_p=0.87, raw Schwab prices)
    to a suspiciously strong "edge" (pnl_p=0.005) purely from switching to
    a dividend-adjusted price source -- see
    experiments/2026-08-08_tv-technical-rating-signal-eval.md.
    """
    cols = {c.lower(): c for c in df.columns}
    if "split_factor" not in cols:
        return df
    sf = df[cols["split_factor"]].fillna(1.0).astype(float)
    if (sf == 1.0).all():
        return df
    future_factor = sf[::-1].cumprod()[::-1] / sf   # product of every split AFTER this row
    out = df.copy()
    for key in ("open", "high", "low", "close"):
        if key in cols:
            out[cols[key]] = out[cols[key]].astype(float) / future_factor
    if "volume" in cols:
        out[cols["volume"]] = out[cols["volume"]].astype(float) * future_factor
    return out


def _load_ohlcv(symbol: str, price_table: "str | None",
                start: "str | None", end: "str | None") -> pd.DataFrame:
    import query as q
    tables = [price_table] if price_table else \
        ["tiingo_prices", "yfinance_universe_prices", "prices", "market_history"]
    for t in tables:
        try:
            df = q.load(t, symbol=symbol, start=start, end=end)
        except Exception:
            continue
        if df.empty:
            continue
        if not all(k in (c.lower() for c in df.columns) for k in _REQUIRED):
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = (df.drop_duplicates("date").sort_values("date")
                .set_index("date"))
        # split-only adjustment when the source carries a split ratio
        # (tiingo_prices); other sources are used as-is -- yfinance_universe_
        # prices' plain close/open/high/low are already split-adjusted at
        # the source (Yahoo's convention), and neither `prices` (Schwab) nor
        # `market_history` carry a split ratio to adjust with. adj_close/
        # adj_open/... are intentionally never preferred here -- see
        # _split_only_adjust's docstring.
        df = _split_only_adjust(df)
        df.attrs["price_table"] = t
        return df
    return pd.DataFrame()


def rating_history(symbol: str,
                   price_table: "str | None" = None,
                   start: "str | None" = None,
                   end: "str | None" = None) -> pd.DataFrame:
    """
    TradingView-style rating history for a symbol from stored prices
    (tiingo_prices / prices / market_history, first with data wins).
    Date-indexed; see tv_rating() for columns.
    """
    df = _load_ohlcv(symbol, price_table, start, end)
    if df.empty:
        return df
    out = tv_rating(df)
    out["symbol"] = symbol
    return out


def rating_panel(symbols: "list[str]",
                 price_table: "str | None" = None,
                 start: "str | None" = None,
                 end: "str | None" = None,
                 columns: "list[str] | None" = None) -> pd.DataFrame:
    """
    Tidy (symbol, date) panel of ratings for many symbols — the input shape
    the event backtester expects. `columns` trims the output (default: the
    three rating scores + label).
    """
    keep = columns or ["rating_ma", "rating_osc", "rating_all", "rating_label"]
    frames = []
    for sym in symbols:
        df = rating_history(sym, price_table, start, end)
        if df.empty:
            continue
        frames.append(df.reset_index()[["symbol", "date"] + keep])
    return (pd.concat(frames, ignore_index=True)
            if frames else pd.DataFrame())
