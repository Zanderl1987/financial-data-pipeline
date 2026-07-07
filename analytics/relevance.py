"""
Article -> company relevance scoring.

Given an article's headline/summary and a target symbol, answer "how relevant
is this article to this company?" on a 0..1 scale, two ways:

  DIRECT   - the company (ticker or name alias) is mentioned in the text.
             Headline mention = 1.0, summary-only mention = 0.7.
  INDIRECT - the article is about a market driver (oil, rates, the dollar,
             gold, ...) that the company has a *measured* exposure to
             (analytics/exposure.py, |t_ex_mkt| > 3). Scales with the
             strength of the exposure, capped at 0.6 so an indirect hit
             never outranks a direct mention.

The final relevance is the max of the two channels.

Company aliases come from the finnhub_profile table (ticker + official name)
plus hand-written short forms ("Disney", "Goldman", "JPMorgan", ...). Ticker
mentions only count in unambiguous forms ($XOM, "NYSE: XOM") -- bare tickers
like V, BA, KO are ordinary English words/initials and are ignored.

Usage
-----
  python -m analytics.relevance --symbol CVX --limit 10        # score recent news
  python -m analytics.relevance --symbol JPM --text "Fed signals rate cuts ahead"

Library:
  from analytics import relevance
  rel = relevance.Relevance(["CVX", "JPM"])        # loads aliases + exposures
  rel.score("Oil surges as OPEC cuts output", "", "CVX")
"""

import argparse
import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import query as q

# ---------------------------------------------------------------------------
# Company aliases
# ---------------------------------------------------------------------------

# Legal suffixes stripped from official names to form a base alias
_NAME_SUFFIX_RE = re.compile(
    r"\s+(inc|corp|co|companies|company|corporation|group|international|"
    r"holdings?)\.?$", re.IGNORECASE)

# Hand-written short forms the suffix-stripper can't derive. Keyed by ticker.
MANUAL_ALIASES = {
    "AAPL": ["Apple"],
    "AMZN": ["Amazon"],
    "AXP":  ["American Express", "Amex"],
    "BA":   ["Boeing"],
    "CRM":  ["Salesforce"],
    "CSCO": ["Cisco"],
    "CVX":  ["Chevron"],
    "DIS":  ["Disney", "Walt Disney"],
    "GS":   ["Goldman Sachs", "Goldman"],
    "HD":   ["Home Depot"],
    "HON":  ["Honeywell"],
    "IBM":  ["IBM"],
    "JNJ":  ["Johnson & Johnson", "J&J"],
    "JPM":  ["JPMorgan", "JP Morgan", "JPMorgan Chase"],
    "KO":   ["Coca-Cola", "Coca Cola", "Coke"],
    "MCD":  ["McDonald's", "McDonalds"],
    "MMM":  ["3M"],
    "MRK":  ["Merck"],
    "MSFT": ["Microsoft"],
    "NKE":  ["Nike"],
    "NVDA": ["Nvidia", "NVIDIA"],
    "PG":   ["Procter & Gamble", "P&G"],
    "SHW":  ["Sherwin-Williams", "Sherwin Williams"],
    "TRV":  ["Travelers"],
    "UNH":  ["UnitedHealth", "United Health"],
    "V":    ["Visa"],
    "VZ":   ["Verizon"],
    "WMT":  ["Walmart", "Wal-Mart"],
}

# Unambiguous ticker mention forms: $XOM cashtags and "NYSE: XOM" style
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
_EXCHANGE_RE = re.compile(
    r"\((?:NYSE|NASDAQ|AMEX|CBOE)\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\)",
    re.IGNORECASE)


