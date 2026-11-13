"""
test_finviz_ticker_parsing.py — regression cover for the Finviz ticker bug.

Found 2026-10-05 while auditing whether DOW was tracked: DOW was absent from
every Finviz table, which looked like a coverage gap. It was actually present
as "DDOW". Finviz had changed its Ticker cell to nest a logo <img> plus a
one-letter exchange hint inside a `company-ticker` element, and pd.read_html
concatenates all descendant text in a cell -- so every ticker parsed with a
spurious leading capital ("A" + "AAPL" = "AAAPL").

100% of finviz_screener / finviz_financials / finviz_movers / finviz_insider
rows written since 2026-08-02 were affected, which silently broke every join
on `ticker` against prices / fundamentals / securities.

These tests pin the parsing behaviour and the sanity guard so the next Finviz
markup change fails loudly instead of quietly corrupting the column again.
"""

import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import finviz_pipeline as fv  # noqa: E402


# Real Finviz markup, captured live 2026-10-05. Screener/movers wrap the logo
# element in <a>; the insider-trading page uses <span> for the same role.
# Both carry the genuine ticker in a SIBLING tab-link anchor.
SCREENER_CELL = (
    '<td data-boxover-ticker="AAPL" data-boxover-company="Apple Inc">'
    '<span class="flex items-center gap-1 pl-0.5">'
    '<a class="company-ticker" href="stock?t=AAPL&amp;ty=c&amp;p=d&amp;b=1" '
    'style="--hover-color: #1A1E21; '
    '--logo-url: url(\'https://logo.finviz.com/AAPL.svg\')">'
    '<img src="https://logo.finviz.com/AAPL.svg" alt="AAPL logo" '
    'crossorigin="anonymous" '
    'onload="this.classList.add(\'is-loaded\')" />'
    "<span>A</span></a>"
    '<a href="stock?t=AAPL&ty=c&p=d&b=1" class="tab-link">AAPL</a>'
    "</span></td>"
)

INSIDER_CELL = (
    '<td data-boxover-ticker="AAPL" data-boxover-company="Apple Inc">'
    '<span class="flex items-center gap-1">'
    '<span class="company-ticker" style="--hover-color: #1A1E21; '
    '--logo-url: url(\'https://logo.finviz.com/AAPL.svg\')">'
    '<img src="https://logo.finviz.com/AAPL.svg" alt="AAPL logo" '
    'crossorigin="anonymous" '
    'onload="this.classList.add(\'is-loaded\')" />'
    "<span>A</span></span>"
    '<a href="stock?t=AAPL&b=2" class="tab-link">AAPL</a>'
    "</span></td>"
)


def _page(cells: list[str]) -> str:
    """Wrap ticker cells in a minimal Finviz-shaped results table."""
    rows = "".join(
        f"<tr><td>{i + 1}</td>{c}"
        "<td><a href='#'>Apple Inc</a></td></tr>"
        for i, c in enumerate(cells)
    )
    return (
        '<html><body><table class="table table-bordered screeners">'
        "<thead><tr><th>No.</th><th>Ticker</th><th>Company</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></body></html>"
    )


class TestTickerLogoStripping:
    def test_strip_removes_logo_element_anchor_form(self):
        out = fv._strip_ticker_logo_anchors(SCREENER_CELL)
        assert "company-ticker" not in out
        # The genuine ticker must survive -- that is the whole point.
        assert ">AAPL</a>" in out

    def test_strip_removes_logo_element_span_form(self):
        """The insider page wraps the same content in <span>, not <a>."""
        out = fv._strip_ticker_logo_anchors(INSIDER_CELL)
        assert "company-ticker" not in out
        assert ">AAPL</a>" in out

    def test_stripped_markup_stays_balanced(self):
        """A regex that ate a closing tag would break sibling cell parsing."""
        for cell in (SCREENER_CELL, INSIDER_CELL):
            out = fv._strip_ticker_logo_anchors(cell)
            assert out.count("<span") == out.count("</span>")
            assert out.count("<a ") == out.count("</a>")

    def test_strip_is_a_noop_on_old_markup(self):
        """Pages without the logo element must pass through untouched."""
        old = '<td><a href="stock?t=AAPL&b=2" class="tab-link">AAPL</a></td>'
        assert fv._strip_ticker_logo_anchors(old) == old

    def test_parsed_ticker_is_not_doubled(self):
        df = fv._find_table_with_col(_page([SCREENER_CELL]), "Ticker")
        assert not df.empty
        tickers = [str(t).strip() for t in df["Ticker"]]
        assert tickers == ["AAPL"]

    def test_parsed_ticker_not_doubled_insider_markup(self):
        df = fv._find_table_with_col(_page([INSIDER_CELL]), "Ticker")
        assert not df.empty
        assert [str(t).strip() for t in df["Ticker"]] == ["AAPL"]


class TestTickerPlausibilityGuard:
    """
    The guard for the whole failure mode: a mangled ticker column produces
    valid-looking strings that don't exist, so nothing downstream notices --
    rows just silently fail to join.
    """

    @staticmethod
    def _frame(tickers):
        return pd.DataFrame({"ticker": tickers})

    def test_accepts_real_tickers(self):
        real = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
                "JPM", "WMT", "V", "UNH", "JNJ", "PG", "HD", "CVX", "KO"]
        fv._assert_tickers_plausible(self._frame(real * 6))

    def test_rejects_doubled_tickers(self):
        """Exactly the bug: every ticker carrying a spurious leading capital."""
        doubled = [t[0] + t for t in
                   ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
                    "JPM", "WMT", "V", "UNH", "JNJ", "PG", "HD", "CVX", "KO"]]
        with pytest.raises(ValueError, match="not recognized|recognized symbols"):
            fv._assert_tickers_plausible(self._frame(doubled * 6))

    def test_skips_small_frames(self):
        """Too few rows to give a reliable signal -- must not raise."""
        fv._assert_tickers_plausible(self._frame(["ZZZZ", "YYYY", "XXXX"]))

    def test_skips_when_no_ticker_column(self):
        fv._assert_tickers_plausible(pd.DataFrame({"name": ["x"] * 100}))

    def test_known_tickers_loaded_from_repo(self):
        """The guard is useless if its reference list is empty."""
        known = fv._known_tickers()
        assert len(known) > 1000
        assert "AAPL" in known
        assert "DOW" in known