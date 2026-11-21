#!/usr/bin/env python3
"""
USDA AMS Livestock Mandatory Reporting (LMR) beef pipeline -- the wholesale and
cash-cattle middle of the beef chain. The repo already had the two ends:
cattle futures (`futures`: LE=F, GF=F) and retail beef (`bls_avg_price`:
ground beef, sirloin), plus the World Bank's monthly global beef price.

Source: USDA AMS LMR DataMart, keyless public JSON API
  https://mpr.datamart.ams.usda.gov/services/v1.1/reports/{slug}/{section}?q=report_date=MM/DD/YYYY:MM/DD/YYYY
  Packers must report these trades by law (LMR Act), so the series are complete
  and continuous. History from April 2001. Vetted 2026-11-21: no key, no
  observed rate limit; full backfill is ~2s per report. robots.txt times out
  (the host serves no robots file); /services/ is the published data API.

Reports pulled:
  2453 LM_XB403  National Daily Boxed Beef Cutout & Cuts - Negotiated - PM
       -> beef_cutout          (daily: Choice/Select cutout $/cwt + volume in loads)
       -> beef_primal_values   (daily: rib/chuck/round/loin/brisket/... $/cwt)
  2477 LM_CT150  5 Area Weekly Weighted Average Direct Slaughter Cattle
       -> cattle_cash_weekly   (weekly: steer/heifer cash price by selling basis
                                and Choice grade mix, $/cwt, with head counts)

Point-in-time: every row carries `published_at` (USDA's publish timestamp,
Central time). The cutout's `date` is the trading day, published ~14:45 CT the
same day. The cattle report's `report_date` is the Monday it was PUBLISHED;
its trades are from the week before. Join on `published_at`, not on a date.

Two things about `published_at` (checked 2026-11-21): rows dated before ~2005
were bulk-loaded by USDA in 2004-2006, and a corrected report replaces the
original in the DataMart with the correction's timestamp. So `published_at` is
when the CURRENT figure became public -- never earlier than the truth, which
is the safe direction for a backtest, but it can be much later.

Rows with no numbers are dropped: the DataMart holds empty placeholder rows for
every cutout day of 2001-2003 (real cutout history starts 2004-01-05), and a
cattle category in which nothing traded that week has no price or head count.

USDA re-publishes corrected reports; incremental runs re-fetch the last
INCREMENTAL_DAYS days and curated.py keeps the newest fetch per key.

CLI:
  python usda_beef_pipeline.py                # last 45 days
  python usda_beef_pipeline.py --backfill     # 2001 -> today
  python usda_beef_pipeline.py --start 2020-01-01 --end 2020-12-31

Outputs:
  storage/raw/usda_ams/beef_cutout/year=YYYY/month=MM/beef_cutout_{mode}_{date}.parquet
  storage/raw/usda_ams/beef_primal/year=YYYY/month=MM/beef_primal_values_{mode}_{date}.parquet
  storage/raw/usda_ams/cattle_cash/year=YYYY/month=MM/cattle_cash_weekly_{mode}_{date}.parquet

Exits 1 if any table comes back empty, so run_all never reports a silent PASS.
"""

import argparse
import datetime
import sys
import time

import pandas as pd
import requests

from storage_utils import write_partitioned

BASE_URL = "https://mpr.datamart.ams.usda.gov/services/v1.1/reports"
RAW_ROOT = "storage/raw/usda_ams"
BACKFILL_START = datetime.date(2001, 1, 1)
INCREMENTAL_DAYS = 45
CHUNK_YEARS = 2          # cattle detail is ~3.3k rows/year; keep responses modest
REQUEST_GAP = 1.0
RETRIES = 4
HEADERS = {"User-Agent": "financial-data-pipeline/1.0"}


# --------------------------------------------------------------------- fetch

def _fetch(slug: int, section: str, start: datetime.date, end: datetime.date) -> list[dict]:
    """One report section over [start, end]. Retries 429/5xx and transport errors."""
    url = f"{BASE_URL}/{slug}/{requests.utils.quote(section)}"
    q = f"report_date={start:%m/%d/%Y}:{end:%m/%d/%Y}"
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, params={"q": q}, headers=HEADERS, timeout=180)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            payload = resp.json()
            stats = payload.get("stats", {})
            total, returned = stats.get("totalRows:"), stats.get("returnedRows:")
            if total is not None and returned is not None and total != returned:
                raise RuntimeError(f"{slug}/{section} {q}: truncated ({returned} of {total} rows)")
            return payload.get("results", [])
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - retry transport/HTTP errors
            last = exc
            time.sleep(3 * attempt)
    raise RuntimeError(f"{slug}/{section} {q}: failed after {RETRIES} attempts ({last})")


def _chunks(start: datetime.date, end: datetime.date):
    cur = start
    while cur <= end:
        stop = min(datetime.date(cur.year + CHUNK_YEARS, 1, 1) - datetime.timedelta(days=1), end)
        yield cur, stop
        cur = stop + datetime.timedelta(days=1)


def _fetch_range(slug: int, section: str, start: datetime.date, end: datetime.date) -> list[dict]:
    rows: list[dict] = []
    for a, b in _chunks(start, end):
        rows.extend(_fetch(slug, section, a, b))
        time.sleep(REQUEST_GAP)
    return rows


# --------------------------------------------------------------------- parse

