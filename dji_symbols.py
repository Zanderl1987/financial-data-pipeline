"""
Single source of truth for the DJIA-30 constituent tickers.

Consolidated 2026-10-05. This logic was previously copy-pasted into seven
pipelines (alpha_vantage_fundamentals, finnhub, fundamentals, options_chain,
price_history, sec_edgar, plus an inline list in synthetic_options). Every copy
carried the same two defects, described below.

DEFECT 1 -- the scraper silently never worked. All seven copies did:

    pd.read_html("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average")

and searched for a table with a "Symbol"/"Ticker" column of 25-35 rows. That
table is no longer on that page: it moved to "List of Dow Jones Industrial
Average companies" (verified live 2026-10-05). The main article now only embeds
the components list as a navbox template. So the `for df in tables` loop found
nothing, the `raise` fired, and EVERY pipeline silently fell through to its
hardcoded fallback on every single run. The `print("Scraped N symbols")` branch
was almost certainly dead code. Callers cannot tell the difference -- a stale
answer looks exactly like a fresh one.

DEFECT 2 -- the hardcoded fallback was stale in both directions, which is how
this was found. Verified live against the S&P Global change history and
Wikipedia's "Historical components of the Dow Jones Industrial Average":

  - VZ (Verizon) was still in the list. Alphabet REPLACED Verizon on
    2026-06-29, so VZ had not been a DJIA member for months.
  - GOOGL (Alphabet) was missing entirely -- added the same 2026-06-29 date.
  - DOW (Dow Inc.) was correctly absent: Sherwin-Williams replaced it on
    2024-11-08, alongside Nvidia replacing Intel. Do NOT "fix" DOW back in --
    it is an S&P 500 / Russell 3000 member, not a DJIA constituent.

Both lists still contained 30 names, so no count-based sanity check could catch
this. A DJIA-keyed comparison biased by a phantom VZ and a missing GOOGL is a
silent data-quality bug, not a visible failure.

WHY WIKITEXT RATHER THAN read_html: the components table's Symbol column is
populated by `{{NYSE link|XXX}}` / `{{NASDAQ link|XXX}}` templates, which
read_html does not resolve into clean ticker text (this is the same class of
problem sp500_membership_pipeline.py hit on the S&P 500 -- see its module
docstring). Fetching raw wikitext via the MediaWiki action API and regexing the
link templates yields exact, unambiguous tickers.

Fallback policy: FALLBACK_DJI_SYMBOLS below is a hand-verified snapshot, not a
guess. It is only used when the fetch fails, and `get_dji_symbols()` reports
which path it took so a fallback-driven run is never mistaken for a live one.
"""

import re
import time

import requests

# The components table lives here, NOT on the main "Dow Jones Industrial
# Average" article (see DEFECT 1).
COMPONENTS_PAGE = "List of Dow Jones Industrial Average companies"

_API_URL = "https://en.wikipedia.org/w/api.php"
_HEADERS = {"User-Agent": "financial-data-pipeline (Wikipedia DJIA components)"}

_MAX_RETRIES = 3
_RETRY_SLEEP = 10

# Matches the {{NYSE link|MMM}} / {{NASDAQ link|GOOGL}} templates that carry the
# Symbol column, plus the equivalent plain-text forms, so a future wikitext
# edit that drops the templates degrades to the plain pipe rather than to
# nothing at all.
_SYMBOL_RE = re.compile(r"\{\{\s*(?:NYSE|NASDAQ)\s+link\s*\|\s*([^}|]+)")
_PLAIN_SYMBOL_RE = re.compile(r"^\|\s*([A-Z][A-Z.\-]{0,5})\s*$")

# Hand-verified 2026-10-05 against the live components table. Reflects the
# 2026-06-29 Alphabet-for-Verizon swap. Sorted for readability only;
# get_dji_symbols() preserves the page's own row order.
FALLBACK_DJI_SYMBOLS = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GOOGL", "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM",
    "MRK", "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "WMT",
]

# Sanity bound. The DJIA has exactly 30 members; allow a little slack so a
# single-seat HTML artifact cannot hard-fail the fetch, but reject anything
# wild enough to indicate we parsed the wrong table.
_EXPECTED_COUNT = 30
_MIN_COUNT, _MAX_COUNT = 25, 35


class DJISymbolError(RuntimeError):
    """Raised when the live fetch fails AND the caller asked to fail loudly."""


def _fetch_wikitext(page: str) -> str:
    """MediaWiki action API raw wikitext, with retry. Raises on final failure."""
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.get(
                _API_URL,
                params={
                    "action": "parse",
                    "page": page,
                    "prop": "wikitext",
                    "format": "json",
                    "formatversion": 2,
                },
                headers=_HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()["parse"]["wikitext"]
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
            last_exc = exc
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_SLEEP * attempt)
    raise RuntimeError(f"could not fetch wikitext for {page!r}: {last_exc}")


def _parse_symbols(wikitext: str) -> list[str]:
    """
    Extract DJIA tickers from the components table wikitext.

    Scoped to the table carrying id="constituents" so an unrelated table on the
    same page (or a navbox of company links) can't be picked up instead.
    """
    start = wikitext.find('id="constituents"')
    if start == -1:
        raise ValueError('no table with id="constituents" found on the page')
    end = wikitext.find("\n|}", start)
    table = wikitext[start:end if end != -1 else None]

    symbols = [s.strip().upper() for s in _SYMBOL_RE.findall(table)]
    if not symbols:
        # Template-free variant. Plain-symbol lines only, so this cannot pick up
        # the Company/Exchange/Sector/date columns around them.
        symbols = [
            m.group(1).upper()
            for line in table.splitlines()
            if (m := _PLAIN_SYMBOL_RE.match(line.strip()))
        ]

    if not symbols:
        raise ValueError("components table contained no recognizable tickers")
    if not _MIN_COUNT <= len(symbols) <= _MAX_COUNT:
        raise ValueError(
            f"parsed {len(symbols)} tickers from the components table; expected "
            f"about {_EXPECTED_COUNT} -- Wikipedia's layout likely changed and "
            f"this parser needs updating rather than returning a wrong universe"
        )
    return symbols


def get_dji_symbols(strict: bool = False) -> list[str]:
    """
    Return the current DJIA-30 constituent tickers, in the source page's order.

    On any failure falls back to FALLBACK_DJI_SYMBOLS and prints which path was
    taken -- a stale-but-known universe beats a crash for these pipelines, but
    it must be visible in the run log rather than looking like a live fetch.

    strict=True re-raises instead of falling back (used by tests, and by
    callers that must not silently analyze a stale universe).
    """
    try:
        symbols = _parse_symbols(_fetch_wikitext(COMPONENTS_PAGE))
        print(f"Fetched {len(symbols)} DJI symbols from Wikipedia ({COMPONENTS_PAGE}).")
        return symbols
    except Exception as exc:  # noqa: BLE001 - any failure falls back by design
        if strict:
            raise DJISymbolError(f"DJIA symbol fetch failed: {exc}") from exc
        print(
            f"Wikipedia fetch failed ({exc}). Using verified fallback list. "
            f"This run is NOT using live index membership."
        )
        return list(FALLBACK_DJI_SYMBOLS)