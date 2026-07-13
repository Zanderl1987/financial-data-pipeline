"""
Curated layer — deduplicated, compacted snapshots of every raw table.

Why this exists
---------------
Every pipeline writes a *new* dated Parquet file on each run, and the query
layer (query.py) globs **all** of them with union_by_name=True. Because the
incremental pipelines re-fetch overlapping windows, the same logical row is
written many times. Measured example: `fundamentals_annual` carried 41.5%
redundant rows across just two files. Any COUNT/AVG/SUM/return calc in
analytics/ silently computed over those duplicates.

This module collapses each raw table down to one row per natural key (keeping
the most recently fetched version) and writes a single compacted file to
`storage/curated/<table>/<table>.parquet`. query.py prefers the curated file
when it exists (see query._register_views), so the whole analytics stack reads
clean data with no API changes.

Usage
-----
    python curated.py                 # compact every table that has raw data
    python curated.py --table prices  # compact one table
    python curated.py --summary       # show row reduction per table, no writes
    python curated.py --check         # exit 1 if any table has >5% duplication

    import curated
    curated.compact("fundamentals_annual")   # -> path written
    curated.compact_all()                     # -> summary DataFrame
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query as q

CURATED_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "storage", "curated"
)

# Columns that are pipeline bookkeeping, never part of a natural key, and not
# meaningful for "is this the same row" comparisons.
_BOOKKEEPING = {"fetched_at", "year", "month"}

# ---------------------------------------------------------------------------
# Natural-key registry
# ---------------------------------------------------------------------------
# For a table listed here, dedup keeps one row per key tuple — the row with the
# newest `fetched_at` (a later fetch reflects a restatement/correction). Tables
# absent from this map fall back to FULL-ROW dedup: identical rows re-fetched on
# later runs collapse to one, which already removes the bulk of the redundancy
# without risking data loss from a wrong key guess.
KEYS: dict[str, list[str]] = {
    # Equity / ETF prices — one bar per symbol per day
    "prices":                 ["symbol", "date"],
    "tiingo_prices":          ["symbol", "date"],
    "sector_etfs":            ["symbol", "date"],
    "schwab_quotes":          ["symbol"],            # snapshot table
    "futures":                ["symbol", "date"],
    # FRED-style series — one value per series per date
    "macro":                  ["series_id", "date"],
    "commodities":            ["series_id", "date"],
    "imf_commodities":        ["series_id", "date"],
    "metals_spot":            ["series_id", "date"],
    # EIA gas tables are wide (one row per region/product/price_type per date)
    "gas_spot":               ["date", "series", "price_type", "product"],
    "gas_retail":             ["date", "duoarea", "product", "price_type"],
    # Fundamentals — one fact per company/metric/period/form/unit
    "fundamentals_annual":    ["cik", "metric", "period_end", "fiscal_period", "form", "unit"],
    "fundamentals_quarterly": ["cik", "metric", "period_end", "fiscal_period", "form", "unit"],
    # Events / disclosures
    "earnings_calendar":      ["symbol", "date"],
    "insider_transactions":   ["id"],
    "institutional_holdings": ["cik", "cusip", "filed_date", "put_call"],
    "finnhub_recommendations":["symbol", "period"],
    "sec_ftd":                ["settlement_date", "cusip"],
    "finra_short_interest":   ["symbol", "settlement_date"],
    "cot":                    ["market", "date"],
    # Options — one contract quote per symbol/expiry/strike/type/date
    "options_history":        ["symbol", "expiration_date", "strike_price", "contract_type", "date"],
    "synthetic_options":      ["symbol", "expiration_date", "strike_price", "contract_type", "vol_method", "model", "date"],
    # Dividends
    "dividends":              ["symbol", "ex_date"],
    # Fed sentiment — one doc per RSS link
    "fed_speeches":           ["doc_id"],
    "fed_sentiment":          ["doc_id"],
    # Real estate
    "fhfa_hpi":               ["hpi_type", "hpi_flavor", "frequency", "level", "place_id", "date"],
    "zillow_zhvi":            ["region_id", "date"],
    "zillow_zori":            ["region_id", "date"],
    # Shipping / logistics
    "shipping_gscpi":         ["date"],
    "shipping_freight_ppi":   ["series_id", "date"],
    # EIA refinery activity / crude trade
    "eia_refinery_activity":  ["series_id", "date"],
    "eia_crude_trade":        ["series_id", "date"],
    # TSA checkpoint travel volumes
    "tsa_checkpoint":         ["date"],
}
# NOTE: tables that share a storage directory (treasury_tic_*, google_trends_*,
# reddit_*) are split by filename-prefix globs in query.CATALOG, so each raw
# view — and therefore each curated snapshot — already sees only its own files.


def _curated_path(table: str) -> str:
    return os.path.join(CURATED_ROOT, table, f"{table}.parquet").replace("\\", "/")


class _raw_reads:
    """
    Context manager that forces query.py to read the RAW dated-file globs.

    Compaction must always source from raw — never from a (possibly stale or
    previously mis-keyed) curated snapshot — so that re-running curated.py
    rebuilds cleanly from the ground truth and is genuinely idempotent.
    """

    def __enter__(self):
        self._prev = q.USE_CURATED
        q.USE_CURATED = False
        q.reload()
        return self

    def __exit__(self, *exc):
        q.USE_CURATED = self._prev
        q.reload()
        return False


def _dedup_subset(table: str, df: pd.DataFrame) -> list[str]:
    """
    Return the column list to dedup on.

    A configured natural key is used only when EVERY one of its columns is
    present — a partial key (e.g. dropping price_type from a wide EIA table)
    would be too coarse and silently merge distinct rows. When the key doesn't
    fully match the data, fall back to full-row dedup, which only removes exact
    re-fetched duplicates and can never lose a genuinely distinct row.
    """
    key = KEYS.get(table)
    if key and all(c in df.columns for c in key):
        return key
    return [c for c in df.columns if c not in _BOOKKEEPING]


def _sort_recency(df: pd.DataFrame) -> pd.DataFrame:
    """Sort so the freshest version of a key sorts last (kept by keep='last')."""
    order = [c for c in ("fetched_at", "filed", "filing_date", "filed_date") if c in df.columns]
    if not order:
        return df
    return df.sort_values(order, kind="stable")


def dedup(table: str, df: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate a raw DataFrame on its natural key, keeping the latest version."""
    if df.empty:
        return df
    subset = _dedup_subset(table, df)
    out = _sort_recency(df).drop_duplicates(subset=subset, keep="last")
    return out.reset_index(drop=True)


