"""
evaluation/config.py — YAML-configurable evaluation runner (F1).

Loads evaluation specs from YAML files, enabling reproducible, versioned
evaluation runs without code changes.
"""

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


@dataclass
class SignalConfig:
    """Signal evaluation configuration."""
    name: str
    source: str  # "parquet", "signal-panel", "sentiment", "rating", "rating-changes"
    parquet_path: str | None = None
    factor: str = "composite"
    signal_col: str = "rating_all"
    min_step: int = 1
    lag_days: int = 1
    direction: int = 1


@dataclass
class EventSetConfig:
    """EventSet evaluation configuration."""
    name: str
    source: str  # "parquet"
    parquet_path: str | None = None
    lag_days: int = 1
    min_events: int = 10


@dataclass
class TradeRuleConfig:
    """TradeRule evaluation configuration."""
    name: str
    source: str  # "tv-rule", "custom" (not yet implemented)
    symbols: list[str] | None = None
    price_table: str | None = None
    start: str | None = None
    end: str | None = None


@dataclass
class UniverseConfig:
    """Universe/eligibility configuration."""
    symbols: list[str] | None = None
    exclude_otc: bool = False
    min_dollar_volume: float | None = None
    sp500_only: bool = False


@dataclass
class EvaluationParams:
    """Core evaluation parameters."""
    benchmark: str = "SPY"
    price_table: str | None = None
    quantiles: int = 5
    rebalance: str = "M"
    long_short: bool = True
    n_boot: int = 1000
    n_perm: int = 200
    seed: int = 0


@dataclass
class AdvancedParams:
    """Advanced/opt-in evaluation parameters."""
    meta_label: bool = False
    meta_threshold: float = 0.5
    meta_min_train: int = 50
    meta_refit_every: int = 20
    meta_l2: float = 1.0
    regime_report: bool = False
    regime_benchmark: str = "SPY"
    regime_k: int = 2
    tax: bool = False
    robustness: bool = False
    robustness_n_trials: int = 100
    robustness_sigma_bps: float = 5.0
    robustness_alpha: float = 0.95


@dataclass
class PortfolioParams:
    """Portfolio backtest parameters."""
    cost_bps: float = 0.0
    spread_bps: float = 0.0
    borrow_fee_bps: float = 0.0
    slippage_model: str | None = None
    adv_impact_coeff: float = 0.1
    adv_participation_coeff: float | str | None = None
    aum: float = 1_000_000.0
    adv_window: int = 20
    vol_target: float | None = None
    max_weight: float | None = None
    max_drawdown_stop: float | None = None
    weighting_mode: str = "quantile"
    hrp_lookback: int = 126
    hrp_linkage_method: str = "single"


@dataclass
class CapitalConstrainedParams:
    """Capital-constrained compounding parameters (F1).
    
    Fixes the equal-notional blind spot by tracking actual capital
    deployment, compounding P&L, and enforcing position limits relative
    to available capital at each rebalance.
    """
    enabled: bool = False
    initial_capital: float = 1_000_000.0
    max_leverage: float = 1.0  # 1.0 = no leverage, >1 = leverage allowed
    max_position_pct: float = 0.10  # max single position as fraction of capital
    max_sector_pct: float = 0.30  # max sector concentration
    compound_returns: bool = True  # compound P&L into capital base
    rebalance_on_capital_change: bool = True  # rebalance when capital changes > threshold
    capital_change_threshold_pct: float = 5.0  # rebalance if capital changes > 5%


@dataclass
class PriceVolumeSignalParams:
    """Price-volume signal family extension (F1).
    
    Adds volume-weighted, volume-confirmed, and volume-divergence
    signal variants to the evaluation framework.
    """
    enabled: bool = False
    volume_window: int = 21
    volume_ma_window: int = 63
    min_volume_ratio: float = 1.5  # min volume vs MA for confirmation
    volume_weighted: bool = True  # weight signals by relative volume
    divergence_lookback: int = 5  # lookback for price-volume divergence


@dataclass
class OutputConfig:
    """Output configuration."""
    out_root: str = os.path.join("storage", "reports", "eval")
    registry_path: str | None = None
    write_registry: bool = True
    write_artifacts: bool = True


