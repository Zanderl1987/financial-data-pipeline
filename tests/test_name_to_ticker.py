"""
tests/test_name_to_ticker.py -- name_to_ticker resolution tiers.

Resolver logic is tested against an injected synthetic `securities` frame
(NameResolver.from_frame), not the live store, so wrong-symbol bugs fail fast
and deterministically. One light live-store test covers the CA disclosure
universe coverage (<10 seconds, mirrors other store-backed tests).
"""

import pandas as pd
import pytest

from name_to_ticker import (NameResolver, _SUFFIX_WORDS, normalize,
                            _significant_tokens, build_index)


def _sec(rows):
    return pd.DataFrame(rows,
                        columns=["symbol", "company_name", "is_sp500",
                                 "is_russell3000", "is_russell2000",
                                 "is_wilshire5000", "exchange"])


SEC = _sec([
    ("AA", "Alcoa Corporation", True, True, False, True, "NYSE"),
    ("JWN", "Nordstrom Inc", False, True, True, True, "NYSE"),
    ("DUUKU", "Duke Energy CORP", False, False, False, False, None),
    ("MDLZ", "Mondelez International Inc", True, True, False, True, "NASDAQ"),
    ("ZZZZ", "Noisey Shell Systems Co", False, False, False, False, None),
])


class TestNormalize:
    def test_casefold_and_punct(self):
        assert normalize("Proctor &amp; Gamble Co.") == "PROCTOR AMP GAMBLE CO"
        assert normalize("  apple   Inc. ") == "APPLE INC"

    def test_significant_tokens_strip_suffixes(self):
        assert _significant_tokens("MONDELEZ INTERNATIONAL INC") == ("MONDELEZ", "INTERNATIONAL")
        assert _significant_tokens("Duke Energy CORP") == ("DUKE", "ENERGY")


class TestBuildIndex:
    def test_exact_and_tokens(self):
        exact, tokens, meta = build_index(SEC)
        assert exact["ALCOA CORPORATION"] == "AA"
        assert tokens[("MONDELEZ", "INTERNATIONAL")] == ("MDLZ",)
        assert tokens[("DUKE", "ENERGY")] == ("DUUKU",)
        assert meta["DUUKU"]["listed"] is False
        assert meta["JWN"]["listed"] is True


class TestResolve:
    def test_canonical_tier(self):
        r = NameResolver.from_frame(SEC)
        assert r.resolve("Coca Cola Co")["symbol"] == "KO"
        assert r.resolve("Bank America")["symbol"] == "BAC"
        assert r.resolve("Apple Inc")["symbol"] == "AAPL"
        assert r.resolve("Duke Energy Corp")["symbol"] == "DUK"

    def test_exact_tier(self):
        r = NameResolver.from_frame(SEC)
        res = r.resolve("Alcoa Corporation")
        assert res["symbol"] == "AA" and res["method"] == "exact"

    def test_tokens_tier_unambiguous(self):
        r = NameResolver.from_frame(SEC)
        res = r.resolve("Mondelez International Inc")
        assert res["symbol"] == "MDLZ" and res["method"] in ("exact", "tokens")

    def test_tokens_tier_junk_collision_rejected_when_required(self):
        # DUUKU is a junk legacy listing that shares tokens with the real name.
        r = NameResolver.from_frame(SEC)
        assert r.resolve("Duke Energy Inc", require_listed=True) is None
        assert r.resolve("Duke Energy Inc", require_listed=False)["symbol"] == "DUUKU"

    def test_ambiguous_tokens_return_none(self):
        dup = _sec([("X1", "Widget Master Inc", False, True, False, False, None),
                    ("X2", "Widget Master Corporation", False, True, False, False, None)])
        r = NameResolver.from_frame(dup)
        assert r.resolve("Widget Master Co") is None

    def test_short_or_junk_names_return_none(self):
        r = NameResolver.from_frame(SEC)
        for junk in ["", "n/a", "NONE", "If 'other,' 401k Administrator",
                     "NYSE", "https://x", "U.", "X"]:
            assert r.resolve(junk) is None, junk

    def test_unknown_name_returns_none(self):
        r = NameResolver.from_frame(SEC)
        assert r.resolve("Widgets For All Inc") is None

    def test_single_token_never_token_matches(self):
        r = NameResolver.from_frame(SEC)
        assert r.resolve("Nordstrom")["symbol"] == "JWN"   # canonical alias
        assert r.resolve("Noisey") is None


class TestFrame:
    def test_resolve_frame_columns(self):
        r = NameResolver.from_frame(SEC)
        fr = r.resolve_frame(pd.Series(["Alcoa Corporation", "Neverheard Inc"]))
        assert list(fr.columns) == ["entity", "symbol", "method"]
        assert fr.loc[0, "entity"] == "Alcoa Corporation"
        assert fr.loc[0, "symbol"] == "AA"
        assert fr.loc[1, "symbol"] is None or pd.isna(fr.loc[1, "symbol"])
        assert fr.loc[0, "method"] == "exact"
        assert fr.loc[1, "method"] is None or pd.isna(fr.loc[1, "method"])

    def test_coverage(self):
        r = NameResolver.from_frame(SEC)
        cov = r.coverage(pd.Series(["Alcoa Corporation", "Apple", "Xyz"]))
        assert cov["rows"] == 3
        assert cov["resolved_rows"] == 2
        assert cov["pct_rows"] == pytest.approx(66.7, abs=0.1)


@pytest.mark.skipif(True, reason="live-store path covered by the study run")
def test_live_store_loads():
    r = NameResolver().load()
    assert r.resolve("Apple Inc")["symbol"] == "AAPL"


def test_real_ca_disclosures_resolution_coverage():
    """The event study depends on this rate not collapsing; guard it."""
    import query as q
    sec = q.load("securities")
    if sec.empty:
        pytest.skip("securities table unavailable")
    r = NameResolver.from_frame(sec)
    df = q.load("california_disclosures")
    stock = df[df["nature_of_investment"].fillna("")
               .astype(str).str.contains("Stock", case=False, na=False)]
    fr = r.resolve_frame(stock["business_entity"], require_listed=True)
    assert fr["symbol"].notna().mean() >= 0.40, fr["symbol"].notna().mean()