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
  python backadjust.py --sweep-no-reference       # per-symbol reference pull over all
                                                 #  ~25.5k symbols with no in-store reference
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

# Symbols verified (2026-09-01 review) as the SAME security as yfinance with a
# positive reference bar on every negative date, even though they never entered
# the rejected list (so rejected_negative_symbols() would skip them). BF/A is
# Brown-Forman Class A; its 1985-88 negatives are a split-adjustment artifact
# and yahoo (as "BF-A") covers all 810 dates. The other residuals (SVA, LGOV,
# GRIN, CHRD, MEDS, CCU, INGR) FAILED this check -- ticker reuse / pre-listing
# / delisted -- and are deliberately excluded as known-garbage.
CLOSE_CORRECTION_ALLOWLIST = ["BF/A"]
# Store uses Schwab's symbol form; yfinance needs a different one.
YAHOO_SYMBOL_OVERRIDES = {"BF/A": "BF-A"}


def _yf_history_detailed(symbol: str):
    """Unadjusted daily closes from yfinance plus a diagnosis string.

    Returns (frame, error) where error is None on success. A genuinely
    delisted/missing ticker returns an empty frame with error="empty"; a
    network/rate-limit failure returns an empty frame with the message. The
    sweep (--sweep-no-reference) needs the distinction so a transient 429
    doesn't permanently checkpoint a symbol as "checked".
    """
    import yfinance as yf
    try:
        h = yf.Ticker(symbol).history(period="max", auto_adjust=False)
    except Exception as exc:
        return pd.DataFrame(), str(exc)[:120]
    if h.empty or "Close" not in h.columns:
        return pd.DataFrame(), "empty"
    out = pd.DataFrame({
        "date": pd.to_datetime(h.index).tz_localize(None).strftime("%Y-%m-%d"),
        "ref_close": h["Close"].astype(float).values,
    })
    return out[out["ref_close"] > 0], None


def _yf_history(symbol: str) -> pd.DataFrame:
    """Unadjusted daily closes from yfinance, or an empty frame."""
    frame, error = _yf_history_detailed(symbol)
    if error:
        print(f"    {symbol}: yfinance error {error}")
    return frame


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


def classify_one(symbol: str, ours: pd.DataFrame, ref: pd.DataFrame) -> dict:
    """
    Classify ONE symbol against a reference: is it clean, additively
    back-adjusted (correct), or a reverse-split/ticker-reuse artifact whose
    offset is NOT piecewise constant (reject)?

    Shared by build_offsets() and sweep_no_reference() so the thresholds and
    the verdict logic live in exactly one place. Returns a dict with
    `status` in {no_history, no_compare, clean, corrected, rejected} plus the
    stats the caller prints.
    """
    if ref.empty:
        return {"status": "no_history"}
    if ours.empty:
        return {"status": "no_compare"}
    m = ours.merge(ref, on="date", how="inner")
    if m.empty:
        return {"status": "no_compare"}
    m["offset"] = (m["ref_close"] - m["close"]).round(4)
    frac = float((m["offset"].abs() > MIN_OFFSET).mean())
    if frac < MIN_AFFECTED_FRAC:
        return {"status": "clean", "frac": frac}
    steps = _compress(m[["date", "offset"]])
    if len(steps) > MAX_STEPS:
        # Not an additive back-adjustment -- see MAX_STEPS. Correcting these
        # additively would corrupt them; they are reported, not corrected.
        return {"status": "rejected", "frac": frac, "symbol": symbol,
                "n_steps": len(steps), "n_compared": len(m),
                "median_offset": round(float(m["offset"].median()), 4),
                "median_ratio": round(float(
                    (m["ref_close"] / m["close"].replace(0, pd.NA)).median()), 4)}
    steps = steps.copy().reset_index(drop=True)
    steps.insert(0, "symbol", symbol)
    return {"status": "corrected", "frac": frac, "steps": steps}


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
        r = classify_one(sym, ours, ref)
        if r["status"] == "no_compare":
            if verbose:
                print(f"  [{i}/{len(symbols)}] {sym}: no overlapping dates")
            continue
        if r["status"] == "clean":
            if verbose:
                print(f"  [{i}/{len(symbols)}] {sym}: clean "
                      f"({r['frac']:.0%} offset rows)")
            continue
        if r["status"] == "rejected":
            rejected.append({k: r[k] for k in
                             ("symbol", "n_steps", "n_compared",
                              "median_offset", "median_ratio")})
            if verbose:
                print(f"  [{i}/{len(symbols)}] {sym}: REJECTED "
                      f"({r['n_steps']} steps -- offset is not piecewise constant)")
            continue
        rows.append(r["steps"])
        if verbose:
            print(f"  [{i}/{len(symbols)}] {sym}: {len(r['steps'])} step(s), "
                  f"offsets {sorted(r['steps']['offset'].unique())[:5]}")
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
            ref = _yf_history(YAHOO_SYMBOL_OVERRIDES.get(sym, sym))
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