@dataclass
class EvaluationSpec:
    """Complete evaluation specification from YAML."""
    version: int = 1
    description: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    signal: SignalConfig | None = None
    events: EventSetConfig | None = None
    trade_rule: TradeRuleConfig | None = None
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    evaluation: EvaluationParams = field(default_factory=EvaluationParams)
    advanced: AdvancedParams = field(default_factory=AdvancedParams)
    portfolio: PortfolioParams = field(default_factory=PortfolioParams)
    capital_constrained: CapitalConstrainedParams = field(default_factory=CapitalConstrainedParams)
    price_volume: PriceVolumeSignalParams = field(default_factory=PriceVolumeSignalParams)
    output: OutputConfig = field(default_factory=OutputConfig)


def load_spec(path: str) -> EvaluationSpec:
    """Load an EvaluationSpec from a YAML file."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return _dict_to_spec(data)


def _dict_to_spec(data: dict) -> EvaluationSpec:
    """Convert a dict (from YAML) to EvaluationSpec."""
    spec = EvaluationSpec(
        version=data.get("version", 1),
        description=data.get("description", ""),
        created_at=data.get("created_at", datetime.now().isoformat()),
    )

    # Signal config
    if "signal" in data:
        spec.signal = SignalConfig(**data["signal"])

    # Events config
    if "events" in data:
        spec.events = EventSetConfig(**data["events"])

    # Trade rule config
    if "trade_rule" in data:
        spec.trade_rule = TradeRuleConfig(**data["trade_rule"])

    # Universe config
    if "universe" in data:
        spec.universe = UniverseConfig(**data["universe"])

    # Evaluation params
    if "evaluation" in data:
        spec.evaluation = EvaluationParams(**data["evaluation"])

    # Advanced params
    if "advanced" in data:
        spec.advanced = AdvancedParams(**data["advanced"])

    # Portfolio params
    if "portfolio" in data:
        spec.portfolio = PortfolioParams(**data["portfolio"])

    # Capital constrained params (F1)
    if "capital_constrained" in data:
        spec.capital_constrained = CapitalConstrainedParams(**data["capital_constrained"])

    # Price-volume params (F1)
    if "price_volume" in data:
        spec.price_volume = PriceVolumeSignalParams(**data["price_volume"])

    # Output config
    if "output" in data:
        spec.output = OutputConfig(**data["output"])

    return spec


def spec_to_runner_kwargs(spec: EvaluationSpec) -> dict:
    """Convert EvaluationSpec to kwargs for runner.run()."""
    kwargs = {}

    # Universe
    kwargs["universe"] = spec.universe.symbols
    # Universe eligibility handled in evaluate.py adapter path

    # Evaluation params
    kwargs["benchmark"] = spec.evaluation.benchmark
    kwargs["price_table"] = spec.evaluation.price_table
    kwargs["quantiles"] = spec.evaluation.quantiles
    kwargs["rebalance"] = spec.evaluation.rebalance
    # long_short is determined by signal direction or trade rule
    kwargs["n_boot"] = spec.evaluation.n_boot
    kwargs["n_perm"] = spec.evaluation.n_perm
    kwargs["seed"] = spec.evaluation.seed

    # Advanced params
    kwargs["meta_label"] = spec.advanced.meta_label
    kwargs["meta_threshold"] = spec.advanced.meta_threshold
    kwargs["meta_min_train"] = spec.advanced.meta_min_train
    kwargs["meta_refit_every"] = spec.advanced.meta_refit_every
    kwargs["meta_l2"] = spec.advanced.meta_l2
    kwargs["regime_report"] = spec.advanced.regime_report
    kwargs["regime_benchmark"] = spec.advanced.regime_benchmark
    kwargs["regime_k"] = spec.advanced.regime_k
    kwargs["tax"] = spec.advanced.tax
    kwargs["robustness"] = spec.advanced.robustness
    kwargs["robustness_n_trials"] = spec.advanced.robustness_n_trials
    kwargs["robustness_sigma_bps"] = spec.advanced.robustness_sigma_bps
    kwargs["robustness_alpha"] = spec.advanced.robustness_alpha

    # Portfolio params (passed through to backtest via runner)
    kwargs["cost_bps"] = spec.portfolio.cost_bps
    kwargs["spread_bps"] = spec.portfolio.spread_bps
    kwargs["borrow_fee_bps"] = spec.portfolio.borrow_fee_bps
    kwargs["slippage_model"] = spec.portfolio.slippage_model
    kwargs["adv_impact_coeff"] = spec.portfolio.adv_impact_coeff
    kwargs["adv_participation_coeff"] = spec.portfolio.adv_participation_coeff
    kwargs["aum"] = spec.portfolio.aum
    kwargs["adv_window"] = spec.portfolio.adv_window
    kwargs["vol_target"] = spec.portfolio.vol_target
    kwargs["max_weight"] = spec.portfolio.max_weight
    kwargs["max_drawdown_stop"] = spec.portfolio.max_drawdown_stop
    kwargs["weighting_mode"] = spec.portfolio.weighting_mode
    kwargs["hrp_lookback"] = spec.portfolio.hrp_lookback
    kwargs["hrp_linkage_method"] = spec.portfolio.hrp_linkage_method

    # Capital constrained params (F1) - passed through to portfolio evaluation
    kwargs["capital_constrained"] = spec.capital_constrained

    # Price-volume params (F1) - for signal construction
    kwargs["price_volume"] = spec.price_volume

    # Output
    kwargs["out_root"] = spec.output.out_root
    kwargs["registry_path"] = spec.output.registry_path
    kwargs["write_registry"] = spec.output.write_registry

    return kwargs


# Example YAML template
EXAMPLE_YAML = """# Evaluation specification for financial-data-pipeline
# Save as e.g. eval_spec_my_signal.yaml and run:
#   python -m evaluation.runner_yaml eval_spec_my_signal.yaml

