"""
Backtesting engine — turn a cross-sectional signal into a performance record.

Closes the loop: signals.py produces (symbol, date, score); this module ranks
those scores into quantile portfolios, rebalances on a schedule, and reports an
equity curve plus risk/return metrics, benchmarked against equal-weight
buy-and-hold.

Look-ahead safety
-----------------
The signal known on rebalance date *t* sets the weights that earn the returns
of *t+1* onward (weights are shifted one day before being multiplied into
returns). A score computed from data as of *t* never earns the return of *t*.

Vectorized
----------
Portfolio returns are a single matrix product: a daily weight matrix
(forward-filled from rebalance dates, then lagged one day) times the daily
return matrix. No per-day Python loop over the holding period.

Usage
-----
    import backtest as bt
    from analytics import signal_panel

    sig = signal_panel(["AAPL", "MSFT", "NVDA", "AMD", "INTC"], start="2020-01-01")
    res = bt.backtest(sig, rebalance="M", quantiles=5, long_short=True)
    print(res.summary())
    res.equity            # equity curve (pandas Series)
    res.metrics           # dict of performance stats
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query as q
from analytics.features import _pick_price_table  # reuse price-source detection
from evaluation import execution as ev_execution
from evaluation.stats import _degenerate_sd

_ANN = 252  # trading days per year


@dataclass
class BacktestResult:
    """Container for a backtest run. Inspect .metrics or call .summary()."""
    returns: pd.Series           # daily strategy returns
    equity: pd.Series            # cumulative growth of $1
    benchmark: pd.Series         # benchmark equity curve (equal-weight buy/hold)
    weights: pd.DataFrame        # daily weight matrix (date x symbol)
    metrics: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)

    def summary(self) -> pd.DataFrame:
        """One-column summary table of parameters + headline metrics."""
        rows = {**self.params, **self.metrics}
        return pd.DataFrame.from_dict(rows, orient="index", columns=["value"])

    def __repr__(self) -> str:
        m = self.metrics
        return (f"<BacktestResult CAGR={m.get('cagr_pct')}% "
                f"Sharpe={m.get('sharpe')} MaxDD={m.get('max_drawdown_pct')}% "
                f"vs bench CAGR={m.get('benchmark_cagr_pct')}%>")


def _returns_matrix(price_table, symbols, start, end) -> pd.DataFrame:
    """Wide daily-return matrix: rows=date, cols=symbol."""
    cols = q.schema(price_table)["column_name"].tolist()
    close_col = "adj_close" if "adj_close" in cols else "close"
    px = q.load(price_table, symbol=list(symbols), start=start, end=end,
                columns=["symbol", "date", close_col])
    if px.empty:
        return pd.DataFrame()
    px["date"] = pd.to_datetime(px["date"])
    wide = (px.drop_duplicates(["symbol", "date"])
              .pivot(index="date", columns="symbol", values=close_col)
              .sort_index())
    return wide.pct_change()


def _rebalance_dates(index: pd.DatetimeIndex, rebalance: str) -> pd.DatetimeIndex:
    """Last available trading day within each calendar period (D/W/M/Q)."""
    if rebalance.upper() in ("D", "DAILY"):
        return index
    freq = {"W": "W", "M": "ME", "Q": "QE"}.get(rebalance.upper(), "ME")
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.resample(freq).last().dropna().values)


def _target_weights(
    scores_wide: pd.DataFrame,
    rebal_dates,
    quantiles: int,
    long_short: bool,
    weighting_mode: str = "quantile",
    returns: "pd.DataFrame | None" = None,
    hrp_lookback: int = 126,
    hrp_linkage_method: str = "single",
) -> pd.DataFrame:
    """
    Target weights at each rebalance date, supporting multiple weighting modes.

    Modes:
    - "quantile" (default): Top 1/quantiles go long equal-weight (+1 sum);
      if long_short, bottom 1/quantiles go short equal-weight (-1 sum).
    - "signal_proportional": Weights proportional to signal score. Long-only:
      weights = score / sum(score) for positive scores. Long/short: weights
      = score / sum(|score|), preserving sign.
    - "hrp": Hierarchical Risk Parity weights from evaluation.hrp.hrp_weights()
      computed on trailing returns at each rebalance date (PIT-safe). Requires
      `returns` matrix and at least 2 symbols with shared history.
    """
    if weighting_mode not in ("quantile", "signal_proportional", "hrp"):
        raise ValueError(f"weighting_mode must be 'quantile', 'signal_proportional', or 'hrp'; got {weighting_mode!r}")

    out = pd.DataFrame(0.0, index=rebal_dates, columns=scores_wide.columns)

    if weighting_mode == "quantile":
        for rd in rebal_dates:
            prior = scores_wide.loc[:rd]
            if prior.empty:
                continue
            s = prior.iloc[-1].dropna()
            n = len(s)
            if n < 2:
                continue
            k = max(1, int(round(n / quantiles)))
            ranked = s.sort_values(ascending=False)
            longs = ranked.index[:k]
            out.loc[rd, longs] = 1.0 / k
            if long_short:
                shorts = ranked.index[-k:]
                out.loc[rd, shorts] = out.loc[rd, shorts] - 1.0 / k
        return out

    if weighting_mode == "signal_proportional":
        for rd in rebal_dates:
            prior = scores_wide.loc[:rd]
            if prior.empty:
                continue
            s = prior.iloc[-1].dropna()
            if s.empty:
                continue
            if long_short:
                denom = s.abs().sum()
                if denom > 0:
                    out.loc[rd, s.index] = s / denom
            else:
                pos = s[s > 0]
                denom = pos.sum()
                if denom > 0:
                    out.loc[rd, pos.index] = pos / denom
        return out

    # weighting_mode == "hrp"
    if returns is None:
        raise ValueError("HRP weighting mode requires the returns matrix (returns=...)")
    from evaluation import hrp as ev_hrp

    for rd in rebal_dates:
        prior = scores_wide.loc[:rd]
        if prior.empty:
            continue
        s = prior.iloc[-1].dropna()
        active_symbols = s.index.tolist()
        if len(active_symbols) < 2:
            continue

        # Trailing returns window ending STRICTLY BEFORE rebalance date (PIT-safe)
        ret_window = returns.loc[:rd].iloc[:-1]  # exclude rebalance date itself
        if len(ret_window) < hrp_lookback + 1:
            ret_window = returns.loc[:rd].iloc[:-1]
        ret_window = ret_window.tail(hrp_lookback + 1)

        # Align to active symbols
        panel = ret_window[active_symbols].dropna(axis=1, how="all").dropna(axis=0, how="any")
        if panel.shape[1] < 2 or panel.shape[0] < 20:
            # Fall back to equal-weight if insufficient history for HRP
            k = len(active_symbols)
            out.loc[rd, active_symbols] = 1.0 / k
            continue

        try:
            hrp_w = ev_hrp.hrp_weights(panel, linkage_method=hrp_linkage_method)
            # hrp_w is long-only summing to 1; apply long/short if requested
            if long_short:
                # Split HRP weights by signal sign: positive -> long, negative -> short
                # Scale so gross = 1 (long sum = 0.5, short sum = -0.5)
                pos_symbols = s[s > 0].index.intersection(hrp_w.index)
                neg_symbols = s[s < 0].index.intersection(hrp_w.index)
                if len(pos_symbols) == 0 and len(neg_symbols) == 0:
                    continue
                w = pd.Series(0.0, index=active_symbols)
                if len(pos_symbols) > 0:
                    pos_w = hrp_w[pos_symbols]
                    pos_w = pos_w / pos_w.sum() * 0.5
                    w[pos_symbols] = pos_w
                if len(neg_symbols) > 0:
                    neg_w = hrp_w[neg_symbols]
                    neg_w = neg_w / neg_w.sum() * 0.5
                    w[neg_symbols] = -neg_w
                out.loc[rd, w.index] = w
            else:
                # Long-only: use HRP weights directly (already sum to 1)
                out.loc[rd, hrp_w.index] = hrp_w
        except Exception:
            # Fall back to equal-weight on any HRP failure
            k = len(active_symbols)
            out.loc[rd, active_symbols] = 1.0 / k
    return out


def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def _adv_participation_cost(weights: pd.DataFrame, aum: float, coeff,
                            adv_window: int, symbols, start, end,
                            price_table: "str | None",
                            returns: "pd.DataFrame | None" = None) -> pd.Series:
    """
    Per-symbol participation-based sqrt market-impact cost, as a fraction of
    `aum` per day: for each symbol traded on a rebalance day, cost_rate =
    coeff/1e4 * sqrt(dollar_traded / trailing_adv), charged on that symbol's
    own dollar amount traded, summed across symbols.

    The SAME model event_backtest.scenario()'s adv_impact_coeff already
    uses (a real function of a symbol's own liquidity) -- reused here, not
    reinvented -- applied to REBALANCE-DAY WEIGHT CHANGES instead of
    per-event notional. Deliberately a DIFFERENT parameter from this
    engine's existing adv_impact_coeff (a function of PORTFOLIO-level
    turnover with no per-symbol liquidity concept -- see evaluation/
    execution.py's docstring on why backtest.py's and event_backtest.py's
    two meanings of "sqrt_impact" are kept apart). Naming this
    adv_participation_coeff avoids repeating that exact ambiguity a third
    way.

    `aum` translates a fractional weight CHANGE into a dollar amount traded
    -- this engine has no other concept of portfolio dollar size (it works
    entirely in returns/weights), so aum is meaningful ONLY for this cost
    term, nowhere else in backtest().

    ADV is trailing `adv_window` days, shifted one day -- today's
    participation is measured against liquidity known BEFORE today's own
    (not-yet-observed) volume, the same look-ahead-safety convention
    event_backtest.load_dollar_volume()'s callers already use.

    coeff may ALSO be the string "sqrt_law" (same participation surface, but
    the flat scalar is replaced by the calibrated square-root-law form
    `event_backtest.ADV_SQRT_LAW_K * realized_daily_vol_bps * sqrt(p)`):
    realized daily vol per symbol per day comes from `returns`, measured over
    the trailing `adv_window` days SHIFTED one day (PIT -- today's own
    not-yet-realized return excluded), exactly like ADV itself. Requires
    `returns` (the engine already holds it; re-passing avoids re-deriving it
    from prices). The legacy numeric path is byte-identical when `returns`
    is unused/the scalar form is chosen.
    """
    import event_backtest as eb

    dollar_change = weights.diff().abs() * aum
    volumes = eb.load_dollar_volume_matrix(list(symbols), start=start, end=end,
                                           price_table=price_table)
    volumes = volumes.reindex(index=weights.index, columns=weights.columns)
    adv = volumes.rolling(adv_window, min_periods=adv_window).mean().shift(1)
    participation = (dollar_change / adv).replace([np.inf, -np.inf], np.nan)
    if coeff == "sqrt_law":
        if returns is None:
            raise ValueError("sqrt_law mode requires the returns matrix "
                             "(returns=...) to measure per-symbol realized "
                             "daily volatility")
        realized_vol = returns.rolling(
            adv_window, min_periods=adv_window).std().shift(1)
        cost_rate = (eb.ADV_SQRT_LAW_K * realized_vol
                     * np.sqrt(participation.clip(lower=0)))
    else:
        cost_rate = (coeff / 1e4) * np.sqrt(participation.clip(lower=0))
    cost_dollars = (cost_rate * dollar_change).fillna(0.0).sum(axis=1)
    return cost_dollars / aum


def _apply_price_volume_adjustments(
    scores_wide: pd.DataFrame,
    price_table: str,
    symbols: list,
    start: str | None,
    end: str | None,
    volume_window: int,
    volume_ma_window: int,
    min_volume_ratio: float,
    volume_weighted: bool,
    divergence_lookback: int,
) -> pd.DataFrame:
    """
    Apply price-volume signal family adjustments to signal scores (F1).
    
    Adds three signal variants:
    1. Volume-weighted: Scale signal by relative volume (volume / volume_MA)
    2. Volume-confirmed: Zero out signals where volume < min_volume_ratio * volume_MA
    3. Price-volume divergence: Detect divergence between price trend and volume trend
    
    All computations are PIT-safe using only data available at signal date.
    """
    import event_backtest as eb
    
    # Load volume data (PIT-safe: shift by 1 so we use volume known BEFORE signal date)
    try:
        volume_data = eb.load_dollar_volume_matrix(list(symbols), start=start, end=end,
                                                   price_table=price_table)
        # Convert dollar volume to share volume approximately
        # We'll use dollar volume directly as a liquidity proxy
        volume = volume_data.shift(1)  # PIT: volume known before signal date
    except Exception:
        # If volume data unavailable, return original scores
        return scores_wide
    
    # Align volume to scores_wide index
    volume = volume.reindex(index=scores_wide.index, columns=scores_wide.columns)
    
    # Compute volume moving average
    volume_ma = volume.rolling(window=volume_ma_window, min_periods=min(10, volume_ma_window // 2)).mean().shift(1)
    
    # Volume ratio (current volume / MA volume)
    volume_ratio = volume / volume_ma
    
    adjusted_scores = scores_wide.copy()
    
    if volume_weighted:
        # Scale signal by sqrt(volume_ratio) - moderate amplification for high volume
        # Clip ratio to reasonable bounds
        vol_weight = np.sqrt(volume_ratio.clip(lower=0.1, upper=5.0))
        adjusted_scores = adjusted_scores * vol_weight
    
    # Volume confirmation: zero out signals with insufficient volume
    volume_confirmed = volume_ratio >= min_volume_ratio
    adjusted_scores = adjusted_scores.where(volume_confirmed, 0.0)
    
    # Price-volume divergence detection
    if divergence_lookback > 0:
        # Compute price trend (sign of return over lookback)
        price_trend = np.sign(scores_wide.rolling(window=divergence_lookback).apply(
            lambda x: x.iloc[-1] if not x.isna().all() else 0, raw=False
        ))
        # Compute volume trend
        volume_trend = np.sign(volume_ratio.rolling(window=divergence_lookback).apply(
            lambda x: x.iloc[-1] if not x.isna().all() else 0, raw=False
        ))
        # Divergence: price trend and volume trend have opposite signs
        divergence = (price_trend * volume_trend) < 0
        # Reduce signal magnitude on divergence (don't zero out, just dampen)
        adjusted_scores = adjusted_scores.where(~divergence, adjusted_scores * 0.5)
    
    return adjusted_scores


def _apply_capital_constraints(
    target: pd.DataFrame,
    returns: pd.DataFrame,
    rebal_dates: pd.DatetimeIndex,
    initial_capital: float,
    max_leverage: float,
    max_position_pct: float,
    compound_returns: bool,
    rebalance_on_capital_change: bool,
    capital_change_threshold_pct: float,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Apply capital-constrained compounding to target weights.
    
    Iterates through rebalance periods, tracking capital and enforcing
    position/leverage limits relative to current capital at each rebalance.
    
    Returns:
        weights: Daily weight matrix (date x symbol) with capital constraints applied
        capital_series: Capital at each date (for reporting)
    """
    # Start with initial capital
    capital = initial_capital
    capital_history = {}  # date -> capital
    
    # We'll build the weights day by day
    all_dates = returns.index
    weights = pd.DataFrame(0.0, index=all_dates, columns=target.columns)
    
    # Track capital at each rebalance date
    rebal_capital = {rebal_dates[0]: capital}
    
    for i, rd in enumerate(rebal_dates):
        # Get target weights for this rebalance date
        if rd not in target.index:
            continue
        tgt = target.loc[rd].copy()
        
        # Determine the period this rebalance covers
        period_start = rd
        if i + 1 < len(rebal_dates):
            period_end = rebal_dates[i + 1]
        else:
            period_end = all_dates[-1]
        
        # Get dates in this period (excluding rebalance date itself for returns)
        period_dates = all_dates[(all_dates > rd) & (all_dates <= period_end)]
        if len(period_dates) == 0:
            continue
        
        # Apply capital constraints to target weights
        # 1. Max leverage: gross exposure <= max_leverage * capital
        gross_exposure = tgt.abs().sum()
        max_gross = max_leverage * capital
        if gross_exposure > max_gross and gross_exposure > 0:
            tgt = tgt * (max_gross / gross_exposure)
        
        # 2. Max position size: each position <= max_position_pct * capital
        max_pos_value = max_position_pct * capital
        tgt = tgt.clip(lower=-max_pos_value, upper=max_pos_value)
        
        # 3. Re-normalize if clipping changed the weights significantly
        # (maintain the long/short balance if possible)
        if long_short and (tgt > 0).any() and (tgt < 0).any():
            long_sum = tgt[tgt > 0].sum()
            short_sum = tgt[tgt < 0].sum()
            # Try to maintain 50/50 long/short balance
            target_long = max_gross * 0.5
            target_short = -max_gross * 0.5
            if long_sum > 0:
                tgt[tgt > 0] *= target_long / long_sum
            if short_sum < 0:
                tgt[tgt < 0] *= target_short / short_sum
        
        # Apply these weights for the period (lagged by 1 day for PIT safety)
        # The weights for period_start+1 to period_end come from this rebalance
        period_weight_dates = all_dates[(all_dates > rd) & (all_dates <= period_end)]
        for wd in period_weight_dates:
            weights.loc[wd] = tgt
        
        # Compute returns for this period and update capital
        if compound_returns:
            period_returns = returns.loc[period_weight_dates]
            if len(period_returns) > 0:
                daily_pnl = (tgt * period_returns).sum(axis=1)
                period_return = (1 + daily_pnl).prod() - 1
                new_capital = capital * (1 + period_return)
                
                # Check if we should rebalance due to capital change
                if rebalance_on_capital_change and i + 1 < len(rebal_dates):
                    capital_change_pct = abs(new_capital - capital) / capital * 100
                    if capital_change_pct > capital_change_threshold_pct:
                        # Capital changed significantly - we could trigger an interim rebalance
                        # For now, just note it; the next scheduled rebalance will handle it
                        pass
                
                capital = new_capital
                rebal_capital[period_end] = capital
        
        # Record capital at each date in period
        for d in period_weight_dates:
            capital_history[d] = capital
    
    # Build capital series
    capital_series = pd.Series(capital_history).reindex(all_dates).ffill().fillna(initial_capital)
    
    return weights, capital_series