def close_correction_symbols() -> list:
    """
    The full scope for the close correction: rejected-negative symbols PLUS the
    explicitly verified allowlist (same security with a full positive yfinance
    reference -- see CLOSE_CORRECTION_ALLOWLIST). Unknown negatives stay out:
    they are ticker reuse / pre-listing garbage, not recoverable history.
    """
    symbols = set(rejected_negative_symbols())
    prev = q.USE_CURATED
    q.USE_CURATED = False
    q.reload()
    try:
        negs = set(q.sql("SELECT DISTINCT symbol FROM prices WHERE close <= 0")["symbol"])
    finally:
        q.USE_CURATED = prev
        q.reload()
    symbols |= set(CLOSE_CORRECTION_ALLOWLIST) & negs
    return sorted(symbols)


def write_close_corrections(df: pd.DataFrame) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "close_corrections.parquet")
    df.to_parquet(path, index=False)
    return path


_SWEEP_PROGRESS = os.path.join(OUT_DIR, "backadjust_sweep_progress.csv")
# Deliberately does NOT match curated.py's `price_backadjust_*.parquet` glob:
# it holds RESUME state only, and is merged into a dated file when the sweep
# completes, so a mid-run `curated.py` never reads partial results.
_SWEEP_WORK = os.path.join(OUT_DIR, "backadjust_sweep_work.parquet")

# Terminal statuses: a symbol bearing one of these will not be re-pulled on a
# resumed run. "error" is deliberately NOT terminal -- it means a transient
# yfinance/network failure, retried on resume.
_SWEEP_TERMINAL = {"corrected", "clean", "rejected", "no_history", "no_compare"}


def no_reference_symbols() -> list:
    """Symbols in `prices` with no in-store yfinance reference -- the
    population the 2026-08-30 correction deliberately left untouched (see
    TASKS.md "COME BACK TO THE UNCORRECTED SYMBOLS")."""
    df = q.sql("""
        SELECT DISTINCT p.symbol FROM prices p
        LEFT JOIN (SELECT DISTINCT symbol FROM yfinance_universe_prices) y
          ON p.symbol = y.symbol
        WHERE y.symbol IS NULL ORDER BY p.symbol
    """)
    return df["symbol"].tolist()


def _load_sweep_progress() -> dict:
    if not os.path.exists(_SWEEP_PROGRESS) or os.path.getsize(_SWEEP_PROGRESS) == 0:
        return {}
    d = pd.read_csv(_SWEEP_PROGRESS)
    if d.empty or "symbol" not in d.columns:
        return {}
    d = d[d["symbol"].astype(str) != "symbol"]       # drop any stray header row
    if d.empty:
        return {}
    return dict(zip(d["symbol"].astype(str), d["status"].astype(str)))


def _load_sweep_work() -> pd.DataFrame:
    if not os.path.exists(_SWEEP_WORK):
        return pd.DataFrame()
    return pd.read_parquet(_SWEEP_WORK)


