#!/usr/bin/env python3
"""
Coverage Audit — checks this repo's commodity/currency pipelines against the
closed, enumerable catalogs their upstream sources publish in full, to catch
gaps like the ones found and fixed 2026-09-10: Uranium and Dubai/APSP crude
missing from metals/energy, a Silicon USGS page never wired up, battery
materials (Nickel/Silicon/Copper) missing from comtrade HS codes, and 6 IMF
agriculture series (Hides/Olive Oil/Swine/Salmon/Wool) missing entirely.

Two catalogs checked (both closed lists a single source publishes in full,
so "not referenced anywhere" has an unambiguous, checkable answer):
  1. FRED's IMF PCPS "Global price of X" series (commodities — energy,
     metals, agriculture). ~53 unique series as of 2026-09.
  2. Frankfurter/ECB's supported currency list (FX).

For each catalog entry NOT found by exact ID/code grep across every .py
file, this ALSO greps a curated-table allowlist for the plain-English
commodity name, because a different pipeline can cover the same commodity
under a completely different series ID from a different source. That is
EXACTLY how the first pass of this audit overcounted 2026-09-10: 21 of 27
"IMF agriculture gaps" turned out to already be tracked by
worldbank_pink_sheet.py (a dynamic Excel parse, not a hardcoded FRED ID
list) under names like "Rubber, RSS3" or "Coffee, Robusta". A source-only
grep cannot see that. This second pass can't prove equivalence -- it
downgrades a miss from "GAP" to "POSSIBLE DUPLICATE, needs a human/Claude
look" instead of either silently missing it or silently red-flagging
something already covered.

Requires FRED_API_KEY in .env for the commodity catalog check; the FX
check is keyless (Frankfurter).

CLI:
  python coverage_audit.py                 # check both catalogs, print report
  python coverage_audit.py --fail-on-gap    # exit 1 if any confirmed GAP found
                                             # (for the weekly scheduled task)

Output: prints a report to stdout. Wire into scripts\\weekly_data_quality.ps1
(or a dedicated monthly task -- these catalogs change rarely) the same way
validate.py is wired in, archiving to storage\\quality_reports\\.
"""

import argparse
import os
import re
import sys
from pathlib import Path

import duckdb
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).parent
CURATED_DIR = REPO_ROOT / "storage" / "curated"
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_SEARCH = "https://api.stlouisfed.org/fred/series/search"
FRANKFURTER_CURRENCIES = "https://api.frankfurter.app/currencies"

# Curated tables worth scanning for "same commodity, different source/ID"
# matches. Keep this small and targeted -- scanning all 227 tables (some
# 10s of millions of rows, e.g. prices/fed_soma) would be slow and mostly
# irrelevant. Add a table here if a new commodity-ish pipeline is built.
COMMODITY_TABLES = [
    ("wb_commodities", "commodity"),
    ("commodities", "name"),
    ("imf_commodities", "name"),
    ("metals_spot", "name"),
    ("usgs_minerals", "commodity"),
    ("macro", "name"),
]

STOPWORDS = {"global", "price", "of", "the", "average", "us", "usd", "u.s."}

# Known cases where different sources use an unrelated word for the same
# commodity (a substring match can't bridge these on its own). Add an entry
# here whenever a "GAP" turns out, on inspection, to be a naming mismatch
# rather than a real gap -- keeps the false-positive list from recurring.
SYNONYMS: dict[str, list[str]] = {
    "poultry": ["chicken"],
    "swine": ["pork", "hog"],
    "maize": ["corn"],
    "groundnut": ["peanut"],
}


def fetch_fred_imf_pcps() -> dict[str, str]:
    """Return {series_id (monthly variant): plain_title} for every unique
    IMF PCPS 'Global price of X' series FRED mirrors."""
    if not FRED_API_KEY:
        print("  SKIP: FRED_API_KEY not set -- cannot check the FRED catalog.")
        return {}
    r = requests.get(FRED_SEARCH, params={
        "search_text": "Global price of",
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "limit": 1000,
    }, timeout=30)
    r.raise_for_status()
    seen: dict[str, str] = {}
    for s in r.json().get("seriess", []):
        sid = s["id"]
        if not re.match(r"^P[A-Z0-9]+USD[MAQ]$", sid):
            continue
        base, freq = sid[:-1], sid[-1]
        if freq == "M" or base not in seen:
            seen[f"{base}M"] = s["title"]
    return seen


def fetch_frankfurter_currencies() -> dict[str, str]:
    r = requests.get(FRANKFURTER_CURRENCIES, timeout=30)
    r.raise_for_status()
    return r.json()  # {code: name}, e.g. {"USD": "United States Dollar", ...}


def grep_repo_for(token: str) -> bool:
    """True if `token` appears in any .py file in the repo (source-code check)."""
    for py_file in REPO_ROOT.glob("*.py"):
        if py_file.name == "coverage_audit.py":
            continue
        try:
            if token in py_file.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            continue
    return False


