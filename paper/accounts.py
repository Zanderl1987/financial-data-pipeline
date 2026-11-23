"""Paper-trading account specs and shared constants."""
from __future__ import annotations

from dataclasses import dataclass

SLOT_USD = 1000.0
LOSS_LIMIT_USD = 1000.0
BORROW_FEE_BPS_YR = 270.0
PRICE_FLOOR = 5.0


@dataclass(frozen=True)
class AccountSpec:
    key: str
    slug: str
    start_equity: float
    slots: int
    login: str

    @property
    def env_key(self) -> str:
        return f"APCA_KEY_{self.key.upper()}"

    @property
    def env_secret(self) -> str:
        return f"APCA_SECRET_{self.key.upper()}"


_SLUGS = {
    "joey": "joey_stochrsi_atr",
    "cap": "capitulation_stretch_reversion",
    "rsibb": "rsi_bb_inside_strategy",
}

ACCOUNTS: tuple[AccountSpec, ...] = tuple(
    AccountSpec(f"{short}_{tag}", slug, equity, slots, login)
    for tag, equity, slots, login in (("5k", 5000.0, 5, "A"), ("10k", 10000.0, 10, "B"))
    for short, slug in _SLUGS.items()
)
