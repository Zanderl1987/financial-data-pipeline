#!/usr/bin/env python3
"""
Treasury TIC Pipeline — Foreign Holdings of US Securities.

Downloads the US Treasury's TIC (Treasury International Capital) monthly data:
  1. Major Foreign Holders of Treasury Securities (MFHHIS)
     Who holds how much US debt — China, Japan, UK, Belgium, etc.
     Signal: Reduction in holdings → upward pressure on yields + USD weakening.

  2. Foreign Portfolio Holdings of US Securities (SHL) — annual survey
     Broader equity + debt positions by country.
     Signal: Capital flow shifts by geography → sector/FX implications.

Source: https://ticdata.treasury.gov/
No API key required — public CSV/TXT files.

Outputs:
  storage/raw/treasury_tic/year=YYYY/month=MM/treasury_tic_{feed}_{mode}_{date}.parquet
  CATALOG tables: treasury_tic_holders, treasury_tic_shl

Usage:
  python treasury_tic_pipeline.py             # incremental (last 24 months)
  python treasury_tic_pipeline.py --backfill  # full history from 2000
"""

import argparse
import datetime
import io
import os

import pandas as pd
import requests

from storage_utils import write_partitioned

OUTPUT_DIR = os.path.join("storage", "raw", "treasury_tic")

# Major Foreign Holders of US Treasury Securities — long-run monthly table
MFHHIS_URL = "https://ticdata.treasury.gov/Publish/mfhhis01.txt"

# Monthly table: US long-term securities held by foreign residents
SLT_TABLE1_URL  = "https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/slt_table1.txt"
# Annual historical SHL survey (all-country equities + bonds)
SHL_HIST_URL    = "https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/shlhistdat.txt"

BACKFILL_START_YEAR = 2000
INCREMENTAL_MONTHS  = 24
MAX_RETRIES         = 3


