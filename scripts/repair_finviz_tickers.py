"""
repair_finviz_tickers.py — undo the Finviz doubled-first-letter ticker bug.

WHAT THIS REPAIRS
Finviz changed its Ticker cell to nest a logo <img> plus a one-letter exchange
hint inside a `company-ticker` element, with the real ticker in a sibling
`tab-link` anchor. pd.read_html concatenates all descendant text in a cell, so
every ticker gained a spurious leading capital: "A" + "AAPL" = "AAAPL".
100% of finviz_screener / finviz_financials / finviz_movers / finviz_insider
rows written since 2026-08-02 were affected (~88k rows). Nothing downstream
noticed: the strings are valid, so they simply failed to join against
prices / fundamentals / securities on `ticker`.

The parser itself is fixed in finviz_pipeline.py. This script repairs the
already-stored history, which CANNOT be re-fetched -- these are rolling
"snapshot" pages (today's movers, recent Form 4 filings), so 2026-08-02
onwards exists only in the parquet already on disk. Deleting and re-running
would permanently lose it.

WHY REPAIR RATHER THAN DELETE-AND-REFETCH
`query.py` globs storage/raw/finviz/** directly for these tables, and they are
daily snapshots of rolling pages. There is no API to ask Finviz for
"2026-08-05's insider transactions". Re-running yields today only.

HOW A TICKER IS "PROVED" DOUBLED
Only rewrite X -> X[1:] when BOTH hold:
  1. X is not itself a recognized symbol (so we never touch a real ticker that
     legitimately starts with a doubled letter -- AAPL, ABBV, BRK-A ...), and
  2. X[1:] IS a recognized symbol in the reference set.
Anything not provable is left byte-for-byte untouched and reported in the
summary, so a wrong guess can never be written. Junk placeholder rows Finviz
emits ("JJ", "OO", "PP") stay as they are rather than being invented away.

USAGE
    python scripts/repair_finviz_tickers.py            # dry run, reports only
    python scripts/repair_finviz_tickers.py --apply    # rewrite in place
    python scripts/repair_finviz_tickers.py --tables movers,insider
"""

import argparse
import glob
import os
import sys
import tempfile

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

RAW_ROOT = os.path.join(REPO_ROOT, "storage", "raw", "finviz")
UNIVERSE_CSV = os.path.join(REPO_ROOT, "symbol_universe.csv")

# Tables whose `ticker` column is corrupted. `groups/*` is keyed on `name`
# (industry / country / sector / valuation groups), not tickers, and the
# group_valuation / sector / country tables were never affected.
AFFECTED = ("screener", "financials", "insider", "movers")

# Extra symbol sources beyond symbol_universe.csv. symbol_universe.csv has no
# dash-class entries at all and misses a lot of the smaller listings Finviz's
# movers page returns, so the union is what makes the "is X[1:] a real symbol"
# test work for ~99% of rows instead of ~96%.
EXTRA_SYMBOL_TABLES = (
    ("prices", "symbol"),
    ("yfinance_universe_prices", "symbol"),
    ("tiingo_prices", "symbol"),
    ("securities", "symbol"),
    ("fundamentals_annual", "symbol"),
    ("sp500_membership", "symbol"),
    ("option_listing_changes", "symbol"),
)


def build_reference() -> set[str]:
    """Recognized symbols: the repo universe CSV unioned with stored price data."""
    ref: set[str] = set()
    if os.path.exists(UNIVERSE_CSV):
        ref |= {
            s.strip().upper()
            for s in pd.read_csv(UNIVERSE_CSV, usecols=[0], dtype=str).iloc[:, 0]
            if isinstance(s, str) and s.strip()
        }

    try:
        import duckdb
        import query as q

        q.reload()
        con = duckdb.connect()
        q._register_views(con)
        for table, col in EXTRA_SYMBOL_TABLES:
            try:
                ref |= {
                    r[0].upper()
                    for r in con.execute(
                        f"SELECT DISTINCT {col} FROM {table}").fetchall()
                    if r[0] and isinstance(r[0], str)
                }
            except Exception:
                # A missing/unbuilt table just means a smaller reference.
                pass
        con.close()
    except Exception:
        pass

    return ref


