"""
strategies/crsp_pull.py -- download the CRSP population for the survivorship re-score.

Writes, under storage/licensed/crsp/ (never storage/curated/, which is published):

  companies.parquet   one row per company that was ever a common stock (share code 10 or 11)
                      on NYSE, AMEX or Nasdaq (exchange code 1, 2 or 3): the columns
                      strategies/survivorship.py reads, plus `crsp_dlstcd`, `left_population`
                      and `n_spells`, which explain any change from CRSP's own values.
  prices.parquet      CRSP legacy daily rows (crsp.dsf) for those companies, only on days the
                      company was in the population. A holdout company's prices stop at
                      DEV_END: they are capped in the request itself, and refused if they
                      arrive anyway.
  pull_report.json    counts, the first scored date, and Stage 3 symbols no company matched.

Rules from the survivorship amendment (changes 3 and 4) and how they are applied here:

  * In the population on a day = that day's name record has share code 10/11 and exchange
    code 1/2/3. A halt or suspension (-2/-1) that continues such a spell stays in it, so the
    last real price before a delisting is kept. No selection uses a company's later outcome.
  * A company that left the population other than by delisting (for example its share code
    changed) is closed at its last eligible close: its later CRSP delisting is not applied,
    because that fall happened outside the population. `crsp_dlstcd` keeps CRSP's code.
  * The ticker used to match Stage 3 symbols is the company's last ticker in the population,
    with "-<class>" added only when Stage 3 holds that form (Yahoo writes BF-B).
  * The holdout assignment is survivorship.assign_holdout, the same function the scorer
    runs at load time, plus three stricter rules for Stage 3 holdout symbols that no
    company matched by ticker (for example a ticker changed after CRSP's last date). A
    development-assigned company not matched to any Stage 3 symbol moves to the holdout if:
      1. any ticker in its CRSP name history is that symbol ("holdout_ticker_history");
      2. its 2016-2017 closes track the symbol's Yahoo closes: 60+ shared days, 90% of them
         within 2% of the median price ratio, so a later split still matches
         ("holdout_price_match"); or
      3. the symbol has too few pre-2018 days to compare, and the company's first CRSP date is
         within 7 days of the symbol's first Yahoo date ("holdout_listing_date").
    The thresholds were fixed before any CRSP data was seen. The rules only move companies
    INTO the holdout, and read no price after DEV_END, no delisting and no strategy result.

So the download has two steps: every company through DEV_END, then the rules, then
development companies only from the day after DEV_END.

Needs the WRDS username in WRDS_USER and the password in ~/.pgpass (or %APPDATA%\\postgresql\\
pgpass.conf on Windows). This file never reads a password.

Usage (from the repo root, once WRDS works):
    C:\\ProgramData\\anaconda3\\python.exe -m strategies.crsp_pull --tables-only
    C:\\ProgramData\\anaconda3\\python.exe -m strategies.crsp_pull
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies import survivorship as sv                       # noqa: E402
from strategies.stage3 import DEV_END, _is_holdout_symbol       # noqa: E402

COMMON_SHRCD = (10, 11)
PRIMARY_EXCHCD = sv.EXCHANGES          # NYSE, AMEX, Nasdaq
HALTED_EXCHCD = (-2, -1)               # halted / suspended: continues a spell, never starts one
DEFAULT_START = "1990-01-01"
#: Trailing dollar volume needs 20 sessions before the first scored date; 60 calendar days is ample.
MIN_LEAD_DAYS = 60
AFTER_DEV_END = (pd.Timestamp(DEV_END) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

#: Stricter holdout rules. Fixed before any CRSP data was seen; never tuned on results.
MATCH_START = "2016-01-01"     # price matching uses 2016-01-01 .. DEV_END only
MATCH_MIN_DAYS = 60
MATCH_BAND = 0.02              # the same 2% as survivorship.compare_with_yahoo
MATCH_SHARE = 0.90
LISTING_DAYS = 7

NAMES_SQL = "select permno, namedt, nameendt, shrcd, exchcd, ticker, shrcls from crsp.dsenames"
DELIST_SQL = "select permno, dlstdt, dlstcd, dlret from crsp.dsedelist"
PRICE_COLUMNS = ("permno", "date") + sv._CRSP_COLUMNS[1:]


# ------------------------------------------------------------------ queries

def prices_sql(permnos, start: str, end: "str | None"):
    """(sql, params). Permnos are checked as integers before they go into the text."""
    ids = []
    for p in permnos:
        if isinstance(p, bool) or not isinstance(p, (int,)) and not hasattr(p, "__index__"):
            raise TypeError(f"permno must be an integer, got {p!r}")
        ids.append(str(int(p)))
    sql = (f"select {', '.join(PRICE_COLUMNS)} from crsp.dsf "
           f"where permno in ({', '.join(ids)}) and date >= %(start)s")
    params = {"start": start}
    if end is not None:
        sql += " and date <= %(end)s"
        params["end"] = end
    return sql + " order by permno, date", params


class WrdsSource:
    """The real source. Opens one WRDS connection; the password comes from pgpass."""

    def __init__(self, username: str):
        import wrds
        self.db = wrds.Connection(wrds_username=username)

    def names(self) -> pd.DataFrame:
        return self.db.raw_sql(NAMES_SQL, date_cols=["namedt", "nameendt"])

    def delist(self) -> pd.DataFrame:
        return self.db.raw_sql(DELIST_SQL, date_cols=["dlstdt"])

    def prices(self, permnos, start, end) -> pd.DataFrame:
        sql, params = prices_sql(permnos, start, end)
        return self.db.raw_sql(sql, params=params, date_cols=["date"])

    def describe(self) -> dict:
        """Column names and types of the tables used, to check the mapping before pulling."""
        out = {}
        for table in ("dsenames", "dsedelist", "dsf"):
            out[table] = self.db.describe_table("crsp", table)[["name", "type"]].astype(str).values.tolist()
        return out

    def close(self):
        self.db.close()


# ------------------------------------------------------------------ the population

def _eligible_rows(names: pd.DataFrame) -> pd.Series:
    common = names["shrcd"].isin(COMMON_SHRCD)
    return common & names["exchcd"].isin(PRIMARY_EXCHCD + HALTED_EXCHCD)


def eligible_spells(names: pd.DataFrame) -> pd.DataFrame:
    """(permno, start, end) for each unbroken run of eligible name rows.
    A run made only of halts is dropped: a halt continues a spell, it does not start one."""
    return _spells(names)[0]


def _spells(names: pd.DataFrame):
    """(spells, sorted names); spells index rows of the sorted names."""
    n = names.sort_values(["permno", "namedt"]).reset_index(drop=True)
    n["ok"] = _eligible_rows(n)
    n["run"] = (n["ok"] != n.groupby("permno")["ok"].shift()).cumsum()
    rows = []
    for (permno, _run), g in n[n["ok"]].groupby(["permno", "run"], sort=True):
        primary = g[g["exchcd"].isin(PRIMARY_EXCHCD)]
        if primary.empty:
            continue
        rows.append({"permno": permno, "start": g["namedt"].min(), "end": g["nameendt"].max(),
                     "last_row": g.index[-1], "last_primary_row": primary.index[-1]})
    return pd.DataFrame(rows, columns=["permno", "start", "end", "last_row", "last_primary_row"]), n


def in_spells(prices: pd.DataFrame, spells: pd.DataFrame) -> pd.DataFrame:
    """Rows on days the company was in the population."""
    if prices.empty:
        return prices
    p = prices.reset_index(drop=True)
    p["date"] = pd.to_datetime(p["date"])
    m = p.reset_index().merge(spells[["permno", "start", "end"]], on="permno")
    keep = m.loc[(m["date"] >= m["start"]) & (m["date"] <= m["end"]), "index"].unique()
    return p.iloc[sorted(keep)]


def build_companies(names: pd.DataFrame, delist: pd.DataFrame, stage3_symbols=()) -> pd.DataFrame:
    """One row per company ever in the population, in the shape survivorship.py reads."""
    spells, n = _spells(names)
    stage3_symbols = set(stage3_symbols)
    last_name_end = n.groupby("permno")["nameendt"].max()
    history = {}
    for r in n.itertuples():
        t = "" if pd.isna(r.ticker) else str(r.ticker)
        if not t:
            continue
        forms = history.setdefault(r.permno, set())
        forms.add(t)
        cls = getattr(r, "shrcls", None)
        if isinstance(cls, str) and cls.strip():
            forms.add(f"{t}-{cls.strip()}")
    dl = delist.drop_duplicates("permno", keep="last").set_index("permno")
    rows = []
    for permno, s in spells.groupby("permno", sort=True):
        last = s.iloc[-1]
        name = n.loc[last["last_row"]]
        primary = n.loc[last["last_primary_row"]]
        ticker = "" if pd.isna(name["ticker"]) else str(name["ticker"])
        shrcls = name.get("shrcls")
        if isinstance(shrcls, str) and shrcls.strip() and f"{ticker}-{shrcls.strip()}" in stage3_symbols:
            ticker = f"{ticker}-{shrcls.strip()}"
        left = bool(last_name_end.loc[permno] > last["end"])
        crsp_code = int(dl.loc[permno, "dlstcd"]) if permno in dl.index else sv.ACTIVE_CODE
        if left or permno not in dl.index:
            dlstdt, dlstcd, dlret = pd.NaT, sv.ACTIVE_CODE, None
        else:
            dlstdt, dlstcd, dlret = dl.loc[permno, "dlstdt"], crsp_code, dl.loc[permno, "dlret"]
        rows.append({"permno": int(permno), "ticker": ticker, "begdt": s["start"].min(),
                     "dlstdt": dlstdt, "dlstcd": dlstcd, "dlret": dlret,
                     "exchcd": int(primary["exchcd"]), "crsp_dlstcd": crsp_code,
                     "left_population": left, "n_spells": len(s),
                     "ticker_history": "|".join(sorted(history.get(permno, ())))})
    out = pd.DataFrame(rows, columns=list(sv._COMPANY_COLUMNS) + ["crsp_dlstcd", "left_population", "n_spells",
                                                                   "ticker_history"])
    for c in ("begdt", "dlstdt"):
        out[c] = pd.to_datetime(out[c])
    out["dlret"] = pd.to_numeric(out["dlret"], errors="coerce")
    return out


def _holdout_assignment(companies: pd.DataFrame, stage3_ranges: dict):
    """Exactly what survivorship._read_companies does before assign_holdout."""
    c = companies.copy()
    c["dlstdt"] = pd.to_datetime(c["dlstdt"]).where(c["dlstcd"] != sv.ACTIVE_CODE)
    return sv.assign_holdout(c, stage3_ranges)


# ------------------------------------------------------------------ the pull

def _check_root(out_root: str) -> None:
    parts = os.path.normpath(os.path.abspath(out_root)).lower().split(os.sep)
    if "curated" in parts:
        raise ValueError("CRSP must not be written under storage/curated/, which is published")


def _plan(permnos: list, batch: int) -> list:
    return [permnos[i:i + batch] for i in range(0, len(permnos), batch)]


def _plan_hash(plan, start: str) -> str:
    text = json.dumps([[int(p) for p in b] for b in plan] + [start])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ stricter holdout rules

def _window(yahoo: pd.Series) -> pd.Series:
    """Yahoo closes usable for matching: MATCH_START .. DEV_END only, whatever was passed in."""
    if yahoo is None or len(yahoo) == 0:
        return pd.Series(dtype=float)
    y = pd.Series(yahoo, dtype=float).copy()
    y.index = pd.to_datetime(y.index)
    y = y[(y.index >= pd.Timestamp(MATCH_START)) & (y.index <= pd.Timestamp(DEV_END))]
    return y[np.isfinite(y) & (y > 0)]


def price_matches(yahoo: pd.Series, closes: pd.DataFrame) -> list:
    """Columns of `closes` (dates x permno, split-adjusted CRSP closes) whose price ratio to
    `yahoo` stays within MATCH_BAND of its median on MATCH_SHARE of MATCH_MIN_DAYS+ shared days."""
    y = _window(yahoo)
    if len(y) < MATCH_MIN_DAYS or closes.empty:
        return []
    c = closes.reindex(y.index)
    ratio = c.rdiv(y, axis=0).where(lambda r: np.isfinite(r) & (r > 0))
    n = ratio.notna().sum()
    near = (ratio.div(ratio.median()).sub(1.0).abs() <= MATCH_BAND).sum()
    ok = (n >= MATCH_MIN_DAYS) & (near >= MATCH_SHARE * n)
    return [p for p in ratio.columns[ok.to_numpy()]]


def _candidate_closes(parts: list, permnos) -> pd.DataFrame:
    if not parts or not len(permnos):
        return pd.DataFrame()
    p = pd.concat([pd.read_parquet(f, filters=[("permno", "in", [int(x) for x in permnos]),
                                               ("date", ">=", pd.Timestamp(MATCH_START))]) for f in parts],
                  ignore_index=True)
    p = p[(p["prc"] > 0) & (p["cfacpr"] > 0) & (p["date"] <= pd.Timestamp(DEV_END))]
    p = p.assign(close=p["prc"] / p["cfacpr"])
    return p.pivot_table(index="date", columns="permno", values="close", aggfunc="last")


def extra_holdout(companies: pd.DataFrame, assigned: pd.DataFrame, unmatched_holdout: list,
                  stage3_ranges: dict, load_yahoo, closes_for) -> tuple:
    """({permno: basis}, {rule: n moved}, unresolved symbols). Only development-assigned
    companies not matched to any Stage 3 symbol are candidates; nothing leaves the holdout."""
    a = assigned.set_index("permno")
    cand = a.index[(~a["holdout"]) & (a["basis"] != "stage3_match")]
    c = companies.set_index("permno").loc[cand]
    histories = {p: set(str(h).split("|")) - {""} for p, h in c["ticker_history"].items()}
    forced, counts, unresolved = {}, {"ticker_history": 0, "price_match": 0, "listing_date": 0}, []
    closes = None

    def move(permnos, rule):
        hit = False
        for p in permnos:
            hit = True
            if p not in forced:
                forced[p] = f"holdout_{rule}"
                counts[rule] += 1
        return hit

    for sym in sorted(unmatched_holdout):
        hit = move([p for p, h in histories.items() if sym in h], "ticker_history")
        yahoo = _window(load_yahoo(sym, DEV_END))
        if len(yahoo) >= MATCH_MIN_DAYS:
            if closes is None:
                closes = closes_for(list(cand))
            hit = move(price_matches(yahoo, closes), "price_match") or hit
        else:
            first = pd.Timestamp(stage3_ranges[sym][0])
            near = c.index[(c["begdt"] - first).abs() <= pd.Timedelta(days=LISTING_DAYS)]
            hit = move(list(near), "listing_date") or hit
        if not hit:
            unresolved.append(sym)
    return forced, counts, unresolved


# ------------------------------------------------------------------ the pull

def _fetch(source, ids, start, end, spells, reserved: set) -> pd.DataFrame:
    """One request, checked: only the companies asked for, and no reserved rows."""
    raw = source.prices(ids, start, end)
    raw = raw[list(PRICE_COLUMNS)] if len(raw) else pd.DataFrame(columns=PRICE_COLUMNS)
    raw["date"] = pd.to_datetime(raw["date"])
    extra = set(raw["permno"].unique()) - set(ids)
    if extra:
        raise RuntimeError(f"source returned {len(extra)} companies that were not requested")
    sv.assert_no_reserved_rows(raw, reserved)
    return in_spells(raw, spells)


def _write(df: pd.DataFrame, path: str) -> None:
    df.sort_values(["permno", "date"]).to_parquet(path + ".tmp", index=False)
    os.replace(path + ".tmp", path)


def pull(source, stage3_ranges: dict, out_root: str = sv.CRSP_ROOT, *, start: str = DEFAULT_START,
         batch: int = 500, overwrite: bool = False, min_lead_days: int = MIN_LEAD_DAYS, load_yahoo=None,
         log=print) -> dict:
    """Download companies and prices in two steps (see the module docstring). Resumes from
    finished batches if a run with the same plan was interrupted. Anything that looks like a
    holdout leak deletes every batch pulled."""
    _check_root(out_root)
    prices_path = os.path.join(out_root, "prices.parquet")
    if os.path.exists(prices_path) and not overwrite:
        raise FileExistsError(f"{prices_path} exists; pass overwrite=True to replace it")

    names = source.names()
    companies = build_companies(names, source.delist(), stage3_symbols=stage3_ranges.keys())
    spells = eligible_spells(names)
    assigned, unmatched = _holdout_assignment(companies, stage3_ranges)
    unmatched_holdout = [s for s in unmatched if _is_holdout_symbol(s)]
    if unmatched_holdout and load_yahoo is None:
        raise ValueError(f"{len(unmatched_holdout)} holdout Stage 3 symbols are unmatched; "
                         f"pass load_yahoo so the stricter holdout rules can run")
    os.makedirs(out_root, exist_ok=True)

    permnos = sorted(int(p) for p in companies["permno"])
    plan = _plan(permnos, batch)
    parts_dir = os.path.join(out_root, "_parts")
    manifest = os.path.join(parts_dir, "plan.sha256")
    digest = _plan_hash(plan, start)
    if os.path.isdir(parts_dir):
        with open(manifest, encoding="utf-8") as fh:
            if fh.read().strip() != digest:
                raise RuntimeError(f"{parts_dir} is from a different pull; delete it to start over")
    os.makedirs(parts_dir, exist_ok=True)
    with open(manifest, "w", encoding="utf-8") as fh:
        fh.write(digest)
    a_parts = [os.path.join(parts_dir, f"a_{i:05d}.parquet") for i in range(len(plan))]
    b_parts = [os.path.join(parts_dir, f"b_{i:05d}.parquet") for i in range(len(plan))]

    try:
        # Step 1: every company, through DEV_END only. Until the rules have run, every
        # company is treated as reserved after DEV_END.
        for i, ids in enumerate(plan):
            if not os.path.exists(a_parts[i]):
                _write(_fetch(source, ids, start, sv.pull_end(True), spells, set(ids)), a_parts[i])
                log(f"through {DEV_END}: batch {i + 1}/{len(plan)}")

        forced, rule_counts, unresolved = extra_holdout(
            companies, assigned, unmatched_holdout, stage3_ranges, load_yahoo,
            lambda cands: _candidate_closes(a_parts, cands))
        companies["forced_holdout"] = companies["permno"].isin(list(forced))
        companies["forced_basis"] = companies["permno"].map(forced)
        holdout = set(assigned.loc[assigned["holdout"], "permno"].astype(int)) | set(forced)
        holdout_file = os.path.join(parts_dir, "holdout.json")
        if os.path.exists(holdout_file):
            with open(holdout_file, encoding="utf-8") as fh:
                if set(json.load(fh)) != holdout:
                    raise RuntimeError("holdout set differs from the interrupted pull; delete _parts and start over")
        with open(holdout_file, "w", encoding="utf-8") as fh:
            json.dump(sorted(holdout), fh)
        companies.to_parquet(os.path.join(out_root, "companies.parquet"), index=False)
        log(f"companies: {len(companies)}; {len(holdout)} holdout; stricter rules moved {rule_counts}")

        # Step 2: development companies only, from the day after DEV_END.
        for i, ids in enumerate(plan):
            dev_ids = [p for p in ids if p not in holdout]
            if dev_ids and not os.path.exists(b_parts[i]):
                _write(_fetch(source, dev_ids, AFTER_DEV_END, sv.pull_end(False), spells, holdout), b_parts[i])
                log(f"after {DEV_END}: batch {i + 1}/{len(plan)}")
    except RuntimeError:
        shutil.rmtree(parts_dir, ignore_errors=True)
        raise

    _combine(a_parts, b_parts, prices_path)
    first = sv.first_common_open_date(sv._first_opens(prices_path), companies)
    if first < pd.Timestamp(start) + pd.Timedelta(days=min_lead_days):
        os.remove(prices_path)
        raise RuntimeError(f"first scored date {first.date()} leaves under {min_lead_days} days of earlier "
                           f"rows for trailing volume; pull again with an earlier --start")
    shutil.rmtree(parts_dir)

    basis = assigned.set_index("permno")["basis"].copy()
    for p, b in forced.items():
        basis.loc[p] = b
    report = {"start": str(first.date()), "pull_start": start, "dev_end_for_holdout": DEV_END,
              "n_companies": len(companies), "n_holdout": len(holdout),
              "n_left_population": int(companies["left_population"].sum()),
              "n_multiple_spells": int((companies["n_spells"] > 1).sum()),
              "basis": basis.value_counts().to_dict(),
              "holdout_rules": rule_counts,
              "stage3_unmatched": {"holdout": unmatched_holdout,
                                   "development": [s for s in unmatched if not _is_holdout_symbol(s)]},
              "stage3_unresolved_holdout": unresolved}
    with open(os.path.join(out_root, "pull_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    log(f"done: first scored date {report['start']}; {len(unresolved)} holdout Stage 3 symbols "
        f"matched no company by any rule")
    return report


def _combine(a_parts: list, b_parts: list, prices_path: str) -> None:
    """One file, in permno order, so the scorer's permno filter skips row groups."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    tmp = prices_path + ".tmp"
    writer = schema = None
    try:
        for a, b in zip(a_parts, b_parts):
            frames = [pd.read_parquet(a)] + ([pd.read_parquet(b)] if os.path.exists(b) else [])
            frames = [f for f in frames if len(f)] or frames[:1]
            df = pd.concat(frames, ignore_index=True).sort_values(["permno", "date"])
            table = pa.Table.from_pandas(df, preserve_index=False)
            if writer is None:
                schema = table.schema
                writer = pq.ParquetWriter(tmp, schema)
            writer.write_table(table.cast(schema) if table.schema != schema else table)
        if writer is None:
            pq.write_table(pa.table({c: [] for c in PRICE_COLUMNS}), tmp)
    finally:
        if writer is not None:
            writer.close()
    os.replace(tmp, prices_path)


# ------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download the CRSP population for the survivorship re-score.")
    ap.add_argument("--out", default=sv.CRSP_ROOT)
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--tables-only", action="store_true",
                    help="print the columns of the CRSP tables used and stop; downloads no data")
    args = ap.parse_args(argv)

    username = os.environ.get("WRDS_USER")
    if not username:
        try:
            from dotenv import load_dotenv
            load_dotenv()
            username = os.environ.get("WRDS_USER")
        except ImportError:
            pass
    if not username:
        raise SystemExit("WRDS_USER is not set")

    source = WrdsSource(username)
    try:
        if args.tables_only:
            print(json.dumps(source.describe(), indent=1))
            return 0
        import query
        from strategies import stage3
        ranges = sv.stage3_ranges(query, stage3.PRICE_TABLE)

        def load_yahoo(sym, end):
            df = stage3._load_ohlcv(sym, stage3.PRICE_TABLE, start=None, end=end)
            return pd.Series(dtype=float) if df is None or df.empty else df["close"]
        pull(source, ranges, args.out, start=args.start, batch=args.batch, overwrite=args.overwrite,
             load_yahoo=load_yahoo)
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
