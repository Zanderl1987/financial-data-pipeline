"""
Name -> ticker resolution for disclosure-style data that reports entity NAMES
(CA Form 700, gift/travel perk schedules, holdings in any raw filing) rather
than machine-readable tickers.

The resolver matches a reported entity name against the repo's canonical
company-name index (the `securities` reference table) plus a curated canonical
alias table. It is intentionally conservative: on ambiguous or garbage input it
returns None rather than a wrong symbol. A wrong ticker in an event study is a
look-ahead-quality error, not a coverage miss.

Match tiers, in order of trust:

  canonical  - exact hit on the curated alias table (CTRL-Q style: well-known
               blue chips, ADRs of foreign names, split/kennel collisions):
               the only tier for names that `securities` does not cover.
  exact      - the normalized name equals a normalized `securities` company_name.
  tokens     - normalized name's significant-token set equals a company_name's.
               Only accepted when the candidate is an unambiguous single match.

`securities` also carries index-membership flags (is_russell3000, is_sp500,
is_wilshire5000); the shell-collision class (junk legacy listings like
"DUKE ENERGY CORP" as DUUKU with no exchange/flags) is rejected by a caller-
selectable `require_listed` filter (default on for the event-study tier).

Usage:
    from name_to_ticker import NameResolver
    r = NameResolver()
    r.resolve("Coca Cola Co")        # -> {"symbol": "KO", "method": "canonical", ...}
    r.resolve_frame(named_series)    # -> DataFrame with entity/symbol/method
    r.unresolved(named_series)       # -> entities that failed, for review

CLI:
    python name_to_ticker.py --names "Coca Cola Co" "Apple Inc" "DUUKU"
"""

from __future__ import annotations

import argparse
import re
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query as q  # noqa: E402

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# Legal/residue words stripped from the END of a name before token matching.
# Deliberately excludes descriptive words (INTERNATIONAL, GROUP, HOLDINGS,
# TECHNOLOGIES) that are usually part of the identifying name, and share-class
# junk (ADR, CLASS, COM, ...) that Form-700-style extractions append.
_SUFFIX_WORDS = {
    "INC", "CORP", "CO", "COMPANY", "COMPANIES", "CORPORATION",
    "PLC", "LP", "L P", "LTD", "LIMITED", "LLC", "LLP",
    "SA", "S A", "S/A", "NV", "N V", "SE", "AG", "A G",
    "ADR", "ADRS", "CLASS", "CL", "CLS", "STK", "COM", "DEL", "NEW",
}

_NONWORD_RE = re.compile(r"[^A-Z0-9]+")


def normalize(name: str) -> str:
    """Upper, collapse punctuation/whitespace. '& ' -> plain space, single-space."""
    return _NONWORD_RE.sub(" ", str(name).upper()).strip()


def _significant_tokens(name: str) -> tuple[str, ...]:
    """Normalized token tuple with suffix/residue words stripped from the end."""
    words = normalize(name).split()
    while words and (words[-1] in _SUFFIX_WORDS or words[-1].isdigit()
                     or len(words[-1]) <= 1):
        words.pop()
    return tuple(words)


# ---------------------------------------------------------------------------
# Curated canonical aliases (normalized name -> symbol). Covers names the
# `securities` index lacks: ADRs of foreign blue chips, and renames/shell
# collisions where trusting the token index would point at the wrong series.
# ---------------------------------------------------------------------------