def compact(table: str) -> "str | None":
    """
    Read all raw files for `table`, dedup, and write the curated snapshot.

    Returns the curated file path, or None if the table has no raw data.
    """
    if table not in q.CATALOG:
        raise ValueError(f"Unknown table '{table}'. Available: {sorted(q.CATALOG)}")

    with _raw_reads():
        raw = q.load(table)
    if raw.empty:
        return None

    clean = dedup(table, raw)
    out_path = _curated_path(table)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    clean.to_parquet(out_path, index=False, compression="snappy")
    q.reload()
    return out_path


def compact_all(tables: "list[str] | None" = None, verbose: bool = True) -> pd.DataFrame:
    """
    Compact every table (or a given subset) that has raw data.

    Returns a summary DataFrame: table | raw_rows | curated_rows | removed | pct_removed.
    """
    names = tables if tables is not None else list(q.CATALOG)
    rows = []
    with _raw_reads():
        for name in names:
            try:
                raw = q.load(name)
            except Exception as e:  # noqa: BLE001 — surface, keep going
                if verbose:
                    print(f"  {name:28s} ERROR {str(e)[:50]}")
                continue
            if raw.empty:
                continue
            clean = dedup(name, raw)
            out_path = _curated_path(name)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            clean.to_parquet(out_path, index=False, compression="snappy")

            removed = len(raw) - len(clean)
            pct = round(100 * removed / len(raw), 1) if len(raw) else 0.0
            rows.append({
                "table": name,
                "raw_rows": len(raw),
                "curated_rows": len(clean),
                "removed": removed,
                "pct_removed": pct,
            })
            if verbose:
                flag = "  <-- dupes" if pct >= 5 else ""
                print(f"  {name:28s} {len(raw):>10,} -> {len(clean):>10,}  (-{pct:>4.1f}%){flag}")

    # _raw_reads.__exit__ reloads views; curated files now take precedence
    return pd.DataFrame(rows)


def summary(tables: "list[str] | None" = None) -> pd.DataFrame:
    """Dry-run: report duplication per table without writing curated files."""
    names = tables if tables is not None else list(q.CATALOG)
    rows = []
    with _raw_reads():
        loaded = {}
        for name in names:
            try:
                df = q.load(name)
            except Exception:
                continue
            if not df.empty:
                loaded[name] = df
    for name, raw in loaded.items():
        clean = dedup(name, raw)
        removed = len(raw) - len(clean)
        rows.append({
            "table": name,
            "raw_rows": len(raw),
            "curated_rows": len(clean),
            "removed": removed,
            "pct_removed": round(100 * removed / len(raw), 1) if len(raw) else 0.0,
            "keyed": name in KEYS,
        })
    df = pd.DataFrame(rows)
    return df.sort_values("pct_removed", ascending=False).reset_index(drop=True) if not df.empty else df


def main() -> int:
    ap = argparse.ArgumentParser(description="Compact raw tables into deduplicated curated snapshots.")
    ap.add_argument("--table", help="compact a single table")
    ap.add_argument("--summary", action="store_true", help="report duplication, write nothing")
    ap.add_argument("--check", action="store_true", help="exit 1 if any table has >5%% duplication")
    args = ap.parse_args()

    if args.summary or args.check:
        df = summary([args.table] if args.table else None)
        if df.empty:
            print("No tables with raw data found.")
            return 0
        print(df.to_string(index=False))
        if args.check:
            bad = df[df["pct_removed"] > 5.0]
            if not bad.empty:
                print(f"\n{len(bad)} table(s) exceed 5% duplication. Run `python curated.py` to compact.")
                return 1
        return 0

    if args.table:
        path = compact(args.table)
        print(f"Wrote {path}" if path else f"No raw data for '{args.table}'.")
        return 0

    print("Compacting all tables with raw data...\n")
    df = compact_all()
    if df.empty:
        print("No tables with raw data found.")
        return 0
    total_removed = int(df["removed"].sum())
    total_raw = int(df["raw_rows"].sum())
    print(f"\nDone. {len(df)} tables compacted, {total_removed:,} duplicate rows removed "
          f"({100*total_removed/total_raw:.1f}% of {total_raw:,}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
