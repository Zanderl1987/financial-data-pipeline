"""
Tests for strategies/pine_bridge.py -- the generic (unverified) Stage 2
Pine-to-TradeRule translation bridge. Distinct from tests/test_tv_ports.py,
which covers the separate hand-verified strategies/ports/ system.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.pine_bridge import (  # noqa: E402
    UnrecognizedStrategyError,
    _match_input,
    load_pine_script_rule,
    parse_pine_inputs,
)

TV_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "storage", "tv_scripts")


# ---------------------------------------------------------- parse_pine_inputs

def test_parse_pine_inputs_positional_title():
    src = 'rsiLength = input.int(14, "RSI Length")\n' \
          'buyLevel  = input.float(30, "Buy Level")\n'
    inputs = parse_pine_inputs(src)
    assert inputs["rsiLength"] == {"type": "int", "value": 14, "title": "RSI Length"}
    assert inputs["buyLevel"] == {"type": "float", "value": 30.0, "title": "Buy Level"}


def test_parse_pine_inputs_keyword_title_with_extra_kwargs():
    src = 'keyvalue = input.float(2.0, title="Key Value (Sensitivity)", step=0.1, group="UT Bot Setup")\n'
    inputs = parse_pine_inputs(src)
    assert inputs["keyvalue"] == {"type": "float", "value": 2.0, "title": "Key Value (Sensitivity)"}


def test_parse_pine_inputs_ignores_non_numeric_inputs():
    """input.bool/input.source/input.timeframe etc. aren't matched -- only
    int/float, which is what the rule builders actually consume."""
    src = 'htfFilterOn = input.bool(false, title="Enable HTF Filter")\n' \
          'fastLen = input.int(9, "Fast Length")\n'
    inputs = parse_pine_inputs(src)
    assert "htfFilterOn" not in inputs
    assert inputs["fastLen"]["value"] == 9


def test_parse_pine_inputs_empty_source():
    assert parse_pine_inputs("") == {}
    assert parse_pine_inputs("// just a comment, no inputs at all") == {}


# ------------------------------------------------------------------ _match_input

def test_match_input_requires_all_patterns():
    inputs = {
        "rsiLength": {"type": "int", "value": 14, "title": "RSI Length"},
        "buyLevel": {"type": "float", "value": 30.0, "title": "Buy Level"},
    }
    assert _match_input(inputs, [r"rsi", r"len|period"]) == 14
    assert _match_input(inputs, [r"buy|oversold"]) == 30.0
    assert _match_input(inputs, [r"sell|overbought"]) is None


def test_match_input_matches_against_var_name_when_title_is_generic():
    inputs = {"fastLen": {"type": "int", "value": 9, "title": "Length"}}
    assert _match_input(inputs, [r"fast"]) == 9


# ------------------------------------------------------------ load_pine_script_rule

def test_load_rsi_recovery_uses_real_script_params():
    """RgAMIpig-RSI-30-65-Recovery-Strategy declares rsiLength=14, buyLevel=30,
    sellLevel=65 -- the historical bug (fixed 2026-08-12) hardcoded
    sell_level=70.0 regardless of source, silently substituting the wrong
    threshold for any script whose author picked non-default parameters."""
    rule = load_pine_script_rule("rgamipig_rsi_30_65_recovery_strategy",
                                 tv_scripts_dir=TV_SCRIPTS_DIR)
    assert rule.name == "pine_rsi_14_30.0_65.0"


def test_load_ut_bot_scalper_uses_real_script_params():
    """rabiah6x_ut_bot_scalper declares keyvalue=2.0 (title "Key Value
    (Sensitivity)") and atrperiod=10 -- template default would be key=1.0."""
    rule = load_pine_script_rule("rabiah6x_ut_bot_scalper", tv_scripts_dir=TV_SCRIPTS_DIR)
    assert rule.name == "pine_ut_bot_2.0_10"


def test_load_raises_when_file_missing():
    """A missing source file has nothing to classify -- raise, don't guess
    from the slug name alone."""
    with pytest.raises(UnrecognizedStrategyError):
        load_pine_script_rule("no_such_script_xyz_rsi", tv_scripts_dir=TV_SCRIPTS_DIR)


def test_load_raises_for_multi_indicator_script():
    """A script mixing several indicator families (here: rsi + atr + sma) must
    raise rather than silently collapse onto a generic default template --
    see UnrecognizedStrategyError's docstring for the 2026-08-13 bug this
    guards against (byte-identical simulated results for unrelated
    strategies)."""
    with pytest.raises(UnrecognizedStrategyError):
        load_pine_script_rule("mrr_mean_reversion_range", tv_scripts_dir=TV_SCRIPTS_DIR)


@pytest.mark.parametrize("pine_path", [
    os.path.join(TV_SCRIPTS_DIR, f) for f in os.listdir(TV_SCRIPTS_DIR) if f.endswith(".pine")
])
def test_all_collected_scripts_classify_or_raise_cleanly(pine_path):
    """Every currently-collected .pine file must either produce a valid
    TradeRule or raise UnrecognizedStrategyError -- guards against a
    regex/branch regression raising some OTHER exception (a real bug) or
    silently mis-classifying (the 2026-08-13 bug this bridge was rewritten
    to prevent)."""
    slug = os.path.splitext(os.path.basename(pine_path))[0]
    try:
        rule = load_pine_script_rule(slug, tv_scripts_dir=TV_SCRIPTS_DIR)
    except UnrecognizedStrategyError:
        return
    assert rule.name
    assert callable(rule.entries)
    assert callable(rule.exits)


# ------------------------------------------------------------ evaluation.adapters wiring

def test_from_pine_script_delegates_to_load_pine_script_rule():
    """evaluation.adapters.from_pine_script() is the evaluate.py-facing entry
    point onto this module -- previously had zero call sites and zero
    coverage (see work-notes/financial-data-pipeline/SESSION_NOTES_2026-08-12_tv-catalog.md session 7)."""
    from evaluation.adapters import from_pine_script

    rule = from_pine_script("rgamipig_rsi_30_65_recovery_strategy")
    assert rule.name == "pine_rsi_14_30.0_65.0"