def load_company_aliases(symbols=None) -> "dict[str, list[str]]":
    """
    ticker -> list of name aliases, from finnhub_profile + MANUAL_ALIASES.
    Falls back to MANUAL_ALIASES alone if the profile table is unavailable.
    """
    aliases: dict[str, list[str]] = {}
    try:
        prof = q.load("finnhub_profile")
    except Exception:
        prof = pd.DataFrame()
    if not prof.empty:
        for _, row in prof.drop_duplicates("symbol").iterrows():
            sym, name = str(row["symbol"]), str(row.get("name") or "")
            if not name:
                continue
            names = [name]
            base = _NAME_SUFFIX_RE.sub("", name).strip()
            # strip twice: "Goldman Sachs Group Inc" -> "Group Inc" -> ""
            base2 = _NAME_SUFFIX_RE.sub("", base).strip()
            for b in (base, base2):
                if b and b.lower() != name.lower():
                    names.append(b)
            aliases[sym] = names
    for sym, extra in MANUAL_ALIASES.items():
        cur = aliases.setdefault(sym, [])
        cur.extend(a for a in extra if a.lower() not in
                   {c.lower() for c in cur})
    if symbols is not None:
        aliases = {s: aliases.get(s, []) for s in symbols}
    return aliases


def _alias_patterns(aliases: "dict[str, list[str]]"):
    """ticker -> compiled word-boundary regex over all its aliases."""
    pats = {}
    for sym, names in aliases.items():
        parts = [re.escape(n) for n in names if n]
        if parts:
            pats[sym] = re.compile(
                r"(?<![\w$])(" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)
    return pats


def extract_tickers(text: str, aliases: "dict[str, list[str]]",
                    _pat_cache: dict = {}) -> "set[str]":
    """
    Symbols mentioned in `text`: company-name aliases (case-insensitive,
    word-boundary) plus unambiguous ticker forms ($XOM, "(NYSE: XOM)").
    Bare tickers are deliberately NOT matched.
    """
    if not text:
        return set()
    key = id(aliases)
    if key not in _pat_cache:
        _pat_cache[key] = _alias_patterns(aliases)
    found = {m.group(1).upper() for m in _CASHTAG_RE.finditer(text)}
    found |= {m.group(1).upper() for m in _EXCHANGE_RE.finditer(text)}
    found &= set(aliases)                       # only symbols we know
    for sym, pat in _pat_cache[key].items():
        if pat.search(text):
            found.add(sym)
    return found


# ---------------------------------------------------------------------------
# Driver topic tagging (maps article text -> exposure.py driver names)
# ---------------------------------------------------------------------------

DRIVER_PATTERNS = {
    "oil":      r"\b(crude|oil price|oil prices|opec|wti|brent|petroleum|"
                r"oil output|oil supply|oil demand)\b",
    "natgas":   r"\b(natural gas|lng|nat gas)\b",
    "gasoline": r"\b(gasoline|pump price|fuel price)\b",
    "gold":     r"\bgold (price|prices|futures|rally|slump)|price of gold\b",
    "copper":   r"\bcopper\b",
    "silver":   r"\bsilver (price|prices|futures)\b",
    "wheat":    r"\bwheat\b",
    "corn":     r"\bcorn (price|prices|futures|crop)\b",
    "soybeans": r"\bsoybeans?\b",
    "t10y":     r"\b(treasury yield|10-year|ten-year|bond yield|interest rate|"
                r"rate (hike|cut|hikes|cuts)|fed funds|federal reserve|fomc)\b",
    "eur":      r"\b(dollar (strength|weakness|rally|slide)|weaker dollar|"
                r"stronger dollar|dxy|euro)\b",
    "vix":      r"\b(volatility|vix|market turmoil|market selloff)\b",
}
_DRIVER_RE = {d: re.compile(p, re.IGNORECASE) for d, p in DRIVER_PATTERNS.items()}

# Indirect relevance never outranks a direct mention
INDIRECT_CAP = 0.6
T_SIGNIFICANT = 3.0        # |t_ex_mkt| needed before an exposure counts
T_SATURATION = 20.0        # |t| at which indirect relevance hits the cap


def tag_drivers(text: str) -> "list[str]":
    """Driver names (exposure.py registry) whose topic appears in `text`."""
    if not text:
        return []
    return [d for d, pat in _DRIVER_RE.items() if pat.search(text)]


