"""
strategies/screen.py -- Stage 1 source screen for collected Pine scripts.

Applies the hard exclusions pre-registered in
experiments/2026-08-11_tv-strategy-catalog-preregistration.md to raw Pine source,
BEFORE any translation effort is spent. A script hitting any exclusion is recorded
in the catalog with its `excluded_reason` and never ported.

The highest-value rule here is the repaint screen. A script using
`barmerge.lookahead_on`, or pulling a higher timeframe without waiting for that bar
to close, sees the future. Such scripts backtest beautifully on TradingView and
would enter the catalog as its top performers if they were not removed first.

Error direction
---------------
These are deliberately conservative heuristics on text, not a Pine parser. They are
tuned to over-exclude: wrongly dropping a sound script costs one sample slot, while
wrongly admitting a repainting script contaminates the campaign's headline result.
`needs_review` marks the calls where a human should confirm before the exclusion is
treated as final.

Outputs
-------
`ScreenResult` per script; `screen_source()` is pure (no I/O).

Usage
-----
    from strategies.screen import screen_source

    res = screen_source(pine_text, script_name="Some Indicator")
    if res.admitted:
        ...  # proceed to Stage 2 translation
    else:
        print(res.excluded_reason)

    # CLI over a directory of .pine files
    python strategies/screen.py storage/tv_scripts/
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional

EXCLUSION_CODES = (
    "lookahead",          # explicit barmerge.lookahead_on
    "unconfirmed_htf",    # higher-timeframe pull without confirmation
    "intrabar_recalc",    # calc_on_every_tick / calc_on_order_fills
    "no_exit",            # no exit condition -- cannot form a TradeRule
    "no_entry",           # visualization only
    "external_input",     # reads data this repo cannot supply from OHLCV
)

# --- comment / string stripping ------------------------------------------------
# Pine uses // for line comments. Strings can be single- or double-quoted. Both are
# removed before matching so that a script merely *discussing* lookahead in a
# comment, or naming a plot "lookahead", is not excluded for it.
_LINE_COMMENT = re.compile(r"//[^\n]*")
_STRING = re.compile(r"""(['"])(?:\\.|(?!\1).)*\1""")

# --- exclusion patterns --------------------------------------------------------
_LOOKAHEAD = re.compile(r"\block(ahead)?\s*=\s*barmerge\.lookahead_on\b")
_LOOKAHEAD_BARE = re.compile(r"\bbarmerge\.lookahead_on\b")

# request.security( in v5/v6, bare security( in v3/v4
_SECURITY_CALL = re.compile(r"\b(?:request\.)?security\s*\(")

_CALC_EVERY_TICK = re.compile(r"\bcalc_on_every_tick\s*=\s*true\b", re.IGNORECASE)
_CALC_ORDER_FILLS = re.compile(r"\bcalc_on_order_fills\s*=\s*true\b", re.IGNORECASE)

# Entry / exit vocabulary. strategy.* for strategy() scripts; for indicator()
# scripts an "entry" is any alertcondition/plotshape driven by a crossover.
_STRATEGY_ENTRY = re.compile(r"\bstrategy\.(entry|order)\s*\(")
_STRATEGY_EXIT = re.compile(r"\bstrategy\.(close|close_all|exit|cancel)\s*\(")
_SIGNAL_VOCAB = re.compile(
    r"\b(ta\.)?cross(over|under)?\s*\(|\balertcondition\s*\(|\bplotshape\s*\(", re.IGNORECASE
)

# Declarations
_IS_STRATEGY = re.compile(r"^\s*strategy\s*\(", re.MULTILINE)
_IS_INDICATOR = re.compile(r"^\s*(indicator|study)\s*\(", re.MULTILINE)
_IS_LIBRARY = re.compile(r"^\s*library\s*\(", re.MULTILINE)

# Data this repo cannot reproduce from its OHLCV panel.
_EXTERNAL_INPUT = re.compile(
    r"\brequest\.(financial|dividends|splits|earnings|economic|quandl|seed)\s*\(",
)

_INPUT_CALL = re.compile(r"\binput\s*(?:\.\s*\w+)?\s*\(")
_VERSION = re.compile(r"//@version\s*=\s*(\d+)")

# Mechanism-family keyword hints. Order matters: first family whose pattern hits
# two or more distinct keywords wins; ties and misses fall through to "hybrid".
_FAMILY_HINTS = {
    "trend": (r"\bta\.(ema|sma|wma|hma|vwma)\b", r"\bsupertrend\b", r"\badx\b",
              r"\bta\.macd\b", r"\bichimoku\b"),
    "mean_reversion": (r"\bta\.rsi\b", r"\bta\.bb\b", r"\bbollinger\b",
                       r"\bta\.stoch\b", r"\bz_?score\b", r"\bta\.cci\b"),
    "breakout": (r"\bta\.highest\b", r"\bta\.lowest\b", r"\bdonchian\b",
                 r"\bbreakout\b", r"\bpivot(high|low)\b"),
    "volatility": (r"\bta\.atr\b", r"\bta\.stdev\b", r"\bkeltner\b", r"\bta\.tr\b"),
    "volume": (r"\bta\.obv\b", r"\bta\.vwap\b", r"\bvolume\b", r"\bta\.mfi\b",
               r"\bta\.accdist\b"),
}


@dataclass
class ScreenResult:
    """Outcome of the Stage 1 source screen for one script."""
    script_name: str
    admitted: bool
    excluded_reason: Optional[str] = None
    pine_version: Optional[int] = None
    script_kind: str = "unknown"          # strategy / indicator / library / unknown
    mechanism_family: str = "hybrid"
    param_count: int = 0
    needs_review: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def __repr__(self) -> str:
        verdict = "ADMIT" if self.admitted else f"EXCLUDE:{self.excluded_reason}"
        return f"<ScreenResult {self.script_name!r} {verdict} family={self.mechanism_family}>"


def _strip_noise(src: str) -> str:
    """Remove string literals and line comments so matches only see real code."""
    return _LINE_COMMENT.sub("", _STRING.sub('""', src))


def _detect_kind(code: str) -> str:
    if _IS_STRATEGY.search(code):
        return "strategy"
    if _IS_LIBRARY.search(code):
        return "library"
    if _IS_INDICATOR.search(code):
        return "indicator"
    return "unknown"


def _security_guard_status(code: str) -> str:
    """
    Classify how well higher-timeframe pulls are guarded against repainting.

    Returns one of:
      "none"        -- no security call present
      "local"       -- every call is guarded at the call site
      "global_only" -- some call is unguarded locally, but the script references
                       barstate.isconfirmed somewhere, so it may be gated by a
                       surrounding condition this text scan cannot see
      "unguarded"   -- some call is unguarded and no confirmation appears anywhere

    A call is locally guarded when it offsets the expression by one bar (`close[1]`
    or a trailing `[1]` on the call), or gates on `barstate.isconfirmed` inside its
    own argument list. Anything else can deliver a value from a still-forming
    higher-timeframe bar, which is look-ahead on historical data.

    "global_only" is deliberately NOT treated as safe. A lone mention of
    barstate.isconfirmed elsewhere in a script is weak evidence that this
    particular call is gated, and admitting a repainting strategy contaminates the
    campaign's headline result far more than dropping a sound one costs.

    Heuristic: scans the balanced-paren body of each call plus the 8 characters
    that follow it (to catch the `request.security(...)[1]` idiom).
    """
    if not _SECURITY_CALL.search(code):
        return "none"

    has_global_confirm = "barstate.isconfirmed" in code
    for m in _SECURITY_CALL.finditer(code):
        start = m.end() - 1  # at the opening paren
        depth, i = 0, start
        while i < len(code):
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = code[start:i + 1]
        trailer = code[i + 1:i + 9]
        locally_guarded = (
            "[1]" in body
            or trailer.lstrip().startswith("[1]")
            or "barstate.isconfirmed" in body
        )
        if not locally_guarded:
            return "global_only" if has_global_confirm else "unguarded"
    return "local"


def _mechanism_family(code: str) -> str:
    lowered = code.lower()
    best, best_hits = "hybrid", 0
    for family, patterns in _FAMILY_HINTS.items():
        hits = sum(1 for p in patterns if re.search(p, lowered))
        if hits > best_hits:
            best, best_hits = family, hits
    return best if best_hits >= 2 else "hybrid"


def screen_source(src: str, script_name: str = "") -> ScreenResult:
    """
    Apply the pre-registered Stage 1 exclusions to one Pine script.

    Returns a ScreenResult; never raises on malformed input (a script that cannot
    be parsed at all is excluded as `no_entry` with a note, not an exception).
    """
    code = _strip_noise(src or "")

    vm = _VERSION.search(src or "")
    version = int(vm.group(1)) if vm else None
    kind = _detect_kind(code)
    family = _mechanism_family(code)
    params = len(_INPUT_CALL.findall(code))

    res = ScreenResult(
        script_name=script_name,
        admitted=False,
        pine_version=version,
        script_kind=kind,
        mechanism_family=family,
        param_count=params,
    )

    if kind == "library":
        res.excluded_reason = "no_entry"
        res.notes.append("library script: exports functions, defines no strategy")
        return res

    # --- repaint screens (highest value; run first) ---
    if _LOOKAHEAD.search(code) or _LOOKAHEAD_BARE.search(code):
        res.excluded_reason = "lookahead"
        res.notes.append("barmerge.lookahead_on: sees future bars")
        return res

    guard = _security_guard_status(code)
    if guard in ("unguarded", "global_only"):
        res.excluded_reason = "unconfirmed_htf"
        res.notes.append("request.security without [1] offset or barstate.isconfirmed")
        res.needs_review.append("unconfirmed_htf is a text heuristic; confirm by hand")
        if guard == "global_only":
            res.notes.append(
                "script references barstate.isconfirmed elsewhere: the call may be "
                "gated by a surrounding condition -- review before treating as final"
            )
        return res

    if _CALC_EVERY_TICK.search(code) or _CALC_ORDER_FILLS.search(code):
        res.excluded_reason = "intrabar_recalc"
        res.notes.append("recalculates intrabar: backtest cannot be reproduced on bars")
        return res

    if _EXTERNAL_INPUT.search(code):
        res.excluded_reason = "external_input"
        res.notes.append("reads non-OHLCV data this repo cannot supply")
        return res

    # --- tradeability screens ---
    has_entry = bool(_STRATEGY_ENTRY.search(code)) or bool(_SIGNAL_VOCAB.search(code))
    has_exit = bool(_STRATEGY_EXIT.search(code))

    if not has_entry:
        res.excluded_reason = "no_entry"
        res.notes.append("no entry condition: visualization only")
        return res

    if kind == "strategy" and not has_exit:
        res.excluded_reason = "no_exit"
        res.notes.append("strategy() with entries but no close/exit call")
        return res

    if kind != "strategy" and not has_exit:
        # An indicator with crossover vocabulary implies a symmetric exit (exit on
        # the opposite cross). Admissible, but the exit is our inference, not the
        # author's, and that must be visible in the catalog.
        res.needs_review.append(
            "indicator(): exit rule inferred as opposite cross, not author-specified"
        )

    if params > 8:
        res.notes.append(f"high parameter count ({params}): deprioritize per protocol")

    res.admitted = True
    return res


def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 1 Pine source screen")
    ap.add_argument("path", help="a .pine file or a directory of them")
    args = ap.parse_args(argv)

    paths = []
    if os.path.isdir(args.path):
        for root, _dirs, files in os.walk(args.path):
            # `_`-prefixed files are collection bookkeeping (rosters, notes), not scripts
            paths.extend(os.path.join(root, f) for f in sorted(files)
                         if f.endswith(".pine") and not f.startswith("_"))
    else:
        paths = [args.path]

    if not paths:
        print(f"X no .pine files under {args.path}")
        return 1

    admitted = 0
    for p in paths:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        r = screen_source(src, script_name=os.path.basename(p))
        mark = "+" if r.admitted else "-"
        detail = r.mechanism_family if r.admitted else r.excluded_reason
        print(f"{mark} {r.script_name:<45} {detail:<18} params={r.param_count}")
        for n in r.needs_review:
            print(f"    ! {n}")
        for n in r.notes:
            print(f"    - {n}")
        admitted += int(r.admitted)

    print(f"\n>> {admitted}/{len(paths)} admitted to Stage 2")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
