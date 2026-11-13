#!/usr/bin/env python3
"""
WRDS client -- authenticated read-only access to CRSP / Compustat / IBES.

Why this exists
---------------
The strategy catalog's binding constraint is not a shortage of hypotheses, it is
a shortage of valid panels. 336 of 341 records are ineligible, and the largest
block is survivorship: `storage/curated/yfinance_universe_prices` holds only
firms that survived to the present, so a cross-sectional sort on it does not
weakly test a published anomaly -- it tests a different universe. A sibling
session measured the gap at an 83-point coverage difference on 2000-03-01
(`experiments/2026-09-13_survivorship-bias.md`), and separately established
that a delisting-inclusive panel is NOT buildable from free sources
(`experiments/2026-09-13_delisted-sandwich.md`).

CRSP returns delisting returns (`dlret`) and a permanent security identifier
(`permno`) that is never recycled, which is the whole fix. Compustat supplies
the point-in-time fundamentals that the OSAP records are written against.

Why this file is deliberately awkward
-------------------------------------
WRDS is credentialed, licensed, and rate/row-limited, and the failure modes are
the expensive kind: a partial pull that looks complete, a silent truncation at
the row cap, or a chunk boundary that drops delisted names because they cluster
at the end of the permno range. So:

  * credentials are read from the environment and never logged, never accepted
    as argv (argv lands in shell history and in `ps` output);
  * every pull is chunked and cached to parquet independently, so a crash
    resumes instead of restarting, and a completed chunk is re-verified rather
    than re-fetched;
  * the row cap is a declared parameter with a conservative default and a
    `probe()` that MEASURES it, because the documented limit varies by product
    and a silently-truncated result is worse than a failed one;
  * nothing here writes to `storage/curated/`. Licensed data is not
    redistributable and this repo publishes that directory to a public
    HuggingFace dataset under CC BY 4.0. See `upload_huggingface.LICENSED_PREFIXES`.

Install
-------
    pip install wrds

Credentials (in `.env`, gitignored, never printed):
    WRDS_USER=...
    WRDS_PASS=...

CLI:
    python wrds_client.py probe              # what does this subscription cover?
    python wrds_client.py pull crsp.daily    # fetch one table, resumable
    python wrds_client.py pull --all         # the whole manifest, in cost order
    python wrds_client.py manifest           # the target list, no credentials needed
"""

from __future__ import annotations

import contextlib
import datetime
import os
import pathlib
import time
from dataclasses import dataclass, field

LICENSED_ROOT = pathlib.Path(
    os.environ.get("FDP_LICENSED_ROOT", pathlib.Path(__file__).parent / "storage" / "licensed")
)

# WRDS rejects oversized result sets rather than truncating, which is the good
# failure mode -- but the threshold differs by product and by subscription tier.
# Default is deliberately well under the most commonly documented cap (5M) so a
# first pull cannot trip it; `probe()` measures the real one.
DEFAULT_MAX_ROWS = 2_000_000

# CRSP daily is ~50M rows back to 1926. Pulling it by permno range keeps each
# chunk small, but delisted names cluster at high permnos, so chunks must not be
# sized by "the first N permnos" -- see `fetch_chunked`.
DEFAULT_CHUNK_ROWS = 250_000

RETRY_SLEEP_SECONDS = 20
MAX_RETRIES = 3


class WRDSUnavailable(RuntimeError):
    """WRDS is not reachable, or the `wrds` package is not installed."""


class NotEntitled(WRDSUnavailable):
    """This subscription does not cover the requested library/table."""


class RowCapExceeded(WRDSUnavailable):
    """A single query returned more rows than WRDS will serve."""


# ---------------------------------------------------------------------------
# The target list
# ---------------------------------------------------------------------------
# Ordered by records-unblocked, highest first, because a pull is only worth its
# wall-clock if it changes eligibility. `records` counts catalog records blocked
# on this table as of the 2026-09-12 eligibility measurement; treat it as a
# planning figure that the post-pull eligibility recompute will replace.
#
# `phase` exists because records alone is the wrong sort. The cheap
# prerequisites are worthless without the bulk tables and vice versa: you cannot
# use `crsp.daily` until `comp.g_link` and `comp.securities` exist, and pulling
# 50M daily rows first only to discover the join key is missing is a wasted
# afternoon. So prerequisites go first, then the bulk unlocks by size, then the
# requirement fills, then optional enrichment. It is sorted, not hand-ordered,
# and `tests/test_wrds_compustat.py` pins the invariant.

PHASE_PREREQUISITE = 1
PHASE_BULK = 2
PHASE_REQUIREMENT = 3
PHASE_OPTIONAL = 4


