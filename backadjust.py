#!/usr/bin/env python3
"""
Undo Schwab's special-dividend back-adjustment in `prices`.

THE PROBLEM
-----------
Schwab back-adjusts deep price history for special dividends SUBTRACTIVELY:

    schwab_close(t) = raw_close(t) - (sum of special dividends with ex-date > t)

Confirmed by cross-validating against yfinance on 2026-08-30. For COST every
row in a window differs from yfinance's unadjusted close by the same constant
to the cent: 39 through 2009, 32 after 2015-06 (= the 7 + 10 + 15 specials
still to come), 0 after 2024-06 (none left). KO, which pays no specials, has
no offset at all.

Wherever those future specials exceed the old nominal price the stored value
goes NEGATIVE -- COST 1986-07-09 sits at -28.31 with 1.1M real shares traded.
That is genuine history in an unusual convention, NOT corruption. It also
silently breaks every return computed off `close`, because a subtractive
adjustment preserves differences but not ratios, and it violates this repo's
stated split-only convention (see analytics.technical._split_only_adjust).

THE CORRECTION
--------------
The offset is a step function that only changes on special-dividend dates and
decays to zero at the present, so it is recoverable empirically rather than by
reconstructing dividend metadata (which did not reproduce it exactly -- COST's
1999 offset is 39, not the 44 its full special history implies).

For each affected symbol we fetch yfinance's unadjusted close, take
`offset(t) = yfinance_close(t) - schwab_close(t)`, compress the result into
constant-offset date ranges, and store them. `curated.py` then adds the offset
back when compacting, so `storage/raw/` keeps whatever Schwab actually returned
and the correction is reproducible and auditable.

Dates earlier than yfinance's coverage inherit the earliest observed offset,
which is correct: the offset can only change at a special-dividend date, and
any such date before yfinance's history would already be included in it.

Usage:
  python backadjust.py --detect            # find affected symbols, write report
  python backadjust.py --build             # build the offset table
  python backadjust.py --build-close-corrections   # negative-price close fix
  python backadjust.py --verify            # re-check corrected prices vs yfinance
"""

import argparse
import datetime
import os
import time

import pandas as pd

import query as q
from storage_utils import write_partitioned

OUT_DIR = os.path.join("storage", "raw", "price_backadjust")

# An offset below this is noise (rounding, stale splits), not a real
# back-adjustment. COST's is 39; the smallest real ones seen are ~$1.
MIN_OFFSET = 0.01
# Treat a symbol as affected when this share of compared rows shows an offset.
MIN_AFFECTED_FRAC = 0.20
# A back-adjustment offset is PIECEWISE CONSTANT -- it only moves on a special
# dividend, so a symbol should show a handful of steps across decades. A symbol
# whose difference drifts continuously is NOT back-adjusted; its difference is
# multiplicative (a split-adjustment mismatch) or it is ticker reuse. Correcting
# those additively would corrupt them, so they are rejected and reported instead.
# Observed after smoothing: PCAR resolves to 13 steps (one per annual special),
# COST to a handful; AA produced 7,678, i.e. a continuously moving ratio.
MAX_STEPS = 60
REQUEST_PAUSE = 0.6


def _yf_history(symbol: str) -> pd.DataFrame:
    """Unadjusted daily closes from yfinance, or an empty frame."""
    import yfinance as yf
    try:
        h = yf.Ticker(symbol).history(period="max", auto_adjust=False)
    except Exception as exc:
        print(f"    {symbol}: yfinance error {str(exc)[:60]}")
        return pd.DataFrame()
    if h.empty or "Close" not in h.columns:
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": pd.to_datetime(h.index).tz_localize(None).strftime("%Y-%m-%d"),
        "ref_close": h["Close"].astype(float).values,
    })
    return out[out["ref_close"] > 0]


def detect_candidates() -> pd.DataFrame:
    """
    Symbols whose stored prices differ from the in-store yfinance table by a
    stable non-zero amount. Cheap first pass -- no network.
    """
    return q.sql(f"""
        SELECT p.symbol AS symbol,
               COUNT(*) AS n_compared,
               MEDIAN(y.close - p.close) AS median_offset,
               SUM(CASE WHEN ABS(y.close - p.close) > {MIN_OFFSET}
                        THEN 1 ELSE 0 END) AS n_offset
        FROM prices p
        JOIN yfinance_universe_prices y
          ON p.symbol = y.symbol AND p.date = y.date
        WHERE p.close IS NOT NULL AND y.close > 0
        GROUP BY p.symbol
        HAVING ABS(MEDIAN(y.close - p.close)) > {MIN_OFFSET}
    """)


def negative_price_symbols() -> list:
    """
    Symbols carrying a non-positive price. These are back-adjusted by
    definition and must be corrected even without an in-store reference.
    """
    df = q.sql("""
        SELECT DISTINCT symbol FROM prices
        WHERE close <= 0 OR open <= 0 OR high <= 0 OR low <= 0
    """)
    return df["symbol"].tolist()