def _get_text(url: str) -> str | None:
    headers = {"User-Agent": "financial-data-pipeline/1.0 (research)"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code == 200:
                return r.text
            print(f"  HTTP {r.status_code} for {url}")
            return None
        except requests.RequestException as exc:
            print(f"  Error (attempt {attempt}): {exc}")
    return None


# ── Major Foreign Holders (MFHHIS) ────────────────────────────────────────────

_MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,  "May": 5,  "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def fetch_mfhhis(start_year: int) -> pd.DataFrame:
    """
    Parse the MFHHIS tab-delimited file.
    Layout: row N = month names (Dec Nov Oct ...), row N+1 = years (2025 2025 ...).
    Data rows follow the separator line ("------").
    """
    print("  [mfhhis] Fetching major foreign holders table...")
    text = _get_text(MFHHIS_URL)
    if not text:
        return pd.DataFrame()

    lines = [ln.rstrip("\r") for ln in text.splitlines()]

    # Locate the month-name header row (first row whose tabs give >= 3 month abbrs)
    month_row_idx = None
    for i, line in enumerate(lines):
        parts = [p.strip() for p in line.split("\t") if p.strip()]
        if sum(1 for p in parts if p in _MONTH_ABBR) >= 3:
            month_row_idx = i
            break

    if month_row_idx is None:
        print("  Could not locate month header row.")
        return pd.DataFrame()

    month_parts = [p.strip() for p in lines[month_row_idx].split("\t")]
    year_parts  = [p.strip() for p in lines[month_row_idx + 1].split("\t")]

    # Build column date labels — skip the first tab cell (empty / "Country")
    date_cols: list[str] = []
    for m, y in zip(month_parts, year_parts):
        if m in _MONTH_ABBR and y.isdigit() and len(y) == 4:
            date_cols.append(f"{y}-{_MONTH_ABBR[m]:02d}")

    if not date_cols:
        print("  No date columns parsed.")
        return pd.DataFrame()

    # Data rows start after the "------" separator
    data_start = month_row_idx + 2
    for i in range(data_start, min(data_start + 5, len(lines))):
        if "---" in lines[i]:
            data_start = i + 1
            break

    rows = []
    for line in lines[data_start:]:
        if not line.strip() or line.strip().startswith(("*", "1/", "2/", "Note", "Grand")):
            continue
        cells = [c.strip().strip('"') for c in line.split("\t")]
        country = cells[0] if cells else ""
        if not country or len(country) < 2:
            continue
        values = [c for c in cells[1:] if c]
        for j, val in enumerate(values):
            if j >= len(date_cols):
                break
            holdings = pd.to_numeric(val.replace(",", ""), errors="coerce")
            if pd.isna(holdings):
                continue
            rows.append({
                "country":     country,
                "date":        date_cols[j] + "-01",
                "holdings_bn": holdings,
            })

    if not rows:
        print("  No data rows parsed.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df = df[df["date"].dt.year >= start_year]
    df["source"] = "mfhhis"
    df["units"]  = "USD billions"

    print(f"    {len(df):,} country-month rows")
    return df.sort_values(["country", "date"]).reset_index(drop=True)


# ── SHL Annual Survey ─────────────────────────────────────────────────────────

def fetch_slt_table1() -> pd.DataFrame:
    """
    Fetch SLT Table 1 — US long-term securities held by foreign residents.
    Long-form tab-delimited: country, country_code, date (YYYY-MM), holdings columns.
    """
    print("  [slt1] Fetching monthly long-term foreign holdings (Table 1)...")
    text = _get_text(SLT_TABLE1_URL)
    if not text or len(text) < 200:
        print("  No data returned.")
        return pd.DataFrame()

    lines = [ln.rstrip("\r") for ln in text.splitlines()]

    # Find the machine-readable header row (contains "country_code" and "date")
    header_idx = None
    for i, line in enumerate(lines):
        if "country_code" in line.lower() and "date" in line.lower():
            header_idx = i
            break

    if header_idx is None:
        print("  Could not find machine-readable header.")
        return pd.DataFrame()

    try:
        df = pd.read_csv(
            io.StringIO("\n".join(lines[header_idx:])),
            sep="\t",
            on_bad_lines="skip",
        )
    except Exception as exc:
        print(f"  Parse error: {exc}")
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df.get("date", pd.Series(dtype=str)), errors="coerce")
    df = df.dropna(subset=["date"])
    df["source"] = "slt_table1"
    df["units"]  = "USD millions"

    numeric_cols = [c for c in df.columns if c not in ("country", "country_code", "date", "source", "units")]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    print(f"    {len(df):,} country-month rows | {df['date'].min().strftime('%Y-%m')} to {df['date'].max().strftime('%Y-%m')}")
    return df.sort_values(["country", "date"]).reset_index(drop=True)


# ── Main ───────────────────────────────────────────────────────────────────────

def main(backfill: bool = False) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    now   = datetime.datetime.utcnow()
    today = now.strftime("%Y%m%d")
    mode  = "backfill" if backfill else "incremental"
    start_year = BACKFILL_START_YEAR if backfill else now.year - (INCREMENTAL_MONTHS // 12 + 1)

    print(f"Treasury TIC Pipeline  mode={mode}\n")

    # ── MFHHIS ────────────────────────────────────────────────────────────────
    df_holders = fetch_mfhhis(start_year)
    if not df_holders.empty:
        df_holders["fetched_at"] = now.isoformat()
        path = write_partitioned(
            df_holders, OUTPUT_DIR, f"treasury_tic_holders_{mode}_{today}.parquet"
        )
        print(f"[+] {path}  ({len(df_holders):,} rows)\n")

    # ── SLT Table 1 — monthly long-term foreign holdings ──────────────────────
    df_slt = fetch_slt_table1()
    if not df_slt.empty:
        df_slt["fetched_at"] = now.isoformat()
        path = write_partitioned(
            df_slt, OUTPUT_DIR, f"treasury_tic_slt_{mode}_{today}.parquet"
        )
        print(f"[+] {path}  ({len(df_slt):,} rows)")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Treasury TIC pipeline — foreign holdings of US securities (keyless)"
    )
    parser.add_argument("--backfill", action="store_true",
                        help=f"Full history from {BACKFILL_START_YEAR}. Default: last {INCREMENTAL_MONTHS} months.")
    args = parser.parse_args()
    main(backfill=args.backfill)