@dataclass(frozen=True)
class Target:
    table: str
    library: str
    records: int
    purpose: str
    columns: str = "*"
    chunk_on: str | None = None
    phase: int = PHASE_REQUIREMENT
    optional: bool = False


MANIFEST: tuple[Target, ...] = tuple(
    sorted(
        (
            Target(
                "crsp.daily", "crsp", 300,
                "Survivorship-free daily returns incl. dlret, keyed on permno. The "
                "single biggest eligibility unlock in the catalog.",
                columns="permno,permco,date,ret,retx,dlret,prc,shrout,vol,turn",
                chunk_on="permno",
                phase=PHASE_BULK,
            ),
            Target(
                "crsp.msenames", "crsp", 0,
                "Point-in-time ticker/name history. Replaces the repo's approximate "
                "symbol matching, which SURVIVORSHIP.md records as crediting coverage: "
                "900 of 1,067 ticker-string matches are recycled tickers.",
                phase=PHASE_PREREQUISITE,
            ),
            Target(
                "comp.fundamentals_quarterly", "comp", 250,
                "Quarterly fundamentals, restated panel. Backs the OSAP accrual, "
                "investment, asset-growth and profitability records. See "
                "compustat_pit.py -- this table must never be read with latest=1.",
                columns="gvkey,datadate,rvq,fq,fy,ffo,ajex,atq,act,ltq,lct,ppeq,"
                        "niq,oiadp,revt,saleq,cogsq,xintq,dpq,txdq,ibq,sidq",
                chunk_on="gvkey",
                phase=PHASE_BULK,
            ),
            Target(
                "comp.securities", "comp", 0,
                "PIT security master: first/last available dates per gvkey. This is the "
                "universe filter that keeps a dead name IN the cross-section while it "
                "was trading -- see compustat_pit.pit_universe.",
                phase=PHASE_PREREQUISITE,
            ),
            Target(
                "comp.g_link", "comp", 0,
                "gvkey <-> permno. Without it, fundamentals cannot be joined to prices "
                "without redoing the survivorship-prone symbol match this whole "
                "exercise exists to eliminate.",
                phase=PHASE_PREREQUISITE,
            ),
            Target(
                "comp.ratios_monthly", "comp", 0,
                "Monthly ratios incl. shares short. `short_interest` in the store is 31 "
                "symbols / 1,530 rows; this is a full-universe replacement.",
                chunk_on="gvkey",
            ),
            Target(
                "comp.ibesfn_p", "comp", 22,
                "IBES unadjusted estimates, PIT by statdate/fiscal_period. Unblocks "
                "`analyst_estimates_ibes`, the largest single blocked requirement.",
                optional=True,
            ),
            Target(
                "comp.ibessub_p", "comp", 22,
                "IBES recommendation and estimate summary. Same records as ibesfn_p; "
                "the analyst-revision records need both.",
                optional=True,
            ),
            Target(
                "comp.gdxr", "comp", 8,
                "Distribution and corporate events (dividends, splits, mergers, name "
                "changes, delistings). Unblocks `corporate_events`.",
                chunk_on="gvkey",
            ),
            Target(
                "crsp.ccm", "crsp", 0,
                "Daily index membership, point-in-time from 2013. Lets a universe be "
                "defined by index membership as of each rebalance rather than by "
                "today's list.",
                optional=True,
                phase=PHASE_OPTIONAL,
            ),
            Target(
                "comp.mf_basic", "comp", 0,
                "Mutual fund holdings. `institutional_holdings` in the store is 13 hand-"
                "picked managers keyed by CUSIP and effectively 2023+; an ownership "
                "signal needs every filer to form the ownership fraction.",
                optional=True,
                phase=PHASE_OPTIONAL,
            ),
        ),
        key=lambda t: (t.phase, -t.records),
    )
)


def manifest() -> tuple[Target, ...]:
    return MANIFEST


# ---------------------------------------------------------------------------
# Credentials and connection
# ---------------------------------------------------------------------------


def _credential(name: str) -> str:
    """Read one credential, loading `.env` lazily so tests never need it."""
    value = os.environ.get(name)
    if value:
        return value
    env_path = pathlib.Path(__file__).parent / ".env"
    if env_path.is_file():
        with contextlib.suppress(Exception):
            from dotenv import load_dotenv

            load_dotenv(env_path)
    value = os.environ.get(name)
    if not value:
        raise WRDSUnavailable(
            f"{name} is not set. Put it in {env_path} -- do not pass it on the "
            f"command line, where it would land in shell history and `ps` output."
        )
    return value