CANONICAL: dict[str, str] = {
    "BANK AMERICA": "BAC",
    "BANK OF AMERICA": "BAC",
    "COCA COLA CO": "KO",
    "COCA COLA": "KO",
    "COCA COLA COMPANY": "KO",
    "WALT DISNEY CO": "DIS",
    "DISNEY WALT CO": "DIS",
    "DISNEY": "DIS",
    "WALT DISNEY": "DIS",
    "MERCK & CO": "MRK",
    "PROCTOR & GAMBLE CO": "PG",
    "PROCTER & GAMBLE": "PG",
    "PROCTER AND GAMBLE": "PG",
    "NORDSTROM INC": "JWN",
    "NORDSTROM": "JWN",
    "WELLS FARGO & CO": "WFC",
    "WELLS FARGO": "WFC",
    "WELLS FARGO & COMPANY": "WFC",
    "MICROSOFT CORP": "MSFT",
    "MICROSOFT": "MSFT",
    "MICROSOFT CORPORATION": "MSFT",
    "APPLE": "AAPL",
    "APPLE INC": "AAPL",
    "AMAZON": "AMZN",
    "AMAZON COM INC": "AMZN",
    "AMAZON COM": "AMZN",
    "AMAZON COM INC COM": "AMZN",
    "GOOGLE": "GOOGL",
    "GOOGLE INC": "GOOGL",
    "GOOGLE INC COM": "GOOGL",
    "ALPHABET": "GOOGL",
    "ALPHABET INC": "GOOGL",
    "FACEBOOK": "META",
    "META": "META",
    "TESLA": "TSLA",
    "TESLA INC": "TSLA",
    "NETFLIX": "NFLX",
    "NETFLIX INC": "NFLX",
    "COSTCO": "COST",
    "COSTCO WHOLESALE": "COST",
    "COSTCO WHOLESALE CORP": "COST",
    "BERKSHIRE HATHAWAY": "BRK-B",
    "BERKSHIRE HATHAWAY INC": "BRK-B",
    "BERKSHIRE HATHAWAY INC CL B": "BRK-B",
    "JOHNSON & JOHNSON": "JNJ",
    "JOHNSON AND JOHNSON": "JNJ",
    "J&J": "JNJ",
    "PFIZER": "PFE",
    "PFIZER INC": "PFE",
    "MERCK": "MRK",
    "NVIDIA": "NVDA",
    "NVIDIA CORP": "NVDA",
    "INTEL": "INTC",
    "INTEL CORP": "INTC",
    "CISCO": "CSCO",
    "CISCO SYSTEMS": "CSCO",
    "CISCO SYSTEMS INC": "CSCO",
    "AMGEN": "AMGN",
    "AMGEN INC": "AMGN",
    "EBAY": "EBAY",
    "PAYPAL": "PYPL",
    "PAYPAL HOLDINGS": "PYPL",
    "STARBUCKS": "SBUX",
    "STARBUCKS CORP": "SBUX",
    "WALMART": "WMT",
    "WALMART INC": "WMT",
    "VERIZON": "VZ",
    "VERIZON COMMUNICATIONS": "VZ",
    "ABBOTT LABS": "ABT",
    "ABBOT": "ABT",
    "ABBVIE": "ABBV",
    "BRISTOL MEYERS": "BMY",
    "BRISTOL MEYERS SQUIBB": "BMY",
    "CITIGROUP": "C",
    "CITIGROUP INC": "C",
    "DUKE ENERGY": "DUK",
    "DUKE ENERGY CORP": "DUK",
    "NESTLE S A ADR": "NSRGY",
    "NESTLE ADR": "NSRGY",
    "NOVARTIS A G SPONSORED ADR ADR": "NVS",
    "NOVARTIS ADR": "NVS",
    "ROYAL DUTCH SHELL PLC ADR": "SHEL",
    "ROYAL DUTCH SHELL ADR": "SHEL",
    "SHELL PLC ADR": "SHEL",
    "TENCENT HLDGS LTD ADR ADR": "TCEHY",
    "TENCENT ADR": "TCEHY",
    "TENCENT": "TCEHY",
    "BNP PARIBAS ADR": "BNPQY",
    "BNP PARIBAS": "BNPQY",
    "NOVO NORDISK A S F SPONSORED ADR 1 ADR REPS 1": "NVO",
    "NOVO NORDISK ADR": "NVO",
    "GRUPO TELEVISA S A BSPON ADR REP ORD": "TV",
    "GRUPO TELEVISA ADR": "TV",
    "TAIWAN SEMI CONDUCTOR": "TSM",
    "TAIWAN SEMICONDUCTOR": "TSM",
    "TAIWAN SEMI- CONDUCTOR": "TSM",
    "INFENEON TECHNOLOGIES AG": "IFNNY",
    "BRITISH AMERICAN TOBACCO": "BTI",
    "BARRICK GOLD CORP": "GOLD",
    "BARRICK GOLD": "GOLD",
    "SQUARE INC": "SQ",
    "SQUARE": "SQ",
    "BLOCK INC": "SQ",
    "SQUARESPACE INC": "SQSP",
    "CHIPOTLE": "CMG",
    "CHIPOTLE MEXICAN GRILL": "CMG",
    "PROCTOR GAMBLE": "PG",
    "PHILLIP MORRIS INTERNATIONAL": "PM",
    "PROCTOR & GAMBLE": "PG",
    "UNITED TECHNOLOGIES": "RTX",
    "UNITED TECHNOLOGIES CORPORATIONS": "RTX",
    "KRAFT FOODS": "KHC",
    "KRAFT FOODS INC": "KHC",
    "KRAFT HEINZ": "KHC",
    "GRAYSCALE ETHEREUM TR ETH COM": "ETHE",
    "SHW": "SHW",
    "MCDONALDS": "MCD",
    "MCDONALD'S": "MCD",
    "MCDONALDS CORP": "MCD",
    "VISA": "V",
    "VISA INC": "V",
    "MASTERCARD": "MA",
    "META PLATFORMS": "META",
    "BERKSHIRE HALTHAWAY": "BRK-B",
    "HUMA": "HUMA",
    "WORKHORSE GROUP INC": "WKHS",
    "LI AUTO": "LI",
    "XPENG INC": "XPEV",
    "NIO INC": "NIO",
    "BABA": "BABA",
    "LULU LEMON": "LULU",
    "PALO ALTO NETWORKS": "PANW",
    "UPST": "UPST",
    "CARRIER GLOBAL CORP": "CARR",
    "CARRIER": "CARR",
    "ABBVIE INC": "ABBV",
    "L3 HARRIS TECHNOLOGIES": "LHX",
    "EDWARDS LIFESCIENCES CORP": "EW",
    "EW": "EW",
    "SWK": "SWK",
}


