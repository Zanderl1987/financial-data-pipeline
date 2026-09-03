"""
evaluation/execution.py -- the one place execution semantics are defined:
transaction costs, risk controls, position sizing, and portfolio limits.

Step A of the W1 unification (see docs/superpowers/specs/2026-08-16-execution-
engine-unification-design.md) introduces the config types and the two shared
cost functions, and points backtest.py / event_backtest.py / strategies/stage3.py
at them. It changes NO behavior -- tests/test_execution_golden.py pins that.

Step B consumes the RiskControls / Sizing / PortfolioLimits groups inside
evaluation/trades.py. Until then those groups are carried but not read, which is
why there is no cross-engine applicability validation here yet: a config does not
know which engine it is about to be handed to. That check belongs at the point of
use, in Step B.

Two things this module deliberately does NOT unify
--------------------------------------------------
1. WHERE the cost lands. Discrete-trade engines charge a fixed round trip per
   trade; the weight-matrix engine charges turnover x rate per day. Same rate,
   different application point. `round_trip_rate` and `daily_cost` are separate
   for exactly that reason.

2. What "sqrt_impact" means. The two existing engines use that one string for
   two unrelated models:
     - backtest.py       : turnover**0.5 * (adv_impact_coeff / 1e4)   -- a real
                           square-root impact function, coefficient-scaled
     - event_backtest.py : a flat +0.0010 per side, no root, no coefficient
   Collapsing them would silently move every result that passes the flag, so
   they are kept apart here as impact_model="sqrt" and impact_model="flat".
   Whether the flat model deserves to exist is a research question (W2), not a
   refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

TRADING_DAYS = 252

IMPACT_MODELS = (None, "sqrt", "flat")
SIZING_MODES = ("fixed_notional", "fixed_fraction")


@dataclass(frozen=True)
class CostModel:
    """
    commission_bps : per side.
    spread_bps     : the FULL quoted spread; half of it is charged per side.
    borrow_fee_bps : annualized. Accrued daily on short exposure in the
                     weight-matrix engine (daily_cost(), below); accrued over
                     the actual holding period per short trade in the discrete
                     trade simulator (trades.simulate_symbol) -- same rate,
                     two different accrual shapes since one engine has a daily
                     weight series and the other has variable-length trades.
    impact_model   : None | "sqrt" | "flat" -- see module docstring.
    impact_coeff   : for "sqrt", backtest.py's adv_impact_coeff (its own default
                     is 0.1, NOT 0.0 -- callers must pass that through);
                     for "flat", bps added per side (event_backtest.py uses 10.0).
    """
    commission_bps: float = 0.0
    spread_bps: float = 0.0
    borrow_fee_bps: float = 0.0
    impact_model: "str | None" = None
    impact_coeff: float = 0.0

    def __post_init__(self):
        if self.impact_model not in IMPACT_MODELS:
            raise ValueError(f"impact_model must be one of {IMPACT_MODELS}, "
                             f"got {self.impact_model!r}")
        for name in ("commission_bps", "spread_bps", "borrow_fee_bps", "impact_coeff"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")


@dataclass(frozen=True)
class RiskControls:
    """Per-position exits. Consumed by the discrete-trade engine in Step B.

    vol_stop_mult is named for what the existing implementation measures. The
    parameter it replaces is called `atr_stop_mult`, but event_backtest.py's
    computation is `window_px.diff().abs().mean()` -- the mean absolute
    close-to-close change over 14 days, with no highs, no lows, and no prior
    close. That is a close-only volatility proxy, not average true range. The
    math is preserved; only the misleading name is dropped.
    """
    stop_loss_pct: "float | None" = None
    take_profit_pct: "float | None" = None
    vol_stop_mult: "float | None" = None
    trailing: bool = False
    max_holding_days: "int | None" = None

    def __post_init__(self):
        for name in ("stop_loss_pct", "take_profit_pct", "vol_stop_mult"):
            v = getattr(self, name)
            if v is not None and v <= 0:
                raise ValueError(f"{name} must be > 0 or None, got {v}")
        if self.max_holding_days is not None and self.max_holding_days < 1:
            raise ValueError("max_holding_days must be >= 1 or None")


@dataclass(frozen=True)
class Sizing:
    """max_weight applies to the weight-matrix engine; mode/notional to the
    discrete-trade engine. Vol targeting is deliberately absent: it is
    well-defined for a continuously-held weight vector (backtest.py keeps its
    own) and ambiguous for discrete trades."""
    mode: str = "fixed_notional"
    notional: float = 10_000.0
    fraction: "float | None" = None
    max_weight: "float | None" = None

    def __post_init__(self):
        if self.mode not in SIZING_MODES:
            raise ValueError(f"mode must be one of {SIZING_MODES}, got {self.mode!r}")
        if self.notional <= 0:
            raise ValueError("notional must be > 0")
        if self.max_weight is not None and self.max_weight <= 0:
            raise ValueError("max_weight must be > 0 or None")
        if self.mode == "fixed_fraction":
            if self.fraction is None or not (0 < self.fraction <= 1):
                raise ValueError("mode='fixed_fraction' requires 0 < fraction <= 1")
        elif self.fraction is not None:
            raise ValueError("fraction is only meaningful with mode='fixed_fraction'")


@dataclass(frozen=True)
class PortfolioLimits:
    """capital/max_concurrent gate the discrete-trade engine (Step B);
    max_drawdown_stop is the weight engine's circuit breaker.
    None everywhere == today's behavior: unlimited."""
    capital: "float | None" = None
    max_concurrent: "int | None" = None
    max_drawdown_stop: "float | None" = None

    def __post_init__(self):
        if self.capital is not None and self.capital <= 0:
            raise ValueError("capital must be > 0 or None")
        if self.max_concurrent is not None and self.max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1 or None")
        if self.max_drawdown_stop is not None and not (0 < self.max_drawdown_stop < 1):
            raise ValueError("max_drawdown_stop must be a fraction in (0, 1) or None")