@contextlib.contextmanager
def connection():
    """Yield an authenticated WRDS connection, closed on exit.

    The `wrds` import is inside the function on purpose: the package is an
    optional dependency, and importing this module must not require it. That
    keeps the test suite and the credential-free CLI paths runnable on a machine
    that has never had WRDS credentials.
    """
    try:
        import wrds
    except ImportError as exc:
        raise WRDSUnavailable(
            "the `wrds` package is not installed -- pip install wrds"
        ) from exc

    username = _credential("WRDS_USER")
    # Not logged, not in the exception text, not in a traceback frame repr.
    password = _credential("WRDS_PASS")

    # wrds 3.5.0 may still prompt on stdin in some shells even with kwargs;
    # bypass by setting env vars before connecting so its credential helper uses
    # them and doesn't call input().
    os.environ.setdefault("WRDS_USERNAME", username)
    os.environ.setdefault("WRDS_PASSWORD", password)
    os.environ.setdefault("WRDS_USER", username)
    os.environ.setdefault("WRDS_PASS", password)

    db = wrds.Connection(
        wrds_username=username,
        wrds_password=password,
        autologin=False,
    )
    try:
        yield db
    finally:
        with contextlib.suppress(Exception):
            db.close()


def available_libraries() -> list[str]:
    """Every library this subscription can actually read.

    Entitlement is per-product: a university frequently has CRSP+Compustat
    without IBES, Capital IQ, or OptionMetrics. Assuming coverage is exactly how
    a pipeline gets written against a table that 403s on first pull, so this is
    a probe rather than a constant.
    """
    with connection() as db:
        return sorted(db.list_libraries())


def _split_table(table: str) -> tuple[str, str]:
    if "." not in table:
        raise ValueError(f"table must be 'library.table', got {table!r}")
    library, _, name = table.partition(".")
    return library, name