def _looks_reportable(name: str) -> bool:
    """Reject extraction residue that no real entity plausibly authored."""
    if not isinstance(name, str):
        return False
    n = name.strip()
    if len(n) < 4 or len(n) > 120:
        return False
    low = n.lower()
    if not re.search(r"[a-z0-9]", low):
        return False
    junk = ("n/a", "none", "other", "check here", "if 'other'", "https://",
            "wayback", "dba freeconferencecall",
            )
    if any(j in low for j in junk):
        return False
    return True


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Index building (pure, testable without the store)
# ---------------------------------------------------------------------------

def build_index(sec: "pd.DataFrame"):
    """
    Build the three lookup structures from a securities-style frame with
    `company_name` + `symbol` (+ index-membership flags for require_listed).
    Returns (exact -> symbol, significant-tokens -> tuple(symbols), meta).
    """
    exact: dict[str, str] = {}
    tokens: dict[tuple[str, ...], tuple[str, ...]] = {}
    meta: dict[str, dict] = {}
    sec = sec[sec["company_name"].notna() & sec["symbol"].notna()]
    for _, row in sec.iterrows():
        sym = str(row["symbol"])
        name = normalize(str(row["company_name"]))
        if not name:
            continue
        meta[sym] = {
            "listed": bool(row.get("is_russell3000"))
                      or bool(row.get("is_russell2000"))
                      or bool(row.get("is_wilshire5000"))
                      or bool(row.get("is_sp500")),
            "has_flags": any(pd.notna(row.get(c)) for c in
                             ("is_sp500", "is_russell3000",
                              "is_russell2000", "is_wilshire5000")),
            "exchange": row.get("exchange"),
        }
        exact.setdefault(name, sym)
        toks = _significant_tokens(name)
        if toks:
            prev = tokens.get(toks)
            if prev is None:
                tokens[toks] = (sym,)
            elif sym not in prev:
                tokens[toks] = prev + (sym,)
    return exact, tokens, meta


