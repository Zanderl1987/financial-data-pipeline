"""
strategies/ports/ -- Stage 2: Pine-to-TradeRule translation.

One module per admitted strategy. Each port is a pure function of a per-symbol
OHLCV frame (open/high/low/close/volume, DatetimeIndex) that builds a
evaluation.contracts.TradeRule via strategies.ports.base.stateful_rule.

`translation_verified` is "unverified" for every port in this campaign unless
and until a port gains hand-computed unit tests (then "unit_tested") -- see
the pre-registration, section 6. Ports use analytics.technical primitives
where one exists.

Usage
-----
    from strategies.ports import load_rule, all_ports

    rule = load_rule("hybrid_breakout_vcp")       # TradeRule over OHLCV frames
    for info in all_ports():
        print(info.slug, info.translation_verified, info.notes)
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from evaluation.contracts import TradeRule

_PORTS: Dict[str, "PortInfo"] = {}
_BUILDERS: Dict[str, Callable] = {}
_DISCOVERED = False


@dataclass
class PortInfo:
    """Provenance + fidelity metadata for one translated strategy."""
    slug: str
    tv_url: str
    tv_author: str
    tv_script_name: str
    mechanism_family: str
    param_count: int
    translation_verified: str
    notes: List[str] = field(default_factory=list)


def _register(info: PortInfo, builder: Callable) -> None:
    _PORTS[info.slug] = info
    _BUILDERS[info.slug] = builder


def _discover() -> None:
    """Import every sibling module under strategies/ports/ once, so each
    port's module-level `_register()` call fires. `base` is machinery, not a
    port, and is skipped."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    pkg = importlib.import_module(__name__)
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name == "base":
            continue
        importlib.import_module(f"{__name__}.{mod.name}")
    _DISCOVERED = True


def load_rule(slug: str, params: Optional[dict] = None) -> TradeRule:
    """Build the TradeRule for a ported strategy slug (author defaults unless
    `params` overrides specific inputs)."""
    _discover()
    if slug not in _BUILDERS:
        raise KeyError(f"no ported strategy {slug!r}; have: {sorted(_BUILDERS)}")
    return _BUILDERS[slug](params)


def all_ports() -> List[PortInfo]:
    """Every registered port, newest first."""
    _discover()
    return sorted(_PORTS.values(), key=lambda i: i.slug, reverse=True)


def port_info(slug: str) -> PortInfo:
    _discover()
    if slug not in _PORTS:
        raise KeyError(f"no ported strategy {slug!r}; have: {sorted(_PORTS)}")
    return _PORTS[slug]
