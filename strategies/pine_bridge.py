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


_INPUT_RE = re.compile(
    r'(\w+)\s*=\s*input\.(int|float)\(\s*([-\d.]+)\s*,\s*(?:title\s*=\s*)?"([^"]*)"'
)


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


def _load_pine_inputs(script_slug: str, tv_scripts_dir: str) -> Dict[str, Dict[str, Any]]:
    path = os.path.join(tv_scripts_dir, f"{script_slug}.pine")
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return parse_pine_inputs(f.read())


def load_pine_script_rule(script_slug: str, tv_scripts_dir: str = "storage/tv_scripts") -> TradeRule:
    """
    Inspect the Pine Script file for `script_slug` and return the matching
    TradeRule, using each script's own input.int()/input.float() defaults
    where they can be found -- falling back to the template default only for
    a parameter the source doesn't declare or that can't be located.
    """
    slug_lower = script_slug.lower()
    inputs = _load_pine_inputs(script_slug, tv_scripts_dir)

    if "ut_bot" in slug_lower or "utbot" in slug_lower:
        key_value = _match_input(inputs, [r"key"])
        atr_period = _match_input(inputs, [r"atr", r"len|period"])
        return build_ut_bot_rule(
            key_value=key_value if key_value is not None else 1.0,
            atr_period=int(atr_period) if atr_period is not None else 10,
        )
    elif "rsi" in slug_lower:
        rsi_period = _match_input(inputs, [r"rsi", r"len|period"])
        buy_level = _match_input(inputs, [r"buy|oversold"])
        sell_level = _match_input(inputs, [r"sell|overbought"])
        return build_rsi_threshold_rule(
            rsi_period=int(rsi_period) if rsi_period is not None else 14,
            buy_level=buy_level if buy_level is not None else 30.0,
            sell_level=sell_level if sell_level is not None else 70.0,
        )
    elif "ema" in slug_lower or "moving_average" in slug_lower or "tunnel" in slug_lower:
        fast = _match_input(inputs, [r"fast"])
        slow = _match_input(inputs, [r"slow"])
        return build_ema_cross_rule(
            fast=int(fast) if fast is not None else 9,
            slow=int(slow) if slow is not None else 21,
            side="long",
        )
    else:
        fast = _match_input(inputs, [r"fast"])
        slow = _match_input(inputs, [r"slow"])
        return build_ema_cross_rule(
            fast=int(fast) if fast is not None else 12,
            slow=int(slow) if slow is not None else 26,
            side="long",
        )