def keyword_hits_in_curated(keyword: str) -> list[str]:
    """Search the COMMODITY_TABLES allowlist for `keyword` (and any known
    synonyms) in its text column. Returns a list of 'table: matched_value'
    hits (possible duplicate coverage under a different source/name)."""
    hits = []
    con = duckdb.connect(":memory:")
    for word in [keyword] + SYNONYMS.get(keyword, []):
        for table, col in COMMODITY_TABLES:
            path = CURATED_DIR / table / f"{table}.parquet"
            if not path.exists():
                continue
            try:
                rows = con.execute(
                    f"SELECT DISTINCT {col} FROM read_parquet(?) "
                    f"WHERE lower({col}) LIKE ? LIMIT 5",
                    [str(path), f"%{word.lower()}%"],
                ).fetchall()
            except duckdb.Error:
                continue
            for (val,) in rows:
                hit = f"{table}: {val!r}"
                if hit not in hits:
                    hits.append(hit)
    return hits


def title_keyword(title: str) -> str:
    """'Global price of Coffee, Robustas' -> 'coffee'. Best-effort: strip
    the boilerplate prefix and take the first meaningful word."""
    t = title.lower().replace("global price of", "").strip()
    t = re.split(r"[,;(]", t)[0].strip()
    words = [w for w in t.split() if w not in STOPWORDS]
    word = words[0] if words else t
    # Naive de-pluralization so "Bananas" matches a curated "Banana, Europe" --
    # this is a substring match, not NLP; it only needs to handle the plain "s"
    # case well enough to avoid an easy false gap. Fine to skip 'y'->'ies' etc.
    if word.endswith("s") and len(word) > 3:
        word = word[:-1]
    return word


def audit_fred_commodities() -> tuple[list[str], list[str]]:
    """Returns (gaps, possible_duplicates) as human-readable lines."""
    print("=" * 78)
    print("CATALOG 1: FRED IMF PCPS 'Global price of X' commodity series")
    print("=" * 78)
    catalog = fetch_fred_imf_pcps()
    if not catalog:
        return [], []
    print(f"  {len(catalog)} unique series in the catalog\n")

    gaps, dupes = [], []
    for sid, title in sorted(catalog.items()):
        base_id = sid[:-1]  # strip the M -- code may reference PxxxUSDM/A/Q variants
        if grep_repo_for(base_id):
            continue
        keyword = title_keyword(title)
        hits = keyword_hits_in_curated(keyword) if keyword else []
        if hits:
            dupes.append(f"  {sid} | {title}\n      possibly covered by: {'; '.join(hits[:3])}")
        else:
            gaps.append(f"  {sid} | {title}")
    return gaps, dupes


def audit_frankfurter_currencies() -> tuple[list[str], list[str]]:
    print()
    print("=" * 78)
    print("CATALOG 2: Frankfurter/ECB supported currencies (FX)")
    print("=" * 78)
    try:
        catalog = fetch_frankfurter_currencies()
    except requests.RequestException as e:
        print(f"  SKIP: Frankfurter request failed: {e}")
        return [], []
    print(f"  {len(catalog)} currencies in the catalog\n")

    forex_file = REPO_ROOT / "forex_pipeline.py"
    tracked = set()
    if forex_file.exists():
        text = forex_file.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"TARGET_CURRENCIES\s*=\s*\[(.*?)\]", text, re.DOTALL)
        if m:
            tracked = set(re.findall(r'"([A-Z]{3})"', m.group(1)))

    gaps = []
    for code, name in sorted(catalog.items()):
        if code == "USD" or code in tracked:
            continue
        gaps.append(f"  {code} | {name}")
    return gaps, []


def main():
    parser = argparse.ArgumentParser(description="Audit commodity/currency pipelines against closed upstream catalogs")
    parser.add_argument("--fail-on-gap", action="store_true",
                        help="Exit 1 if any confirmed GAP is found (for scheduled-task wiring)")
    args = parser.parse_args()

    all_gaps: list[str] = []
    all_dupes: list[str] = []

    for gaps, dupes in (audit_fred_commodities(), audit_frankfurter_currencies()):
        all_gaps += gaps
        all_dupes += dupes

    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    if all_gaps:
        print(f"\n{len(all_gaps)} CONFIRMED GAP(S) -- not referenced in any .py file AND not found")
        print("under any name in the commodity-table allowlist:")
        for line in all_gaps:
            print(line)
    else:
        print("\nNo confirmed gaps.")

    if all_dupes:
        print(f"\n{len(all_dupes)} POSSIBLE DUPLICATE(S) -- no exact series-ID match in source, but a")
        print("plausible same-commodity match exists under a different name/source.")
        print("Needs a human/Claude look before adding -- may already be covered:")
        for line in all_dupes:
            print(line)

    print()
    if args.fail_on_gap and all_gaps:
        sys.exit(1)


if __name__ == "__main__":
    main()