@dataclass(frozen=True)
class ExecutionConfig:
    """Frozen so it can be hashed into a registry row -- every recorded result
    should carry the execution semantics that produced it."""
    name: str = "default"
    costs: CostModel = field(default_factory=CostModel)
    risk: RiskControls = field(default_factory=RiskControls)
    sizing: Sizing = field(default_factory=Sizing)
    limits: PortfolioLimits = field(default_factory=PortfolioLimits)

    def with_costs(self, **kw) -> "ExecutionConfig":
        return replace(self, costs=replace(self.costs, **kw))

    def as_dict(self) -> dict:
        """Flat, JSON-safe, stable key order -- for registry rows and params."""
        out = {"config_name": self.name}
        for group in ("costs", "risk", "sizing", "limits"):
            obj = getattr(self, group)
            for f_name in obj.__dataclass_fields__:
                out[f"{group}.{f_name}"] = getattr(obj, f_name)
        return out


#: Today's behavior, exactly: no costs, no stops, unlimited concurrency.
LEGACY = ExecutionConfig(name="legacy")

#: The TV strategy catalog campaign's pre-registered primary cost model:
#: 10 bps per side, 20 bps round trip. Reproduces strategies/stage3.py's
#: PRIMARY_COST_BPS. See experiments/2026-08-11_tv-strategy-catalog-preregistration.md.
TV_CAMPAIGN = ExecutionConfig(name="tv_campaign",
                              costs=CostModel(commission_bps=10.0))


def resolve(config: "ExecutionConfig | None") -> ExecutionConfig:
    """None means LEGACY. Centralized so every call site agrees."""
    return LEGACY if config is None else config