def indirect_relevance(t_ex_mkt: "float | None") -> float:
    """Map an exposure t-stat to 0..INDIRECT_CAP (0 below significance)."""
    if t_ex_mkt is None:
        return 0.0
    t = abs(float(t_ex_mkt))
    if t < T_SIGNIFICANT:
        return 0.0
    frac = min(1.0, (t - T_SIGNIFICANT) / (T_SATURATION - T_SIGNIFICANT))
    return round(INDIRECT_CAP * (0.5 + 0.5 * frac), 3)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_relevance(headline: str, summary: str, symbol: str,
                    aliases: "dict[str, list[str]]",
                    exposures: "pd.DataFrame | None" = None) -> dict:
    """
    Relevance of one article to one symbol.

    exposures: tidy frame from exposure.exposure_map() (may cover many
    symbols); used for the indirect channel. None = direct channel only.

    Returns {"relevance", "direct", "mentioned", "drivers", "via"} where
    `via` lists (driver, t_ex_mkt, contribution) for the indirect channel.
    """
    headline, summary = headline or "", summary or ""
    in_head = symbol in extract_tickers(headline, aliases)
    in_sum = symbol in extract_tickers(summary, aliases)
    direct = 1.0 if in_head else (0.7 if in_sum else 0.0)

    drivers = tag_drivers(headline + " " + summary)
    via = []
    indirect = 0.0
    if drivers and exposures is not None and not exposures.empty:
        mine = exposures[exposures["symbol"] == symbol]
        for d in drivers:
            row = mine[mine["driver"] == d]
            if row.empty:
                continue
            t = row.iloc[0].get("t_ex_mkt")
            if pd.isna(t):
                t = row.iloc[0].get("t_stat")
            contrib = indirect_relevance(t)
            if contrib > 0:
                via.append((d, float(t), contrib))
                indirect = max(indirect, contrib)

    return {
        "relevance": round(max(direct, indirect), 3),
        "direct": bool(direct),
        "mentioned": sorted(extract_tickers(headline + " " + summary, aliases)),
        "drivers": drivers,
        "via": via,
    }


class Relevance:
    """Convenience wrapper that loads aliases + exposures once."""

    def __init__(self, symbols, start: "str | None" = "2016-01-01",
                 exposures: "pd.DataFrame | None" = None):
        self.symbols = list(symbols)
        self.aliases = load_company_aliases()   # all known, so cross-mentions work
        if exposures is None:
            from analytics.exposure import exposure_map, DRIVERS
            want = [d for d in DRIVER_PATTERNS if d in DRIVERS]
            exposures = exposure_map(self.symbols, drivers=want, start=start)
        self.exposures = exposures

    def score(self, headline: str, summary: str, symbol: str) -> dict:
        return score_relevance(headline, summary, symbol,
                               self.aliases, self.exposures)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Article->company relevance")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--text", default=None,
                        help="score this text instead of recent news")
    parser.add_argument("--limit", type=int, default=10,
                        help="number of recent articles to score")
    parser.add_argument("--start", default="2016-01-01",
                        help="exposure estimation window start")
    args = parser.parse_args()

    rel = Relevance([args.symbol], start=args.start)

    if args.text:
        res = rel.score(args.text, "", args.symbol)
        print(f"\ntext: {args.text}")
        _print_result(args.symbol, res)
        return

    news = q.load("finnhub_news")
    if news.empty:
        print("finnhub_news table is empty.")
        return
    news = news.sort_values("datetime", ascending=False).head(args.limit)
    print(f"\n=== relevance of {len(news)} recent articles to {args.symbol} ===")
    for _, a in news.iterrows():
        res = rel.score(str(a.get("headline", "")), str(a.get("summary", "")),
                        args.symbol)
        head = str(a.get("headline", ""))[:70]
        print(f"\n[{a.get('symbol', '?')}] {head}")
        _print_result(args.symbol, res)


def _print_result(symbol: str, res: dict):
    print(f"  relevance={res['relevance']}  direct={res['direct']}"
          f"  mentioned={res['mentioned']}  drivers={res['drivers']}")
    for d, t, c in res["via"]:
        print(f"    via {d}: t_ex_mkt={t:.1f} -> {c}")


if __name__ == "__main__":
    main()
