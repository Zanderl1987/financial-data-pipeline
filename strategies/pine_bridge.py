"""
pine_bridge.py -- Pine Script Strategy Translation & Execution Bridge.

Converts open-source Pine Script strategies (from storage/tv_scripts/)
into executable evaluation.contracts.TradeRule and Signal objects.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Callable, Dict, Any, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evaluation.contracts import TradeRule, Signal


def _crossed_up(s1: pd.Series, s2: "pd.Series | float") -> pd.Series:
    """True on bars where s1 crosses above s2."""
    val2 = s2 if isinstance(s2, pd.Series) else pd.Series(s2, index=s1.index)
    return (s1 > val2) & (s1.shift(1) <= val2.shift(1))


def _crossed_down(s1: pd.Series, s2: "pd.Series | float") -> pd.Series:
    """True on bars where s1 crosses below s2."""
    val2 = s2 if isinstance(s2, pd.Series) else pd.Series(s2, index=s1.index)
    return (s1 < val2) & (s1.shift(1) >= val2.shift(1))


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = (delta.where(delta > 0, 0.0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=period).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def build_ema_cross_rule(fast: int = 9, slow: int = 21, side: str = "long") -> TradeRule:
    """Moving Average Crossover (Pine ta.crossover / ta.crossunder)."""
    def _entries(df: pd.DataFrame) -> pd.Series:
        px = df["close"]
        f_ema = px.ewm(span=fast, adjust=False).mean()
        s_ema = px.ewm(span=slow, adjust=False).mean()
        return _crossed_up(f_ema, s_ema)

    def _exits(df: pd.DataFrame) -> pd.Series:
        px = df["close"]
        f_ema = px.ewm(span=fast, adjust=False).mean()
        s_ema = px.ewm(span=slow, adjust=False).mean()
        return _crossed_down(f_ema, s_ema)

    return TradeRule(
        name=f"pine_ema_cross_{fast}_{slow}",
        entries=_entries,
        exits=_exits,
        side=side,
    )


def build_rsi_threshold_rule(rsi_period: int = 14, buy_level: float = 30.0,
                             sell_level: float = 70.0) -> TradeRule:
    """RSI Mean Reversion Rule (buy oversold crossed up, sell overbought crossed down)."""
    def _entries(df: pd.DataFrame) -> pd.Series:
        rsi = compute_rsi(df["close"], rsi_period)
        return _crossed_up(rsi, buy_level)

    def _exits(df: pd.DataFrame) -> pd.Series:
        rsi = compute_rsi(df["close"], rsi_period)
        return _crossed_down(rsi, sell_level)

    return TradeRule(
        name=f"pine_rsi_{rsi_period}_{buy_level}_{sell_level}",
        entries=_entries,
        exits=_exits,
        side="long",
    )


def build_ut_bot_rule(key_value: float = 1.0, atr_period: int = 10) -> TradeRule:
    """UT Bot Scalper (ATR trailing stop trailing sensitivity)."""
    def _entries(df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        atr = compute_atr(df, atr_period)
        n_loss = key_value * atr
        trail = close.copy()
        for i in range(1, len(df)):
            prev = trail.iloc[i-1]
            curr_c = close.iloc[i]
            loss = n_loss.iloc[i] if pd.notna(n_loss.iloc[i]) else 0.0
            if curr_c > prev and close.iloc[i-1] > prev:
                trail.iloc[i] = max(prev, curr_c - loss)
            elif curr_c < prev and close.iloc[i-1] < prev:
                trail.iloc[i] = min(prev, curr_c + loss)
            elif curr_c > prev:
                trail.iloc[i] = curr_c - loss
            else:
                trail.iloc[i] = curr_c + loss
        return _crossed_up(close, trail)

    def _exits(df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        atr = compute_atr(df, atr_period)
        n_loss = key_value * atr
        trail = close.copy()
        for i in range(1, len(df)):
            prev = trail.iloc[i-1]
            curr_c = close.iloc[i]
            loss = n_loss.iloc[i] if pd.notna(n_loss.iloc[i]) else 0.0
            if curr_c > prev and close.iloc[i-1] > prev:
                trail.iloc[i] = max(prev, curr_c - loss)
            elif curr_c < prev and close.iloc[i-1] < prev:
                trail.iloc[i] = min(prev, curr_c + loss)
            elif curr_c > prev:
                trail.iloc[i] = curr_c - loss
            else:
                trail.iloc[i] = curr_c + loss
        return _crossed_down(close, trail)

    return TradeRule(
        name=f"pine_ut_bot_{key_value}_{atr_period}",
        entries=_entries,
        exits=_exits,
        side="long",
    )


class UnrecognizedStrategyError(RuntimeError):
    """Raised when a script's source doesn't match a single recognizable
    pattern (pure RSI, pure MA-crossover, or UT-Bot ATR-trail) this bridge
    can translate.

    Before 2026-08-13 this bridge silently substituted a generic default
    template (usually `pine_ema_cross_12_26`) for ANY unmatched script,
    keyed off a slug-substring guess rather than the actual source. That
    caused genuinely different strategies (donchian breakout, fair-value-gap/
    break-of-structure, opening-range breakout, multi-indicator hybrids...)
    to silently collapse onto 1-2 generic templates and produce byte-identical
    simulated trade results -- discovered when Stage 3 batch results showed
    exact duplicate statistics across unrelated strategies. A script this
    bridge cannot confidently classify must be excluded from Stage 3 (or
    given a hand-written port in strategies/ports/) rather than tested under
    the wrong rule.
    """


_INPUT_RE = re.compile(
    r'(\w+)\s*=\s*input\.(int|float)\(\s*([-\d.]+)\s*,\s*(?:title\s*=\s*)?"([^"]*)"'
)

# --- content-based classification ----------------------------------------
# Matched against the actual Pine source (comments/strings stripped), not the
# script's filename/slug -- a script named "boosted_moving_average" or
# "ema_fib_confluence" is not necessarily a simple EMA crossover underneath.

_LINE_COMMENT = re.compile(r"//[^\n]*")
_STRING_LIT = re.compile(r"""(['"])(?:\\.|(?!\1).)*\1""")

_RSI_CALL = re.compile(r"\bta\.rsi\s*\(")
_EMA_CALL = re.compile(r"\bta\.ema\s*\(")
_SMA_CALL = re.compile(r"\bta\.sma\s*\(")
_WMA_CALL = re.compile(r"\bta\.wma\s*\(")
_ATR_CALL = re.compile(r"\bta\.atr\s*\(")
_VWAP_CALL = re.compile(r"\bta\.vwap\b")
_CROSSOVER_CALL = re.compile(r"\bta\.crossover\s*\(")
_CROSSUNDER_CALL = re.compile(r"\bta\.crossunder\s*\(")
_UT_BOT_SIGNATURE = re.compile(r"\bxATRTrailingStop\b|\bnLoss\b")


def _strip_noise(src: str) -> str:
    return _LINE_COMMENT.sub("", _STRING_LIT.sub('""', src))


def _classify(code: str) -> str:
    """Return "ut_bot", "rsi", "ma_cross", or "unrecognized".

    Deliberately conservative: RSI and MA-crossover are only accepted when
    they are the ONLY indicator family driving the script (no ATR/VWAP mixed
    in, no other MA/RSI type present) AND a real ta.crossover/ta.crossunder
    pair is present -- a script combining several indicators needs a real
    hand-written port (strategies/ports/), not a guess from this bridge.
    """
    if _UT_BOT_SIGNATURE.search(code):
        return "ut_bot"

    n_rsi = len(_RSI_CALL.findall(code))
    n_ma = (len(_EMA_CALL.findall(code)) + len(_SMA_CALL.findall(code))
            + len(_WMA_CALL.findall(code)))
    n_atr = len(_ATR_CALL.findall(code))
    n_vwap = len(_VWAP_CALL.findall(code))
    has_cross = bool(_CROSSOVER_CALL.search(code)) and bool(_CROSSUNDER_CALL.search(code))

    if n_rsi >= 1 and n_ma == 0 and n_atr == 0 and n_vwap == 0 and has_cross:
        return "rsi"
    if n_ma >= 2 and n_rsi == 0 and n_atr == 0 and n_vwap == 0 and has_cross:
        return "ma_cross"
    return "unrecognized"


def parse_pine_inputs(pine_source: str) -> Dict[str, Dict[str, Any]]:
    """Extract input.int()/input.float() declarations from Pine source.

    Returns {var_name: {"type": "int"|"float", "value": number, "title": str}},
    covering both positional (`input.int(14, "RSI Length")`) and keyword
    (`input.int(14, title="RSI Length")`) title styles.
    """
    inputs: Dict[str, Dict[str, Any]] = {}
    for var, kind, value, title in _INPUT_RE.findall(pine_source):
        inputs[var] = {
            "type": kind,
            "value": int(value) if kind == "int" else float(value),
            "title": title,
        }
    return inputs


def _match_input(inputs: Dict[str, Dict[str, Any]], patterns: List[str]) -> Optional[float]:
    """First input whose var name + title (lowercased) matches every pattern."""
    for var, info in inputs.items():
        haystack = f"{var} {info['title']}".lower()
        if all(re.search(p, haystack) for p in patterns):
            return info["value"]
    return None


def load_pine_script_rule(script_slug: str, tv_scripts_dir: str = "storage/tv_scripts") -> TradeRule:
    """
    Inspect the Pine Script source for `script_slug` and, if it matches a
    single recognizable pattern (pure RSI, pure MA-crossover, or UT-Bot
    ATR-trail), return the matching TradeRule using the script's own
    input.int()/input.float() values where they can be found (template
    default only for a parameter the source doesn't declare).

    Raises UnrecognizedStrategyError if the script's source doesn't match any
    of these patterns cleanly (mixes multiple indicator families, or uses
    none of them) -- this bridge does not guess in that case; see
    UnrecognizedStrategyError's docstring for why.
    """
    path = os.path.join(tv_scripts_dir, f"{script_slug}.pine")
    if not os.path.isfile(path):
        raise UnrecognizedStrategyError(
            f"{script_slug}: no .pine source file at {path}, nothing to classify"
        )
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()

    inputs = parse_pine_inputs(source)
    code = _strip_noise(source)
    kind = _classify(code)

    if kind == "ut_bot":
        key_value = _match_input(inputs, [r"key"])
        atr_period = _match_input(inputs, [r"atr", r"len|period"])
        return build_ut_bot_rule(
            key_value=key_value if key_value is not None else 1.0,
            atr_period=int(atr_period) if atr_period is not None else 10,
        )
    elif kind == "rsi":
        rsi_period = _match_input(inputs, [r"rsi", r"len|period"])
        buy_level = _match_input(inputs, [r"buy|oversold"])
        sell_level = _match_input(inputs, [r"sell|overbought"])
        return build_rsi_threshold_rule(
            rsi_period=int(rsi_period) if rsi_period is not None else 14,
            buy_level=buy_level if buy_level is not None else 30.0,
            sell_level=sell_level if sell_level is not None else 70.0,
        )
    elif kind == "ma_cross":
        fast = _match_input(inputs, [r"fast"])
        slow = _match_input(inputs, [r"slow"])
        return build_ema_cross_rule(
            fast=int(fast) if fast is not None else 9,
            slow=int(slow) if slow is not None else 21,
            side="long",
        )
    else:
        raise UnrecognizedStrategyError(
            f"{script_slug}: source doesn't match a single recognizable "
            f"pattern (pure RSI, pure MA-crossover, or UT-Bot ATR-trail) -- "
            f"mixes multiple indicator families or uses none of them. Needs "
            f"a hand-written port in strategies/ports/, not this generic bridge."
        )