class NameResolver:
    """Name -> symbol resolver backed by the `securities` reference table."""

    def __init__(self):
        self._exact: dict[str, str] = {}
        self._tokens: dict[tuple[str, ...], tuple[str, ...]] = {}
        self._meta: dict[str, dict] = {}
        self._loaded = False

    @classmethod
    def from_frame(cls, sec: "pd.DataFrame") -> "NameResolver":
        """Build from an injected securities-like frame (unit tests)."""
        r = cls()
        r._exact, r._tokens, r._meta = build_index(sec)
        r._loaded = True
        return r

    def load(self) -> "NameResolver":
        if self._loaded:
            return self
        sec = q.load("securities").drop_duplicates("company_name")
        self._exact, self._tokens, self._meta = build_index(sec)
        self._loaded = True
        return self

    def resolve(self, name: str, require_listed: bool = False):
        """
        Resolve one reported name.

        require_listed: for the lower-trust token tier, only accept a
        candidate that carries index-membership flags (rejects junk legacy
        listings that share a name with a real ticker).
        """
        if not _looks_reportable(name):
            return None
        n = normalize(name)
        if not n:
            return None
        if n in CANONICAL:
            sym = CANONICAL[n]
            return {"symbol": sym, "method": "canonical", "name": name}
        if n in self._exact:
            sym = self._exact[n]
            return {"symbol": sym, "method": "exact", "name": name}
        toks = _significant_tokens(name)
        if len(toks) >= 2:
            cands = self._tokens.get(toks, ())
            if len(cands) == 1:
                sym = cands[0]
                if not (require_listed and not self._meta.get(sym, {}).get("listed")):
                    return {"symbol": sym, "method": "tokens", "name": name}
        return None

    def resolve_frame(self, names: "pd.Series", require_listed: bool = False):
        """Map a Series of entity names to a DataFrame with symbol/method."""
        self.load()
        rows = [self.resolve(n, require_listed=require_listed) for n in names]
        return pd.DataFrame({
            "entity": list(names),
            "symbol": [r["symbol"] if r else None for r in rows],
            "method": [r["method"] if r else None for r in rows],
        })

    def unresolved(self, names: "pd.Series"):
        """Entities that failed resolution (drop to `.unique()`), for review."""
        fr = self.resolve_frame(names)
        return fr.loc[fr["symbol"].isna(), "entity"].astype(str)

    def coverage(self, names: "pd.Series"):
        fr = self.resolve_frame(names)
        ok = fr["symbol"].notna().sum()
        return {
            "rows": int(len(fr)),
            "resolved_rows": int(ok),
            "pct_rows": round(100 * ok / len(fr), 1) if len(fr) else 0.0,
            "distinct": int(fr["entity"].nunique()),
            "resolved_distinct": int(fr.loc[fr["symbol"].notna(), "entity"].nunique()),
            "methods": fr["method"].value_counts().to_dict(),
        }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--names", nargs="+", help="names to resolve")
    ap.add_argument("--require-listed", action="store_true",
                    help="reject token-tier candidates with no index flags")
    args = ap.parse_args()

    r = NameResolver().load()
    if not args.names:
        print("usage: python name_to_ticker.py --names <name> [...]")
        return
    for name in args.names:
        res = r.resolve(name, require_listed=args.require_listed)
        if res:
            print(f"{name!r:35} -> {res['symbol']}  ({res['method']})")
        else:
            print(f"{name!r:35} -> UNRESOLVED")


if __name__ == "__main__":
    main()