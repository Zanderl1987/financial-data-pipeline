"""
strategies/ -- TradingView community strategy catalog.

Stage 1 of the campaign pre-registered in
experiments/2026-08-11_tv-strategy-catalog-preregistration.md: screen collected
Pine source for disqualifying constructs before any translation effort is spent.
"""

from strategies.screen import (  # noqa: F401
    ScreenResult,
    screen_source,
    EXCLUSION_CODES,
)