def _compress(offsets: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse a per-date offset series into constant-offset date ranges.

    A rolling median runs first. Single-day blips are common where our close
    and the reference disagree for one session (a late correction, a halt), and
    unsmoothed they shatter one real step into many -- PCAR shows 16.90 for
    years with a lone 16.64 in the middle, and COST fragmented into 65 runs
    before smoothing. The median is chosen over a mean so a genuine step edge
    stays sharp instead of being ramped across the window.
    """
    offsets = offsets.sort_values("date").reset_index(drop=True)
    offsets["offset"] = (offsets["offset"]
                         .rolling(5, center=True, min_periods=1).median())
    offsets["r"] = offsets["offset"].round(2)
    grp = (offsets["r"] != offsets["r"].shift()).cumsum()
    out = offsets.groupby(grp).agg(
        start_date=("date", "first"),
        end_date=("date", "last"),
        offset=("r", "first"),
        n_days=("date", "size"),
    ).reset_index(drop=True)
    # Single-day blips are quote noise, not adjustment steps.
    return out[(out["n_days"] > 1) | (out["offset"].abs() > MIN_OFFSET)]


def build_offsets(symbols: list, verbose: bool = True) -> pd.DataFrame:
    """Derive the offset step function for each symbol against yfinance."""
    rows, rejected = [], []
    for i, sym in enumerate(symbols, 1):
        time.sleep(REQUEST_PAUSE)
        ref = _yf_history(sym)
        if ref.empty:
            if verbose:
                print(f"  [{i}/{len(symbols)}] {sym}: no yfinance history")
            continue
        ours = q.sql(f"""
            SELECT date, close FROM prices
            WHERE symbol = '{sym}' AND close IS NOT NULL ORDER BY date
        """)
        if ours.empty:
            continue
        m = ours.merge(ref, on="date", how="inner")
        if m.empty:
            if verbose:
                print(f"  [{i}/{len(symbols)}] {sym}: no overlapping dates")
            continue
        m["offset"] = (m["ref_close"] - m["close"]).round(4)
        frac = float((m["offset"].abs() > MIN_OFFSET).mean())
        if frac < MIN_AFFECTED_FRAC:
            if verbose:
                print(f"  [{i}/{len(symbols)}] {sym}: clean ({frac:.0%} offset rows)")
            continue
        steps = _compress(m[["date", "offset"]])
        if len(steps) > MAX_STEPS:
            # Not an additive back-adjustment -- see MAX_STEPS.
            rejected.append({
                "symbol": sym, "n_steps": len(steps), "n_compared": len(m),
                "median_offset": round(float(m["offset"].median()), 4),
                "median_ratio": round(float(
                    (m["ref_close"] / m["close"].replace(0, pd.NA)).median()), 4),
            })
            if verbose:
                print(f"  [{i}/{len(symbols)}] {sym}: REJECTED "
                      f"({len(steps)} steps -- offset is not piecewise constant)")
            continue
        steps.insert(0, "symbol", sym)
        rows.append(steps)
        if verbose:
            print(f"  [{i}/{len(symbols)}] {sym}: {len(steps)} step(s), "
                  f"offsets {sorted(steps['offset'].unique())[:5]}")
    if rejected:
        rej = pd.DataFrame(rejected)
        os.makedirs(OUT_DIR, exist_ok=True)
        rej_path = os.path.join(OUT_DIR, "REJECTED_not_piecewise_constant.csv")
        rej.to_csv(rej_path, index=False)
        print()
        print(f"  {len(rej)} symbol(s) rejected "
              f"(offset not piecewise constant) -> {rej_path}")
        print("  These need separate classification (reverse split / ticker reuse); "
              "they are NOT corrected.")
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    out["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return out


def write_offsets(df: pd.DataFrame) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d")
    return write_partitioned(df, OUT_DIR, f"price_backadjust_{stamp}.parquet")


def build_close_corrections(symbols: list, verbose: bool = True) -> pd.DataFrame:
    """
    Per-date close-replacement table for symbols whose raw Schwab close is
    negative (the rejecteds whose split-adjustment went too far back). On any
    date where Schwab's stored close <= 0 and yfinance has a bar, the corrected
    close is yfinance's unadjusted close, wholesale.

    Read from RAW (not curated): `curated.py` already applies this correction,
    so reading the corrected table would find nothing left to fix. Reads the
    dated raw globs via query.py with the curated flag toggled off, mirrors the
    construction used in the 2026-09-01 session, and is idempotent -- re-running
    it reproduces close_corrections.parquet exactly.

    Symbols whose negative prices sit entirely before yfinance's coverage get no
    correction rows (CCU, INGR): there is no reference to fix against.
    """
    prev = q.USE_CURATED
    q.USE_CURATED = False
    q.reload()
    try:
        rows = []
        for i, sym in enumerate(symbols, 1):
            time.sleep(REQUEST_PAUSE)
            ref = _yf_history(sym)
            if ref.empty:
                if verbose:
                    print(f"  [{i}/{len(symbols)}] {sym}: no yfinance history")
                continue
            ours = q.sql(f"""
                SELECT CAST(date AS VARCHAR) AS date, close FROM prices
                WHERE symbol = '{sym}' AND close IS NOT NULL ORDER BY date
            """)
            if ours.empty:
                continue
            m = ours.merge(ref, on="date", how="inner")
            bad = m[m["close"] <= 0]
            if bad.empty:
                if verbose:
                    print(f"  [{i}/{len(symbols)}] {sym}: no negative closes")
                continue
            rows.append(pd.DataFrame({
                "symbol": sym,
                "date": bad["date"].astype(str),
                "corrected_close": bad["ref_close"].round(4),
            }))
            if verbose:
                print(f"  [{i}/{len(symbols)}] {sym}: {len(bad)} corrected date(s)")
    finally:
        q.USE_CURATED = prev
        q.reload()
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True).drop_duplicates(
        subset=["symbol", "date"], keep="first").reset_index(drop=True)


def rejected_negative_symbols() -> list:
    """
    Symbols that BOTH carry a non-positive stored close in raw prices AND are
    in the rejected-not-piecewise-constant list. The rejected list is the
    scope for the close correction: those symbols' ratio to yfinance is ~1
    (same security, multiplicative mismatch), whereas a negative close outside
    it (SVA, GRIN, ...) is ticker reuse and must NOT be "corrected".
    """
    rej_path = os.path.join(OUT_DIR, "REJECTED_not_piecewise_constant.csv")
    if not os.path.exists(rej_path):
        return []
    rejected = set(pd.read_csv(rej_path)["symbol"])

    prev = q.USE_CURATED
    q.USE_CURATED = False
    q.reload()
    try:
        df = q.sql("SELECT DISTINCT symbol FROM prices WHERE close <= 0")
        negs = df["symbol"].tolist()
    finally:
        q.USE_CURATED = prev
        q.reload()
    return sorted(sym for sym in negs if sym in rejected)


def write_close_corrections(df: pd.DataFrame) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "close_corrections.parquet")
    df.to_parquet(path, index=False)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--detect", action="store_true",
                    help="report candidate symbols (no network)")
    ap.add_argument("--build", action="store_true",
                    help="derive and write the offset table")
    ap.add_argument("--build-close-corrections", action="store_true",
                    help="derive and write per-date close-correction table "
                         "(negative-price symbol fix)")
    ap.add_argument("--verify", action="store_true",
                    help="re-check corrected prices against yfinance")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap how many symbols to process")
    args = ap.parse_args()

    if args.detect or args.build:
        cands = detect_candidates()
        negs = negative_price_symbols()
        symbols = sorted(set(cands["symbol"]) | set(negs))
        print(f"candidates from yfinance comparison : {len(cands):,}")
        print(f"symbols with a non-positive price   : {len(negs):,}")
        print(f"union to correct                    : {len(symbols):,}")
        if args.detect:
            if not cands.empty:
                top = cands.reindex(
                    cands["median_offset"].abs().sort_values(ascending=False).index)
                print("\nlargest median offsets:")
                print(top.head(20).to_string(index=False))
            return 0
        if args.limit:
            symbols = symbols[:args.limit]
        print(f"\nbuilding offsets for {len(symbols):,} symbols...")
        out = build_offsets(symbols)
        if out.empty:
            print("no offsets derived")
            return 1
        path = write_offsets(out)
        print(f"\n-> {path}  ({len(out):,} step rows, "
              f"{out['symbol'].nunique():,} symbols)")
        return 0

    if args.verify:
        from curated import load_backadjust_offsets, load_close_corrections
        off = load_backadjust_offsets()
        cc = load_close_corrections()
        print(f"offset table: {len(off):,} step rows, "
              f"{off['symbol'].nunique():,} symbols"
              if not off.empty else "offset table is empty")
        print(f"close corrections: {len(cc):,} rows, "
              f"{cc['symbol'].nunique():,} symbols"
              if not cc.empty else "close corrections are empty")
        return 0

    if args.build_close_corrections:
        symbols = rejected_negative_symbols()
        print(f"rejected symbols with a non-positive raw close: {len(symbols):,}")
        if args.limit:
            symbols = symbols[:args.limit]
        print(f"\nbuilding close corrections for {len(symbols):,} symbols...")
        out = build_close_corrections(symbols)
        if out.empty:
            print("no corrections derived")
            return 1
        path = write_close_corrections(out)
        print(f"\n-> {path}  ({len(out):,} rows, "
              f"{out['symbol'].nunique():,} symbols)")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