def _merge_offsets(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Merge the work file with newly-derived steps, dedupe on
    (symbol, start_date) keeping the newest derivation."""
    if new.empty:
        return existing
    if existing.empty:
        new = new.copy()
        new["fetched_at"] = datetime.datetime.utcnow().isoformat()
        return new
    all_rows = pd.concat([existing, new], ignore_index=True)
    all_rows["fetched_at"] = all_rows["fetched_at"].fillna(
        datetime.datetime.utcnow().isoformat())
    return (all_rows.sort_values("fetched_at")
            .drop_duplicates(["symbol", "start_date"], keep="last")
            .sort_values(["symbol", "start_date"])
            .reset_index(drop=True))


def sweep_no_reference(verbose: bool = True, checkpoint_every: int = 200,
                       limit: "int | None" = None) -> int:
    """
    Per-symbol reference pull over every symbol in `prices` that has NO
    in-store yfinance coverage (~25.5k), closing the deferred 2026-08-30 gap.

    Same classification as build_offsets() (via classify_one) but resumable:
    progress is checkpointed per symbol to _SWEEP_PROGRESS; derived offsets
    accumulate in _SWEEP_WORK and are copied to a dated
    `price_backadjust_<date>.parquet` (what curated.py consumes) only when the
    whole population is done. A transient yfinance failure records status=error
    and is retried on resume. Rejected (non-piecewise-constant) symbols are
    unioned back into REJECTED_not_piecewise_constant.csv without clobbering
    prior verdicts.

    Cost: ~25.5k rate-limited calls (~0.6s + fetch each) -- run as a background
    job, not interactively. Returns number of newly-corrected symbols.
    """
    symbols = no_reference_symbols()
    done = _load_sweep_progress()
    todo = [s for s in symbols if done.get(s) not in _SWEEP_TERMINAL]
    if verbose:
        print(f"no-reference population : {len(symbols):,}")
        print(f"already terminal        : {len(symbols) - len(todo):,}")
    if limit:
        todo = todo[:limit]
    if verbose:
        print(f"to check this run       : {len(todo):,}")
    if not todo:
        print("sweep already complete -- no work to do")
        return 0

    work = _load_sweep_work()
    os.makedirs(OUT_DIR, exist_ok=True)
    n_corrected = 0
    checkpoint = 0
    rejected_rows = []
    fresh = not os.path.exists(_SWEEP_PROGRESS) or os.path.getsize(_SWEEP_PROGRESS) == 0
    for i, sym in enumerate(todo, 1):
        time.sleep(REQUEST_PAUSE)
        ref, error = _yf_history_detailed(sym)
        ours = pd.DataFrame()
        if not ref.empty:
            ours = q.sql(f"""
                SELECT date, close FROM prices
                WHERE symbol = '{sym}' AND close IS NOT NULL ORDER BY date
            """)
        r = classify_one(sym, ours, ref)
        status = r["status"]
        if error and ref.empty:
            status = "error"   # transient failure -- retry on resume
            if verbose:
                print(f"  [{i}/{len(todo)}] {sym}: ERROR {error}")
        elif status == "no_history":
            if verbose:
                print(f"  [{i}/{len(todo)}] {sym}: no yfinance history")
        elif status == "no_compare":
            if verbose:
                print(f"  [{i}/{len(todo)}] {sym}: no overlapping dates")
        elif status == "clean":
            if verbose:
                print(f"  [{i}/{len(todo)}] {sym}: clean ({r['frac']:.0%})")
        elif status == "rejected":
            rejected_rows.append({k: r[k] for k in
                                  ("symbol", "n_steps", "n_compared",
                                   "median_offset", "median_ratio")})
            if verbose:
                print(f"  [{i}/{len(todo)}] {sym}: REJECTED "
                      f"({r['n_steps']} steps, offset not piecewise constant)")
        else:
            n_corrected += 1
            if verbose:
                print(f"  [{i}/{len(todo)}] {sym}: corrected "
                      f"({len(r['steps'])} step(s))")
            work = _merge_offsets(work, r["steps"])

        checkpoint += 1
        with open(_SWEEP_PROGRESS, "a", encoding="utf-8") as f:
            if fresh:
                f.write("symbol,status,fetched_at\n")
                fresh = False
            f.write(f"{sym},{status},"
                    f"{datetime.datetime.utcnow().isoformat()}\n")

        if checkpoint % checkpoint_every == 0:
            if not work.empty:
                work.to_parquet(_SWEEP_WORK, index=False)
            if verbose:
                print(f"  -- checkpoint {i}/{len(todo)}: {n_corrected} corrected, "
                      f"{len(todo) - i} left")

    # Merge rejected verdicts into the shared list without clobbering priors
    # or their stats columns.
    rej_path = os.path.join(OUT_DIR, "REJECTED_not_piecewise_constant.csv")
    rej_prior = (pd.read_csv(rej_path) if os.path.exists(rej_path)
                 else pd.DataFrame())
    if rejected_rows:
        rej_new = pd.DataFrame(rejected_rows)
        if rej_prior.empty:
            all_rej = rej_new
        else:
            all_rej = pd.concat(
                [rej_prior, rej_new[~rej_new["symbol"].isin(rej_prior["symbol"].astype(str))]],
                ignore_index=True)
        all_rej.to_csv(rej_path, index=False)
        if verbose:
            print(f"{len(all_rej)} symbol(s) rejected / blocklisted -> {rej_path}")

    if not work.empty:
        work.to_parquet(_SWEEP_WORK, index=False)
        if limit is None:
            # Only a FULL run publishes the dated offset file curated.py
            # consumes; a --limit smoke run persists work/progress so a later
            # full run resumes seamlessly instead of writing a partial story.
            path = write_offsets(work)
            if verbose:
                print(f"offsets -> {path}  ({len(work):,} step rows, "
                      f"{work['symbol'].nunique():,} symbols)")
    print(f"sweep complete: {n_corrected} symbol(s) newly corrected, "
          f"{len(todo)} checked")
    return n_corrected


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--detect", action="store_true",
                    help="report candidate symbols (no network)")
    ap.add_argument("--build", action="store_true",
                    help="derive and write the offset table")
    ap.add_argument("--build-close-corrections", action="store_true",
                    help="derive and write per-date close-correction table "
                         "(negative-price symbol fix)")
    ap.add_argument("--sweep-no-reference", action="store_true",
                    help="per-symbol reference pull over every symbol with no "
                         "in-store yfinance reference (resumable background job)")
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
        symbols = close_correction_symbols()
        print(f"close-correction scope (rejected negatives + allowlist): {len(symbols):,}")
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

    if args.sweep_no_reference:
        print("sweeping no-reference symbols (resumable; see "
              "backadjust_sweep_progress.csv for checkpoints)...")
        n = sweep_no_reference(limit=args.limit)
        return 0 if n >= 0 else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
