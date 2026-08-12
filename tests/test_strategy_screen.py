"""
Tests for strategies/screen.py -- the Stage 1 Pine source screen.

The repaint screens carry the most weight in the campaign, so they get the most
coverage here, including the false-positive cases (a script that merely mentions
lookahead in a comment, a properly-guarded higher-timeframe pull).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.screen import screen_source  # noqa: E402


def _strategy(body: str, version: int = 5) -> str:
    return (
        f"//@version={version}\n"
        'strategy("T", overlay=true)\n'
        f"{body}\n"
    )


def _indicator(body: str, version: int = 5) -> str:
    return (
        f"//@version={version}\n"
        'indicator("T", overlay=true)\n'
        f"{body}\n"
    )


_ENTRY_EXIT = (
    "fast = ta.ema(close, 10)\n"
    "slow = ta.ema(close, 50)\n"
    "if ta.crossover(fast, slow)\n"
    '    strategy.entry("L", strategy.long)\n'
    "if ta.crossunder(fast, slow)\n"
    '    strategy.close("L")\n'
)


class TestAdmission:
    def test_clean_strategy_is_admitted(self):
        r = screen_source(_strategy(_ENTRY_EXIT), "clean")
        assert r.admitted
        assert r.excluded_reason is None
        assert r.script_kind == "strategy"
        assert r.pine_version == 5

    def test_indicator_with_crossover_admitted_but_flagged(self):
        src = _indicator(
            "fast = ta.ema(close, 10)\n"
            "slow = ta.ema(close, 50)\n"
            'alertcondition(ta.crossover(fast, slow), "buy")\n'
        )
        r = screen_source(src, "ind")
        assert r.admitted
        assert any("inferred" in n for n in r.needs_review)

    def test_param_count_counts_input_variants(self):
        src = _strategy(
            "a = input.int(14, 'len')\n"
            "b = input.float(2.0, 'mult')\n"
            "c = input(true, 'flag')\n" + _ENTRY_EXIT
        )
        r = screen_source(src, "params")
        assert r.param_count == 3
        assert r.admitted


class TestRepaintScreens:
    def test_lookahead_on_is_excluded(self):
        src = _strategy(
            "htf = request.security(syminfo.tickerid, 'D', close, "
            "lookahead=barmerge.lookahead_on)\n" + _ENTRY_EXIT
        )
        r = screen_source(src, "repaint")
        assert not r.admitted
        assert r.excluded_reason == "lookahead"

    def test_lookahead_in_comment_does_not_exclude(self):
        src = _strategy("// avoids barmerge.lookahead_on deliberately\n" + _ENTRY_EXIT)
        r = screen_source(src, "comment")
        assert r.admitted

    def test_lookahead_in_string_does_not_exclude(self):
        src = _strategy('note = "barmerge.lookahead_on"\n' + _ENTRY_EXIT)
        r = screen_source(src, "string")
        assert r.admitted

    def test_unguarded_security_is_excluded(self):
        src = _strategy(
            "htf = request.security(syminfo.tickerid, 'D', close)\n" + _ENTRY_EXIT
        )
        r = screen_source(src, "htf")
        assert not r.admitted
        assert r.excluded_reason == "unconfirmed_htf"
        assert r.needs_review, "heuristic exclusions must be marked for review"

    def test_security_offset_by_one_bar_is_admitted(self):
        src = _strategy(
            "htf = request.security(syminfo.tickerid, 'D', close[1])\n" + _ENTRY_EXIT
        )
        r = screen_source(src, "htf_guarded")
        assert r.admitted

    def test_security_with_trailing_offset_is_admitted(self):
        src = _strategy(
            "htf = request.security(syminfo.tickerid, 'D', close)[1]\n" + _ENTRY_EXIT
        )
        r = screen_source(src, "htf_trailing")
        assert r.admitted

    def test_call_site_isconfirmed_is_admitted(self):
        src = _strategy(
            "htf = request.security(syminfo.tickerid, 'D', "
            "barstate.isconfirmed ? close : close[1])\n" + _ENTRY_EXIT
        )
        r = screen_source(src, "confirmed_local")
        assert r.admitted

    def test_distant_isconfirmed_excludes_but_flags_for_review(self):
        """
        A lone barstate.isconfirmed elsewhere in the file is weak evidence that
        THIS call is gated. The screen must still exclude (admitting a repainting
        strategy is the costlier error) while marking it for human review.
        """
        src = _strategy(
            "ok = barstate.isconfirmed\n"
            "htf = request.security(syminfo.tickerid, 'D', close)\n" + _ENTRY_EXIT
        )
        r = screen_source(src, "confirmed_global")
        assert not r.admitted
        assert r.excluded_reason == "unconfirmed_htf"
        assert any("gated by a surrounding condition" in n for n in r.notes)

    def test_legacy_v4_bare_security_is_screened(self):
        src = (
            "//@version=4\n"
            'strategy("T")\n'
            "htf = security(syminfo.tickerid, 'D', close)\n"
            "if crossover(close, sma(close, 20))\n"
            '    strategy.entry("L", strategy.long)\n'
            '    strategy.close("L")\n'
        )
        r = screen_source(src, "v4")
        assert not r.admitted
        assert r.excluded_reason == "unconfirmed_htf"
        assert r.pine_version == 4


class TestOtherExclusions:
    def test_calc_on_every_tick_excluded(self):
        src = (
            "//@version=5\n"
            'strategy("T", calc_on_every_tick=true)\n' + _ENTRY_EXIT
        )
        r = screen_source(src, "tick")
        assert r.excluded_reason == "intrabar_recalc"

    def test_calc_on_order_fills_excluded(self):
        src = (
            "//@version=5\n"
            'strategy("T", calc_on_order_fills=true)\n' + _ENTRY_EXIT
        )
        r = screen_source(src, "fills")
        assert r.excluded_reason == "intrabar_recalc"

    def test_strategy_without_exit_excluded(self):
        src = _strategy(
            "if ta.crossover(close, ta.ema(close, 20))\n"
            '    strategy.entry("L", strategy.long)\n'
        )
        r = screen_source(src, "noexit")
        assert r.excluded_reason == "no_exit"

    def test_visualization_only_excluded(self):
        src = _indicator(
            "band = ta.sma(close, 20)\n"
            "plot(band)\n"
            "bgcolor(color.new(color.blue, 90))\n"
        )
        r = screen_source(src, "viz")
        assert r.excluded_reason == "no_entry"

    def test_external_data_excluded(self):
        src = _strategy(
            "eps = request.financial(syminfo.tickerid, 'EARNINGS_PER_SHARE', 'FQ')\n"
            + _ENTRY_EXIT
        )
        r = screen_source(src, "fin")
        assert r.excluded_reason == "external_input"

    def test_library_excluded(self):
        src = "//@version=5\nlibrary(\"utils\")\nexport f(int x) => x * 2\n"
        r = screen_source(src, "lib")
        assert not r.admitted
        assert r.script_kind == "library"


class TestRobustness:
    @pytest.mark.parametrize("src", ["", "   ", "not pine at all", None])
    def test_degenerate_input_does_not_raise(self, src):
        r = screen_source(src, "junk")
        assert not r.admitted
        assert r.excluded_reason == "no_entry"

    def test_mechanism_family_detected(self):
        src = _strategy(
            "r = ta.rsi(close, 14)\n"
            "[m, u, l] = ta.bb(close, 20, 2)\n"
            "if ta.crossover(r, 30)\n"
            '    strategy.entry("L", strategy.long)\n'
            "if ta.crossunder(r, 70)\n"
            '    strategy.close("L")\n'
        )
        r = screen_source(src, "mr")
        assert r.mechanism_family == "mean_reversion"

    def test_single_keyword_family_falls_back_to_hybrid(self):
        r = screen_source(_strategy(_ENTRY_EXIT), "one")
        # _ENTRY_EXIT only uses ta.ema -> one trend keyword, below the 2-hit floor
        assert r.mechanism_family == "hybrid"