def _num(s: pd.Series) -> pd.Series:
    """USDA sends numbers as strings with thousands separators ("3,813,500")."""
    return pd.to_numeric(s.astype("string").str.replace(",", "", regex=False).str.strip(),
                         errors="coerce")


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, format="%m/%d/%Y", errors="coerce").dt.strftime("%Y-%m-%d")


def _published(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, format="%m/%d/%Y %H:%M:%S", errors="coerce").dt.strftime(
        "%Y-%m-%dT%H:%M:%S")


def parse_cutout(values: list[dict], volume: list[dict]) -> pd.DataFrame:
    v = pd.DataFrame(values)
    if v.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": _date(v["report_date"]),
        "choice_600_900": _num(v["choice_600_900_current"]),
        "select_600_900": _num(v["select_600_900_current"]),
        "published_at": _published(v["published_date"]),
    })
    vol = pd.DataFrame(volume)
    if not vol.empty:
        vol_out = pd.DataFrame({"date": _date(vol["report_date"])})
        for col in ("choice_volume_loads", "select_volume_loads",
                    "trimmings_volume_loads", "coarse_volume_loads"):
            vol_out[col] = _num(vol[col]) if col in vol.columns else pd.NA
        out = out.merge(vol_out.drop_duplicates("date", keep="last"), on="date", how="left")
    out = out.dropna(subset=["choice_600_900", "select_600_900"], how="all")
    return out.dropna(subset=["date"]).drop_duplicates("date", keep="last")


def parse_primals(rows: list[dict]) -> pd.DataFrame:
    p = pd.DataFrame(rows)
    if p.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": _date(p["report_date"]),
        "primal": p["primal_desc"].astype("string").str.replace("Primal ", "", regex=False)
                                   .str.strip().str.lower(),
        "choice_600_900": _num(p["choice_600_900"]),
        "select_600_900": _num(p["select_600_900"]),
        "published_at": _published(p["published_date"]),
    })
    out = out.dropna(subset=["choice_600_900", "select_600_900"], how="all")
    return out.dropna(subset=["date", "primal"]).drop_duplicates(["date", "primal"], keep="last")


def parse_cattle(rows: list[dict]) -> pd.DataFrame:
    c = pd.DataFrame(rows)
    if c.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "report_date": _date(c["report_date"]),
        "class": c["class_description"].astype("string").str.strip().str.lower(),
        "selling_basis": c["selling_basis_description"].astype("string").str.strip().str.lower(),
        "grade": c["grade_description"].astype("string").str.strip(),
        "head_count": _num(c["head_count"]),
        "weight_avg": _num(c["weight_range_avg"]),
        "price_low": _num(c["price_range_low"]),
        "price_high": _num(c["price_range_high"]),
        "weighted_avg_price": _num(c["weighted_avg_price"]),
        "published_at": _published(c["published_date"]),
    })
    key = ["report_date", "class", "selling_basis", "grade"]
    out = out.dropna(subset=["head_count", "weighted_avg_price"], how="all")   # nothing traded
    return out.dropna(subset=key).drop_duplicates(key, keep="last")


# ----------------------------------------------------------------------- run

def run(start: datetime.date, end: datetime.date, mode: str, fetched_at: str) -> int:
    stamp = fetched_at[:10].replace("-", "")
    tables = {
        "beef_cutout": ("beef_cutout", lambda: parse_cutout(
            _fetch_range(2453, "Current Cutout Values", start, end),
            _fetch_range(2453, "Current Volume", start, end))),
        "beef_primal_values": ("beef_primal", lambda: parse_primals(
            _fetch_range(2453, "Composite Primal Values", start, end))),
        "cattle_cash_weekly": ("cattle_cash", lambda: parse_cattle(
            _fetch_range(2477, "Detail", start, end))),
    }
    empty = []
    for table, (subdir, build) in tables.items():
        try:
            df = build()
        except Exception as exc:  # noqa: BLE001 - report per table, fail the run below
            print(f"  {table}: ERROR - {exc}")
            empty.append(table)
            continue
        if df.empty:
            print(f"  {table}: no rows for {start} .. {end}")
            empty.append(table)
            continue
        df["fetched_at"] = fetched_at
        path = write_partitioned(df, f"{RAW_ROOT}/{subdir}", f"{table}_{mode}_{stamp}.parquet")
        date_col = "report_date" if "report_date" in df.columns else "date"
        print(f"  {table}: {len(df):,} rows  {df[date_col].min()} .. {df[date_col].max()}  -> {path}")
    if empty:
        print(f"  FAILED tables: {', '.join(empty)}")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="USDA AMS LMR beef: cutout, primals, cash cattle (keyless)")
    parser.add_argument("--backfill", action="store_true", help=f"Full history from {BACKFILL_START}")
    parser.add_argument("--start", type=datetime.date.fromisoformat, default=None, help="YYYY-MM-DD")
    parser.add_argument("--end", type=datetime.date.fromisoformat, default=None, help="YYYY-MM-DD")
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone.utc)
    end = args.end or now.date()
    if args.start:
        start = args.start
    elif args.backfill:
        start = BACKFILL_START
    else:
        start = end - datetime.timedelta(days=INCREMENTAL_DAYS)
    mode = "backfill" if args.backfill else "incremental"

    print(f"USDA Beef Pipeline  mode={mode}  {start} .. {end}")
    code = run(start, end, mode, now.isoformat())
    print("--- USDA BEEF PIPELINE COMPLETE ---" if code == 0 else "--- USDA BEEF PIPELINE FAILED ---")
    sys.exit(code)


if __name__ == "__main__":
    main()