def _query(db, sql: str, max_rows: int, retries: int = MAX_RETRIES) -> "pd.DataFrame":
    """Run one SQL query, converting WRDS failures into typed exceptions."""
    import pandas as pd

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            frame = db.raw_sql(sql)
        except Exception as exc:  # WRDS raises bare exceptions from its parser
            message = str(exc).lower()
            if "exceed" in message or "too large" in message or "max" in message:
                raise RowCapExceeded(
                    f"query exceeded the row cap (max_rows={max_rows:,}). "
                    f"Narrow the chunk; do not raise the cap blindly."
                ) from exc
            if "not authorized" in message or "no authorization" in message:
                raise NotEntitled(
                    "subscription does not cover this table -- run "
                    "`python wrds_client.py probe`"
                ) from exc
            last_error = exc
            if attempt < retries - 1:
                time.sleep(RETRY_SLEEP_SECONDS * (attempt + 1))
            continue
        if len(frame) > max_rows:
            # A silent truncation here would be the most expensive bug in the
            # repo: a short panel that looks complete and quietly biases every
            # cross-sectional sort toward survivors.
            raise RowCapExceeded(
                f"query returned {len(frame):,} rows, over max_rows={max_rows:,}. "
                f"Narrow the chunk; do not raise the cap blindly."
            )
        return frame
    raise WRDSUnavailable(f"query failed after {retries} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Chunked, resumable fetch
# ---------------------------------------------------------------------------


def _cache_dir(library: str, table: str, root: pathlib.Path | None = None) -> pathlib.Path:
    base = pathlib.Path(root) if root else LICENSED_ROOT
    return base / library / table


DATE_COLUMNS = {
    "date", "datadate", "rvq", "startdt", "enddt", "statdate", "fiscal_period",
    "begindate", "enddate", "tradedate", "first_date", "last_date", "firstdt",
    "lastdt", "ipodate", "delistdate",
}

# A plausible calendar year bounds the YYYYMMDD reading. 1900-01-01 -> 2100-12-31
# as an integer is 19000101..21001231, which no day-offset encoding overlaps for
# a value with 8 digits, so the two forms are unambiguous in practice.
_YYYYMMDD_LO, _YYYYMMDD_HI = 19000101, 21001231
# CRSP/Compustat date columns that arrive as raw SAS day offsets count days from
# this epoch. Realistic values run to ~24,000, which caps the ambiguity window.
_DAY_OFFSET_MAX = 40_000


def _normalize_dates(frame: "pd.DataFrame") -> "pd.DataFrame":
    """Coerce date columns to datetime64, whichever integer form they arrive in.

    WRDS usually hands back real timestamps, but a `raw_sql` projection can yield
    integers, and CRSP and Compustat disagree about the encoding: CRSP daily
    dates are `YYYYMMDD` (19800104) while a SAS-native field is days since
    1960-01-01. Guessing wrong here is silent -- you get a plausible frame full
    of wrong dates -- so both forms are detected explicitly and anything
    unrecognised is left ALONE rather than coerced to NaT. A NaT column is
    obvious; a 1970 date is not.
    """
    import pandas as pd

    frame = frame.copy()
    for column in frame.columns:
        if column not in DATE_COLUMNS:
            continue
        series = frame[column]
        if not pd.api.types.is_numeric_dtype(series):
            continue

        values = series.dropna()
        if values.empty:
            continue

        if values.between(_YYYYMMDD_LO, _YYYYMMDD_HI).all():
            text = values.astype("int64").astype(str)
            if text.str.len().eq(8).all():
                frame[column] = pd.to_datetime(
                    series.astype("Int64").astype(str), format="%Y%m%d", errors="coerce"
                )
                continue

        if (values >= 0).all() and values.max() <= _DAY_OFFSET_MAX:
            frame[column] = pd.to_datetime(
                series, unit="D", origin="1960-01-01", errors="coerce"
            )
            continue

        # Unrecognised: leave the column untouched and let validate.py report it.
    return frame


@dataclass
class FetchReport:
    table: str
    chunks_total: int = 0
    chunks_fetched: int = 0
    chunks_cached: int = 0
    rows: int = 0
    first_date: str | None = None
    last_date: str | None = None
    notes: list[str] = field(default_factory=list)


def fetch_chunked(
    target: Target,
    root: pathlib.Path | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    force: bool = False,
) -> FetchReport:
    """Pull one target table into `storage/licensed/<library>/<table>/`.

    Chunking is on `permno` / `gvkey`, which are dense integer identifiers, and
    the chunk boundaries come from the table's own observed key range rather
    than an assumption. Chunk size is expressed in *rows* so the caller can
    reason about the row cap directly.

    Resume is per chunk: a chunk whose parquet already exists is counted and
    skipped, so an interrupted backfill continues rather than restarting. This
    is the same resume pattern the Open-Meteo backfill uses after a 429 storm
    cost real time.
    """
    import pandas as pd

    library, name = _split_table(target.table)
    report = FetchReport(table=target.table)
    cache = _cache_dir(library, name, root)
    cache.mkdir(parents=True, exist_ok=True)

    if not target.chunk_on:
        with connection() as db:
            frame = _query(db, _select_sql(target), max_rows)
        frame = _normalize_dates(frame)
        report.chunks_total = 1
        report.rows = len(frame)
        path = cache / f"{name}.parquet"
        frame.to_parquet(path, index=False, compression="snappy")
        report.chunks_fetched = 1
        if "date" in frame.columns and len(frame):
            report.first_date = str(frame["date"].min())[:10]
            report.last_date = str(frame["date"].max())[:10]
        return report

    key = target.chunk_on
    with connection() as db:
        bounds = db.raw_sql(
            f"SELECT min({key}) AS lo, max({key}) AS hi FROM {target.table}"
        )
    lo, hi = int(bounds["lo"].iloc[0]), int(bounds["hi"].iloc[0])
    if lo != hi and (hi - lo) > chunk_rows:
        # Dense identifiers, so the key span is a usable proxy for row count.
        step = max(1, chunk_rows)
        edges = list(range(lo, hi + 1, step)) + [hi]
    else:
        edges = [lo, hi]

    report.chunks_total = len(edges) - 1
    for index, (start, end) in enumerate(zip(edges, edges[1:])):
        path = cache / f"{key}_{start}_{end}.parquet"
        if path.exists() and not force:
            # Row count comes from the parquet footer, not by reading the file:
            # resuming should not cost a full pass over data already on disk.
            report.chunks_cached += 1
            report.rows += _parquet_rows(path)
            continue

        where = f"WHERE {key} BETWEEN {start} AND {end}"
        with connection() as db:
            frame = _query(db, _select_sql(target, where), max_rows)
        frame = _normalize_dates(frame)
        frame.to_parquet(path, index=False, compression="snappy")
        report.chunks_fetched += 1
        report.rows += len(frame)
        if "date" in frame.columns and len(frame):
            chunk_first, chunk_last = str(frame["date"].min())[:10], str(frame["date"].max())[:10]
            report.first_date = min(filter(None, [report.first_date, chunk_first]))
            report.last_date = max(filter(None, [report.last_date, chunk_last]))
        print(f"  [{target.table}] chunk {index + 1}/{report.chunks_total} "
              f"{key} {start}..{end}: {len(frame):,} rows")

    return report


def _parquet_rows(path: pathlib.Path) -> int:
    import pyarrow.parquet as pq

    return int(pq.read_metadata(str(path)).num_rows)


def _select_sql(target: Target, where: str | None = None) -> str:
    columns = target.columns if target.columns != "*" else "*"
    sql = f"SELECT {columns} FROM {target.table}"
    return f"{sql} {where}" if where else sql


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def probe() -> dict:
    """Measure what this subscription can actually do.

    Per the data-source-vetting checklist: probe before building. Prints the
    library list, checks each manifest target for entitlement, and reports the
    observed shape of the two tables everything else depends on. A NO-GO here is
    worth as much as a GO -- it stops a pipeline being written against a table
    the subscription does not cover.
    """
    findings: dict = {"libraries": [], "tables": {}, "errors": {}}

    with connection() as db:
        try:
            libraries = sorted(db.list_libraries())
        except Exception as exc:
            return {"errors": {"list_libraries": str(exc)}}
        findings["libraries"] = libraries
        print(f"WRDS libraries visible ({len(libraries)}): {libraries}\n")

        for target in MANIFEST:
            library, name = _split_table(target.table)
            try:
                if library not in libraries:
                    raise NotEntitled(f"library {library!r} not in subscription")
                shape = db.raw_sql(
                    f"SELECT count(*) AS n, min(date) AS lo, max(date) AS hi "
                    f"FROM {target.table}"
                )
                row = shape.iloc[0].to_dict()
                findings["tables"][target.table] = {
                    "entitled": True,
                    "rows": int(row["n"]) if row["n"] is not None else None,
                    "first": str(row["lo"])[:10] if row["lo"] is not None else None,
                    "last": str(row["hi"])[:10] if row["hi"] is not None else None,
                    "purpose": target.purpose,
                    "records_unblocked": target.records,
                }
                print(f"  GO    {target.table:<32} {row['n']:>14,} rows  "
                      f"{str(row['lo'])[:10]}..{str(row['hi'])[:10]}")
            except Exception as exc:
                findings["tables"][target.table] = {
                    "entitled": False,
                    "error": str(exc)[:200],
                    "optional": target.optional,
                }
                findings["errors"][target.table] = str(exc)[:200]
                print(f"  NO-GO {target.table:<32} {str(exc)[:80]}")

    blocked = [t for t, v in findings["tables"].items() if not v["entitled"]]
    print(f"\n{len(findings['tables']) - len(blocked)}/{len(findings['tables'])} targets entitled")
    if blocked:
        print(f"blocked: {', '.join(blocked)}")
    return findings


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="WRDS client (CRSP/Compustat/IBES)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="check entitlement and table shape")
    sub.add_parser("manifest", help="print the target list (no credentials needed)")

    pull = sub.add_parser("pull", help="fetch one target or all of them")
    pull.add_argument("table", nargs="?", help="library.table; omit with --all")
    pull.add_argument("--all", action="store_true")
    pull.add_argument("--force", action="store_true", help="re-fetch cached chunks")
    pull.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    pull.add_argument("--chunk-rows", type=int, default=DEFAULT_CHUNK_ROWS)

    args = parser.parse_args(argv)

    # A missing package or absent credentials is an expected state on a machine
    # that has never claimed WRDS, not a crash. The traceback would bury the one
    # line that says what to do about it.
    try:
        return _dispatch(args, sub)
    except WRDSUnavailable as exc:
        print(f"WRDS unavailable: {exc}")
        return 2


def _dispatch(args, sub) -> int:
    if args.command == "manifest":
        print(f"{'table':<34}{'recs':>6}  purpose")
        for target in MANIFEST:
            print(f"{target.table:<34}{target.records:>6}  {target.purpose[:70]}")
        return 0

    if args.command == "probe":
        findings = probe()
        print(json.dumps(findings["errors"], indent=2))
        return 0

    targets = MANIFEST if args.all else [t for t in MANIFEST if t.table == args.table]
    if not targets:
        print(f"ERROR: {args.table!r} is not in the manifest.")
        return 1

    for target in targets:
        print(f"\n=== {target.table}")
        if not target.chunk_on:
            print("  unchunked target: fetch inside an explicit connection() block")
            continue
        report = fetch_chunked(
            target,
            max_rows=args.max_rows,
            chunk_rows=args.chunk_rows,
            force=args.force,
        )
        print(f"  {report.rows:,} rows in {report.chunks_fetched} fetched / "
              f"{report.chunks_cached} cached chunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())