def config_hash(config: "ExecutionConfig | None") -> str:
    """
    Stable 12-hex digest of the execution semantics, for registry rows.

    Deliberately excludes `name`: two configs that price and size trades
    identically must hash identically regardless of what they are called, or
    the registry would treat a rename as a different experiment.
    """
    import hashlib
    cfg = resolve(config)
    payload = {k: v for k, v in cfg.as_dict().items() if k != "config_name"}
    blob = ";".join(f"{k}={payload[k]!r}" for k in sorted(payload))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ cost math

def per_side_rate(costs: CostModel) -> float:
    """
    Fractional cost charged once per side.

    Reproduces `(cost_bps + spread_bps / 2.0) / 1e4` as written at
    backtest.py:198 and event_backtest.py:346, plus the flat impact model's
    constant (event_backtest.py:348 adds 0.0010, i.e. impact_coeff=10.0 bps).

    The "sqrt" impact model is NOT included here -- it is a function of
    turnover, not a per-side constant, and belongs in daily_cost().
    """
    rate = (costs.commission_bps + costs.spread_bps / 2.0) / 1e4
    if costs.impact_model == "flat":
        rate += costs.impact_coeff / 1e4
    return rate


def round_trip_rate(costs: CostModel) -> float:
    """Fractional cost of entering and exiting once (two sides)."""
    return 2.0 * per_side_rate(costs)


def daily_cost(costs: CostModel,
               turnover: pd.Series,
               short_exposure: "pd.Series | None" = None,
               ann: int = TRADING_DAYS) -> pd.Series:
    """
    Per-day cost series for the weight-matrix engine.

    Reproduces backtest.py:196-209 exactly, including its guards: the borrow fee
    is applied only when > 0, and sqrt impact only when both the model is
    selected AND impact_coeff > 0 (a zero coefficient adds nothing rather than
    adding zero, which matters for float reproducibility).

    Note the asymmetry with per_side_rate(): the flat impact model is a per-side
    constant and has no meaning for a turnover-based charge, so it is not applied
    here. Passing a "flat" config to this function is a caller error that Step B
    will validate at the point of use.
    """
    costs_series = turnover * (
        (costs.commission_bps + costs.spread_bps / 2.0) / 1e4
    )
    if costs.borrow_fee_bps > 0 and short_exposure is not None:
        costs_series = costs_series + short_exposure * (
            costs.borrow_fee_bps / (1e4 * ann)
        )
    if costs.impact_model == "sqrt" and costs.impact_coeff > 0:
        costs_series = costs_series + turnover.pow(0.5) * (costs.impact_coeff / 1e4)
    return costs_series


def costs_from_legacy_kwargs(cost_bps: float = 0.0,
                             spread_bps: float = 0.0,
                             borrow_fee_bps: float = 0.0,
                             slippage_model: "str | None" = None,
                             impact_coeff: float = 0.0,
                             flat_impact_bps: float = 0.0) -> CostModel:
    """
    Shim translating the engines' existing flat kwargs into a CostModel, so
    Step A needs no call-site signature changes.

    `slippage_model="sqrt_impact"` maps to whichever model the CALLING engine
    actually implements -- the caller says which by passing impact_coeff (sqrt,
    backtest.py) or flat_impact_bps (flat, event_backtest.py). This is the seam
    where the two meanings of that one string are kept apart; see the module
    docstring.
    """
    if slippage_model is None:
        return CostModel(commission_bps=cost_bps, spread_bps=spread_bps,
                         borrow_fee_bps=borrow_fee_bps)
    if slippage_model != "sqrt_impact":
        raise ValueError(f"unknown slippage_model {slippage_model!r}")
    if flat_impact_bps:
        return CostModel(commission_bps=cost_bps, spread_bps=spread_bps,
                         borrow_fee_bps=borrow_fee_bps,
                         impact_model="flat", impact_coeff=flat_impact_bps)
    return CostModel(commission_bps=cost_bps, spread_bps=spread_bps,
                     borrow_fee_bps=borrow_fee_bps,
                     impact_model="sqrt", impact_coeff=impact_coeff)