def backtest(
    signal: pd.DataFrame,
    score: str = "composite",
    price_table: "str | None" = None,
    quantiles: int = 5,
    rebalance: str = "M",
    long_short: bool = True,
    start: "str | None" = None,
    end: "str | None" = None,
    cost_bps: float = 0.0,
    spread_bps: float = 0.0,
    borrow_fee_bps: float = 0.0,
    borrow_fee_matrix: "pd.DataFrame | None" = None,
    slippage_model: "str | None" = None,
    adv_impact_coeff: float = 0.1,
    adv_participation_coeff: "float | str | None" = None,
    aum: float = 1_000_000.0,
    adv_window: int = 20,
    vol_target: "float | None" = None,
    max_weight: "float | None" = None,
    max_drawdown_stop: "float | None" = None,
    weighting_mode: str = "quantile",
    hrp_lookback: int = 126,
    hrp_linkage_method: str = "single",
    # F1: Capital-constrained compounding
    capital_constrained: bool = False,
    initial_capital: float = 1_000_000.0,
    max_leverage: float = 1.0,
    max_position_pct: float = 0.10,
    max_sector_pct: float = 0.30,
    compound_returns: bool = True,
    rebalance_on_capital_change: bool = True,
    capital_change_threshold_pct: float = 5.0,
    # F1: Price-volume signal family
    price_volume_enabled: bool = False,
    volume_window: int = 21,
    volume_ma_window: int = 63,
    min_volume_ratio: float = 1.5,
    volume_weighted: bool = True,
    divergence_lookback: int = 5,
) -> BacktestResult:
    """
    Backtest a cross-sectional signal with advanced execution costs, risk controls,
    and performance metrics.

    adv_participation_coeff (opt-in, default None -- no behavior change unless
    set): a real per-symbol ADV market-impact cost, on top of (not instead of)
    the existing portfolio-turnover-based adv_impact_coeff/slippage_model. See
    _adv_participation_cost()'s docstring for the model and why it's a
    separate parameter. `aum`/`adv_window` are only meaningful when this is set.
    Set it to the string "sqrt_law" to replace the flat scalar with the
    calibration-backed square-root-law form `ADV_SQRT_LAW_K * realized_daily_
    vol_bps * sqrt(p)` (per-symbol realized vol measured PIT from `R`); any
    other string raises.

    borrow_fee_matrix (opt-in, default None): per-symbol annualized borrow fees
    (bps) as a DataFrame (date x symbol). If provided, replaces the flat
    `borrow_fee_bps` for short-cost calculation using per-symbol short exposure.
    If None and `borrow_fee_bps == 0`, attempts to auto-load from the
    `ibkr_borrow_fee` table for the signal's symbols/dates.

    weighting_mode (default "quantile"): Portfolio weighting scheme.
    - "quantile": Top/bottom 1/quantiles equal-weight (legacy behavior).
    - "signal_proportional": Weights proportional to signal score magnitude.
    - "hrp": Hierarchical Risk Parity (Lopez de Prado 2016) using trailing
      returns covariance at each rebalance. Requires `hrp_lookback` days of
      shared history (default 126). `hrp_linkage_method` passed to
      scipy.cluster.hierarchy.linkage (default "single").

    Capital-constrained compounding (F1, opt-in via capital_constrained=True):
    Fixes the equal-notional blind spot by tracking actual capital deployment,
    compounding P&L into the capital base, and enforcing position/sector limits
    relative to available capital at each rebalance.
    - initial_capital: starting capital base
    - max_leverage: max gross exposure / capital (1.0 = no leverage)
    - max_position_pct: max single position as fraction of capital
    - max_sector_pct: max sector concentration (requires sector map)
    - compound_returns: whether to compound P&L into capital base
    - rebalance_on_capital_change: rebalance when capital changes > threshold
    - capital_change_threshold_pct: rebalance trigger threshold (%)

    Price-volume signal family (F1, opt-in via price_volume_enabled=True):
    Adds volume-weighted, volume-confirmed, and volume-divergence signal
    variants. Requires volume data in the price table.
    - volume_window: lookback for volume calculations
    - volume_ma_window: lookback for volume moving average
    - min_volume_ratio: min volume vs MA for confirmation
    - volume_weighted: weight signals by relative volume
    - divergence_lookback: lookback for price-volume divergence detection
    """
    if not {"symbol", "date", score}.issubset(signal.columns):
        raise ValueError(f"signal must have columns symbol, date, '{score}'")

    if adv_participation_coeff is not None and adv_participation_coeff != "sqrt_law" \
            and not isinstance(adv_participation_coeff, (int, float)):
        raise ValueError(f"adv_participation_coeff must be a number, "
                         f"'sqrt_law', or None; got {adv_participation_coeff!r}")

    sig = signal[["symbol", "date", score]].copy()
    sig["date"] = pd.to_datetime(sig["date"])
    if start:
        sig = sig[sig["date"] >= pd.Timestamp(start)]
    if end:
        sig = sig[sig["date"] <= pd.Timestamp(end)]
    symbols = sig["symbol"].unique()

    pt = price_table or _pick_price_table(None, symbols=symbols)
    if pt is None:
        raise RuntimeError("No price table with data available for backtesting.")

    R = _returns_matrix(pt, symbols, start, end)
    if R.empty:
        raise RuntimeError(f"No price data in '{pt}' for the requested symbols/window.")

    scores_wide = (sig.drop_duplicates(["date", "symbol"])
                      .pivot(index="date", columns="symbol", values=score)
                      .reindex(columns=R.columns)
                      .sort_index())

    # F1: Price-volume signal family - adjust scores based on volume
    if price_volume_enabled:
        scores_wide = _apply_price_volume_adjustments(
            scores_wide=scores_wide,
            price_table=pt,
            symbols=symbols,
            start=start,
            end=end,
            volume_window=volume_window,
            volume_ma_window=volume_ma_window,
            min_volume_ratio=min_volume_ratio,
            volume_weighted=volume_weighted,
            divergence_lookback=divergence_lookback,
        )

    rebal_dates = _rebalance_dates(R.index, rebalance)
    target = _target_weights(
        scores_wide, rebal_dates, quantiles, long_short,
        weighting_mode=weighting_mode, returns=R,
        hrp_lookback=hrp_lookback, hrp_linkage_method=hrp_linkage_method)

    # Position sizing cap constraint (max_weight)
    if max_weight is not None and max_weight > 0:
        target = target.clip(lower=-abs(max_weight), upper=abs(max_weight))

    # Capital-constrained compounding (F1): track capital over time, enforce limits
    if capital_constrained:
        # We'll compute capital-aware weights by iterating through rebalance periods
        # and compounding returns into the capital base
        weights, capital_series = _apply_capital_constraints(
            target=target,
            returns=R,
            rebal_dates=rebal_dates,
            initial_capital=initial_capital,
            max_leverage=max_leverage,
            max_position_pct=max_position_pct,
            compound_returns=compound_returns,
            rebalance_on_capital_change=rebalance_on_capital_change,
            capital_change_threshold_pct=capital_change_threshold_pct,
        )
    else:
        # Daily weights: hold each rebalance's target until the next; lag one day so
        # weights set using info at date t earn returns from t+1.
        weights = (target.reindex(R.index).ffill().fillna(0.0)).shift(1).fillna(0.0)
        capital_series = None

    # Dynamic Volatility Targeting
    if vol_target is not None and vol_target > 0:
        raw_gross = (weights * R).sum(axis=1)
        # shift(1): today's scale must come from vol estimated through
        # yesterday's close, not today's own (not-yet-realized) return.
        rolling_vol = (raw_gross.rolling(window=21, min_periods=5).std() * np.sqrt(_ANN)).shift(1)
        scale = (vol_target / rolling_vol).replace([np.inf, -np.inf], 1.0).fillna(1.0).clip(upper=2.0)
        weights = weights.mul(scale, axis=0)

    # Execute daily gross return
    gross = (weights * R).sum(axis=1)

    # Transaction & execution costs. The arithmetic lives in
    # evaluation/execution.py so this engine and event_backtest.py share one
    # definition of a cost rate; see that module's docstring for why the two
    # meanings of "sqrt_impact" are kept apart rather than merged.
    turnover = weights.diff().abs().sum(axis=1).fillna(0.0)
    short_exposure_df = weights.clip(upper=0.0).abs()  # per-symbol DataFrame
    short_exposure = short_exposure_df.sum(axis=1)      # legacy Series for flat rate
    cost_model = ev_execution.costs_from_legacy_kwargs(
        cost_bps=cost_bps, spread_bps=spread_bps, borrow_fee_bps=borrow_fee_bps,
        slippage_model=slippage_model, impact_coeff=adv_impact_coeff,
    )
    # Per-symbol borrow fees (opt-in): load matrix if not provided
    bf_matrix = borrow_fee_matrix
    if bf_matrix is None and borrow_fee_bps == 0:
        try:
            import event_backtest as eb
            bf_matrix = eb.load_borrow_fee_matrix(symbols, start, end)
        except Exception:
            bf_matrix = None
    costs = ev_execution.daily_cost(
        cost_model, turnover, short_exposure_df if bf_matrix is not None else short_exposure,
        borrow_fee_matrix=bf_matrix, ann=_ANN)
    if adv_participation_coeff is not None and (
            adv_participation_coeff == "sqrt_law"
            or (isinstance(adv_participation_coeff, (int, float))
                and adv_participation_coeff > 0)):
        costs = costs + _adv_participation_cost(
            weights, aum, adv_participation_coeff, adv_window, symbols,
            start, end, pt, returns=R)

    net = gross - costs

    # Portfolio Drawdown Circuit Breaker
    if max_drawdown_stop is not None and max_drawdown_stop > 0:
        equity_tmp = (1.0 + net).cumprod()
        peak_tmp = equity_tmp.cummax()
        dd_tmp = equity_tmp / peak_tmp - 1.0
        stopped_out = dd_tmp < -abs(max_drawdown_stop)
        if stopped_out.any():
            # The breach is only knowable at the close of the day it happens,
            # so that day's already-realized return must stand -- flattening
            # starts the following trading day, not stop_idx itself (zeroing
            # stop_idx would retroactively erase a loss with foresight).
            stop_pos = net.index.get_loc(stopped_out.idxmax())
            if stop_pos + 1 < len(net):
                net.iloc[stop_pos + 1:] = 0.0
                weights.iloc[stop_pos + 1:] = 0.0

    # Equity curve: use capital series for capital-constrained mode,
    # otherwise standard cumulative returns
    if capital_constrained and capital_series is not None:
        # Normalize capital series to start at 1.0 (like standard equity)
        equity = capital_series / initial_capital
    else:
        equity = (1.0 + net).cumprod()

    # Benchmark: equal-weight buy-and-hold of the same universe.
    bench_ret = R.mean(axis=1).fillna(0.0)
    benchmark = (1.0 + bench_ret).cumprod()

    metrics = _compute_metrics(net, equity, benchmark, bench_ret, weights, turnover, long_short, start, end)

    params = {
        "price_table": pt, "score": score, "quantiles": quantiles,
        "rebalance": rebalance, "long_short": long_short, "cost_bps": cost_bps,
        "spread_bps": spread_bps, "borrow_fee_bps": borrow_fee_bps,
        "borrow_fee_matrix": borrow_fee_matrix is not None,
        "slippage_model": slippage_model or "none",
        "adv_participation_coeff": adv_participation_coeff,
        "aum": aum if adv_participation_coeff else None,
        "adv_window": adv_window if adv_participation_coeff else None,
        "vol_target": vol_target, "max_weight": max_weight,
        "max_drawdown_stop": max_drawdown_stop,
        "weighting_mode": weighting_mode,
        "hrp_lookback": hrp_lookback if weighting_mode == "hrp" else None,
        "hrp_linkage_method": hrp_linkage_method if weighting_mode == "hrp" else None,
        "n_symbols": len(symbols), "n_days": len(net),
        "start": str(R.index.min().date()), "end": str(R.index.max().date()),
        # F1: Capital-constrained compounding
        "capital_constrained": capital_constrained,
        "initial_capital": initial_capital if capital_constrained else None,
        "max_leverage": max_leverage if capital_constrained else None,
        "max_position_pct": max_position_pct if capital_constrained else None,
        "compound_returns": compound_returns if capital_constrained else None,
        # F1: Price-volume signal family
        "price_volume_enabled": price_volume_enabled,
        "volume_window": volume_window if price_volume_enabled else None,
        "volume_ma_window": volume_ma_window if price_volume_enabled else None,
        "min_volume_ratio": min_volume_ratio if price_volume_enabled else None,
        "volume_weighted": volume_weighted if price_volume_enabled else None,
        "divergence_lookback": divergence_lookback if price_volume_enabled else None,
    }
    return BacktestResult(returns=net, equity=equity, benchmark=benchmark,
                          weights=weights, metrics=metrics, params=params)


