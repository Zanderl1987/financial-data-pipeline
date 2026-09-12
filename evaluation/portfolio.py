"""
evaluation/portfolio.py -- quantile-portfolio evaluation of a signal frame.
Thin wrapper over backtest.backtest (which already lags weights one day, so
weights set with info at t earn returns from t+1 -- PIT-safe by construction).

F1 additions:
- capital_constrained: dict with keys (enabled, initial_capital, max_leverage,
  max_position_pct, max_sector_pct, compound_returns, rebalance_on_capital_change,
  capital_change_threshold_pct) -- enables capital-constrained compounding mode
- price_volume: dict with keys (enabled, volume_window, volume_ma_window,
  min_volume_ratio, volume_weighted, divergence_lookback) -- enables price-volume
  signal family extensions
"""

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class CapitalConstrainedParams:
    """Capital-constrained compounding parameters (F1).
    
    Fixes the equal-notional blind spot by tracking actual capital
    deployment, compounding P&L, and enforcing position limits relative
    to available capital at each rebalance.
    """
    enabled: bool = False
    initial_capital: float = 1_000_000.0
    max_leverage: float = 1.0
    max_position_pct: float = 0.10
    max_sector_pct: float = 0.30
    compound_returns: bool = True
    rebalance_on_capital_change: bool = True
    capital_change_threshold_pct: float = 5.0


@dataclass
class PriceVolumeSignalParams:
    """Price-volume signal family extension (F1).
    
    Adds volume-weighted, volume-confirmed, and volume-divergence
    signal variants to the evaluation framework.
    """
    enabled: bool = False
    volume_window: int = 21
    volume_ma_window: int = 63
    min_volume_ratio: float = 1.5
    volume_weighted: bool = True
    divergence_lookback: int = 5


def evaluate_portfolio(frame: pd.DataFrame, direction: int = 1,
                       quantiles: int = 5, rebalance: str = "M",
                       long_short: bool = True, start=None, end=None,
                       price_table=None, cost_bps: float = 0.0,
                       capital_constrained: dict | None = None,
                       price_volume: dict | None = None,
                       spread_bps: float = 0.0,
                       borrow_fee_bps: float = 0.0,
                       slippage_model: str | None = None,
                       adv_impact_coeff: float = 0.1,
                       adv_participation_coeff: float | str | None = None,
                       aum: float = 1_000_000.0,
                       adv_window: int = 20,
                       vol_target: float | None = None,
                       max_weight: float | None = None,
                       max_drawdown_stop: float | None = None,
                       weighting_mode: str = "quantile",
                       hrp_lookback: int = 126,
                       hrp_linkage_method: str = "single"):
    """frame: LAG-APPLIED signal frame (symbol, date, value). Returns BacktestResult.
    
    Args:
        capital_constrained: dict with capital-constrained compounding params (F1)
        price_volume: dict with price-volume signal family params (F1)
    """
    import backtest as bt               # local import: repo test convention
    df = frame[["symbol", "date", "value"]].copy()
    if direction == -1:
        df["value"] = -df["value"]

    # Extract capital_constrained params for backtest
    cc_kwargs = {}
    if capital_constrained and capital_constrained.get("enabled", False):
        cc = capital_constrained
        cc_kwargs = {
            "capital_constrained": True,
            "initial_capital": cc.get("initial_capital", 1_000_000.0),
            "max_leverage": cc.get("max_leverage", 1.0),
            "max_position_pct": cc.get("max_position_pct", 0.10),
            "max_sector_pct": cc.get("max_sector_pct", 0.30),
            "compound_returns": cc.get("compound_returns", True),
            "rebalance_on_capital_change": cc.get("rebalance_on_capital_change", True),
            "capital_change_threshold_pct": cc.get("capital_change_threshold_pct", 5.0),
        }

    # Extract price_volume params for backtest (if signal enrichment needed)
    pv_kwargs = {}
    if price_volume and price_volume.get("enabled", False):
        pv = price_volume
        pv_kwargs = {
            "price_volume_enabled": True,
            "volume_window": pv.get("volume_window", 21),
            "volume_ma_window": pv.get("volume_ma_window", 63),
            "min_volume_ratio": pv.get("min_volume_ratio", 1.5),
            "volume_weighted": pv.get("volume_weighted", True),
            "divergence_lookback": pv.get("divergence_lookback", 5),
        }

    return bt.backtest(df, score="value", quantiles=quantiles,
                       rebalance=rebalance, long_short=long_short,
                       start=start, end=end, price_table=price_table,
                       cost_bps=cost_bps, spread_bps=spread_bps,
                       borrow_fee_bps=borrow_fee_bps, slippage_model=slippage_model,
                       adv_impact_coeff=adv_impact_coeff,
                       adv_participation_coeff=adv_participation_coeff,
                       aum=aum, adv_window=adv_window, vol_target=vol_target,
                       max_weight=max_weight, max_drawdown_stop=max_drawdown_stop,
                       weighting_mode=weighting_mode, hrp_lookback=hrp_lookback,
                       hrp_linkage_method=hrp_linkage_method,
                       **cc_kwargs, **pv_kwargs)


def _json_safe(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def summarize_portfolio(res) -> dict:
    """JSON-safe {metrics, params} from a BacktestResult."""
    return {"metrics": {k: _json_safe(v) for k, v in res.metrics.items()},
            "params": {k: _json_safe(v) for k, v in res.params.items()}}