version: 1
description: "Example signal evaluation with capital-constrained compounding"
created_at: "2026-09-12T00:00:00"

signal:
  name: "my_momentum_signal"
  source: "parquet"
  parquet_path: "storage/signals/my_momentum.parquet"
  lag_days: 1
  direction: 1

universe:
  symbols: null  # null = all symbols in signal
  exclude_otc: true
  min_dollar_volume: 5000000  # $5M trailing 21d
  sp500_only: false

evaluation:
  benchmark: "SPY"
  price_table: null
  quantiles: 5
  rebalance: "M"
  long_short: true
  n_boot: 1000
  n_perm: 200
  seed: 0

advanced:
  meta_label: false
  meta_threshold: 0.5
  meta_min_train: 50
  meta_refit_every: 20
  meta_l2: 1.0
  regime_report: true
  regime_benchmark: "SPY"
  regime_k: 2
  tax: false
  robustness: false
  robustness_n_trials: 100
  robustness_sigma_bps: 5.0
  robustness_alpha: 0.95

portfolio:
  cost_bps: 1.0
  spread_bps: 0.5
  borrow_fee_bps: 0.0
  slippage_model: "sqrt"
  adv_impact_coeff: 0.1
  adv_participation_coeff: null
  aum: 1000000.0
  adv_window: 20
  vol_target: null
  max_weight: null
  max_drawdown_stop: null
  weighting_mode: "quantile"
  hrp_lookback: 126
  hrp_linkage_method: "single"

capital_constrained:  # F1: capital-constrained compounding
  enabled: true
  initial_capital: 1000000.0
  max_leverage: 1.0
  max_position_pct: 0.10
  max_sector_pct: 0.30
  compound_returns: true
  rebalance_on_capital_change: true
  capital_change_threshold_pct: 5.0

price_volume:  # F1: price-volume signal family
  enabled: false
  volume_window: 21
  volume_ma_window: 63
  min_volume_ratio: 1.5
  volume_weighted: true
  divergence_lookback: 5

output:
  out_root: "storage/reports/eval"
  registry_path: null
  write_registry: true
  write_artifacts: true
"""

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Evaluation config utilities")
    ap.add_argument("--print-example", action="store_true",
                    help="Print example YAML to stdout")
    ap.add_argument("--validate", type=str, help="Validate a YAML spec file")
    args = ap.parse_args()

    if args.print_example:
        print(EXAMPLE_YAML)
    elif args.validate:
        spec = load_spec(args.validate)
        print(f"Valid: {spec.description}")
        print(f"  Signal: {spec.signal}")
        print(f"  Events: {spec.events}")
        print(f"  TradeRule: {spec.trade_rule}")
        print(f"  Capital constrained: {spec.capital_constrained.enabled}")
        print(f"  Price-volume: {spec.price_volume.enabled}")
    else:
        ap.print_help()