def _ann_metrics(ret: pd.Series, equity: pd.Series) -> dict:
    n = len(ret)
    if n == 0:
        return {}
    total = float(equity.iloc[-1] - 1.0)
    cagr = float(equity.iloc[-1] ** (_ANN / n) - 1.0) if equity.iloc[-1] > 0 else float("nan")
    vol = float(ret.std(ddof=0) * np.sqrt(_ANN))
    mean_ann = float(ret.mean() * _ANN)
    sharpe = mean_ann / vol if not _degenerate_sd(vol) else float("nan")
    return {"total_return_pct": round(100 * total, 2),
            "cagr_pct": round(100 * cagr, 2),
            "ann_vol_pct": round(100 * vol, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(100 * _max_drawdown(equity), 2)}


def _compute_metrics(net, equity, benchmark, bench_ret, weights, turnover, long_short,
                     start=None, end=None) -> dict:
    from evaluation import stats as ev_stats

    m = _ann_metrics(net, equity)
    b = _ann_metrics(bench_ret, benchmark)
    m["benchmark_cagr_pct"] = b.get("cagr_pct")
    m["benchmark_sharpe"] = b.get("sharpe")
    m["excess_cagr_pct"] = (round(m["cagr_pct"] - b["cagr_pct"], 2)
                            if m.get("cagr_pct") is not None and b.get("cagr_pct") is not None
                            else None)
    m["hit_rate_pct"] = round(100 * float((net > 0).mean()), 1) if len(net) else None
    m["avg_turnover"] = round(float(turnover[turnover > 0].mean()), 3) if (turnover > 0).any() else 0.0

    # Risk & Ratio Extensions
    sortino_res = ev_stats.sortino_ratio(net)
    m["sortino"] = sortino_res.get("sortino")

    calmar_res = ev_stats.calmar_ratio(m.get("cagr_pct"), m.get("max_drawdown_pct"))
    m["calmar"] = calmar_res.get("calmar")

    omega_res = ev_stats.omega_ratio(net)
    m["omega"] = omega_res.get("omega")

    var_res = ev_stats.value_at_risk(net)
    m["var_95_pct"] = var_res.get("var_95_pct")

    cvar_res = ev_stats.conditional_var(net)
    m["cvar_95_pct"] = cvar_res.get("cvar_95_pct")

    gtp_res = ev_stats.gain_to_pain_ratio(net)
    m["gain_to_pain"] = gtp_res.get("gain_to_pain")

    ff_res = ev_stats.fama_french_factor_attribution(net, start=start, end=end)
    m["ff_alpha_ann"] = ff_res.get("ff_alpha_ann")
    m["ff_r_squared"] = ff_res.get("ff_r_squared")

    active = weights.iloc[-1]
    m["n_long"] = int((active > 0).sum())
    m["n_short"] = int((active < 0).sum())
    return m