# Longest value we will even consider a ticker. Real tickers top out around 7
# chars. Anything longer is not a mangled ticker -- it is a row where
# pd.read_html grabbed one of Finviz's filter-menu tables instead of the
# results table, so the "ticker" cell holds a filter description
# ("Descriptive Fundamental Technical News ETF All"). Those are a separate,
# pre-existing parse bug (46 rows in finviz_movers, dated 2026-08-12+) and are
# deliberately out of scope here: repairing the doubled first letter cannot
# make them meaningful, and deleting them is a separate call.
MAX_TICKER_LEN = 12


def plan_repairs(tickers, ref: set[str]):
    """
    Map each ticker to its repaired form.

    Returns (mapping, unresolved, junk) where `unresolved` holds short
    lookalike tickers that could not be proved either way, and `junk` holds
    the filter-menu rows described above.
    """
    mapping: dict[str, str] = {}
    unresolved: set[str] = set()
    junk: set[str] = set()
    for raw in tickers:
        if not isinstance(raw, str):
            continue
        t = raw.strip().upper()
        if not t or t in ref or t in mapping:
            continue
        if len(t) > MAX_TICKER_LEN:
            junk.add(t)
            continue
        cand = t[1:]
        if len(t) > 2 and cand in ref:
            mapping[t] = cand
        else:
            unresolved.add(t)
    return mapping, unresolved, junk


def _atomic_write(df: pd.DataFrame, path: str) -> None:
    """Write via temp file + replace so an interrupted run can't truncate data."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(suffix=".parquet", dir=d)
    os.close(fd)
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually rewrite files (default is a dry run)")
    ap.add_argument("--tables", default=",".join(AFFECTED),
                    help="comma-separated subset of " + ",".join(AFFECTED))
    args = ap.parse_args()

    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    ref = build_reference()
    print(f"Reference symbols: {len(ref):,}")
    if not ref:
        print("ERROR: empty reference -- refusing to guess. Aborting.")
        return 1

    all_unresolved: set[str] = set()
    all_junk: set[str] = set()
    total_rows = total_fixed = 0

    for table in tables:
        files = sorted(glob.glob(os.path.join(RAW_ROOT, table, "**", "*.parquet"),
                                 recursive=True))
        if not files:
            print(f"\n[{table}] no files found -- skipped")
            continue

        t_rows = t_fixed = t_files = 0
        for path in files:
            try:
                df = pd.read_parquet(path)
            except Exception as exc:
                print(f"  [skip] {os.path.basename(path)}: {exc}")
                continue
            if "ticker" not in df.columns:
                continue

            before = df["ticker"].astype(str).str.strip().str.upper()
            mapping, unresolved, junk = plan_repairs(before.unique().tolist(), ref)
            all_unresolved |= unresolved
            all_junk |= junk
            if not mapping:
                continue

            df["ticker"] = before.map(lambda t: mapping.get(t, t))
            n_fixed = int(before.isin(mapping).sum())
            t_rows += len(df)
            t_fixed += n_fixed
            t_files += 1
            if args.apply:
                _atomic_write(df, path)

        verb = "repaired" if args.apply else "would repair"
        print(f"\n[{table}] {len(files)} files, {t_files} affected")
        print(f"  rows scanned : {t_rows:,}")
        print(f"  {verb:<12}: {t_fixed:,} rows")
        total_rows += t_rows
        total_fixed += t_fixed

    print("\n" + "=" * 62)
    if args.apply:
        print(f"APPLIED: rewrote {total_fixed:,} of {total_rows:,} rows")
    else:
        print(f"DRY RUN: would rewrite {total_fixed:,} of {total_rows:,} rows")
        print("Re-run with --apply to make it permanent.")
    if all_unresolved:
        shown = sorted(all_unresolved)[:30]
        print(f"\nLeft untouched -- lookalike tickers, not provably doubled: "
              f"{len(all_unresolved)} distinct")
        print("  " + ", ".join(shown)
              + (" ..." if len(all_unresolved) > 30 else ""))
    if all_junk:
        print(f"\nNote: {len(all_junk)} distinct values over {MAX_TICKER_LEN} chars "
              f"are NOT doubled tickers.")
        print("  These are filter-menu rows from a separate pre-existing parse bug "
              "(pd.read_html")
        print("  matched Finviz's filter table instead of the results table). "
              "Out of scope here.")
        print("  e.g. " + "; ".join(sorted(x[:44] for x in all_junk)[:2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())