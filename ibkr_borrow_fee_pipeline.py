#!/usr/bin/env python3
"""
IBKR Borrow Fee Pipeline:

Pulls Interactive Brokers' public stock-loan database (usa.txt) via FTP.
The feed is keyless (shared login 'shortstock', no password), published by IBKR
for their Short-Securities Availability / Short Sale Cost tools.

File format: pipe-delimited with header
  #SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|
where FEERATE = annualized borrow fee rate (bps? decimal? verified on first live pull).

This is snapshot-only (no history on FTP) — same daily-accumulator pattern as
tradingview_pipeline.py / schwab_movers_pipeline.py. Run daily to accumulate a
per-symbol borrow-fee history usable by backtest.py / event_backtest.py cost models.

CLI:
  python ibkr_borrow_fee_pipeline.py              # single daily pull
  python ibkr_borrow_fee_pipeline.py --backfill   # same as default (no history)

Output:
  storage/raw/ibkr/borrow_fee/year=YYYY/month=MM/ibkr_borrow_fee_{YYYYMMDD}.parquet

Schema:
  date | symbol | currency | name | contract_type | isin | rebate_rate |
  fee_rate | available | fetched_at
"""

import argparse
import datetime as dt
import ftplib
import io
import os
import sys
import time

import pandas as pd
from storage_utils import write_partitioned

FTP_HOST = "ftp3.interactivebrokers.com"
FTP_USER = "shortstock"
FTP_PASS = ""  # keyless
FILE_NAME = "usa.txt"
OUTPUT_DIR = os.path.join("storage", "raw", "ibkr", "borrow_fee")

# Expected columns from IBKR's usa.txt (pipe-delimited, header row starts with #)
# #SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|
RAW_COLUMNS = [
    "symbol", "currency", "name", "contract_type", "isin",
    "rebate_rate", "fee_rate", "available"
]

TIMEOUT = 30
RETRIES = 3
RETRY_DELAY = 5


def _fetch_usa_txt() -> str:
    """Download usa.txt from IBKR FTP. Returns raw text."""
    for attempt in range(1, RETRIES + 1):
        try:
            with ftplib.FTP(FTP_HOST, timeout=TIMEOUT) as ftp:
                ftp.login(FTP_USER, FTP_PASS)
                # usa.txt is in the root directory
                buf = io.BytesIO()
                ftp.retrbinary(f"RETR {FILE_NAME}", buf.write)
                return buf.getvalue().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == RETRIES:
                raise RuntimeError(
                    f"IBKR FTP fetch failed after {RETRIES} attempts: {e}"
                ) from e
            time.sleep(RETRY_DELAY)
    raise RuntimeError("unreachable")


def _parse_usa_txt(raw: str, fetched_at: str) -> pd.DataFrame:
    """Parse pipe-delimited usa.txt into a DataFrame."""
    lines = raw.strip().splitlines()
    if not lines:
        return pd.DataFrame(columns=RAW_COLUMNS + ["fetched_at", "date"])

    # First line is header starting with '#'
    header = lines[0].lstrip("#").strip()
    if header != "|".join(RAW_COLUMNS):
        # Tolerate minor header variations (extra trailing |)
        if not header.startswith("SYM|CUR"):
            raise ValueError(f"Unexpected IBKR header: {header[:80]}...")

    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split("|")
        # IBKR format ends with trailing | -> empty last element
        if len(parts) >= len(RAW_COLUMNS):
            parts = parts[:len(RAW_COLUMNS)]
        else:
            # Pad if short
            parts = parts + [""] * (len(RAW_COLUMNS) - len(parts))
        rows.append(parts)

    if not rows:
        return pd.DataFrame(columns=RAW_COLUMNS + ["fetched_at", "date"])

    df = pd.DataFrame(rows, columns=RAW_COLUMNS)
    # fee_rate and rebate_rate are numeric strings; available is int
    for col in ("rebate_rate", "fee_rate"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["available"] = pd.to_numeric(df["available"], errors="coerce").astype("Int64")
    df["fetched_at"] = fetched_at
    df["date"] = pd.Timestamp(fetched_at).date().isoformat()
    # Keep only US equities (CON='STK' typically), but don't hard-filter here
    return df


def main(backfill: bool = False):
    # backfill is a no-op (no history on FTP) but accepted for CLI compatibility
    now = dt.datetime.now(dt.timezone.utc)
    fetched_at = now.isoformat()
    date_str = now.strftime("%Y%m%d")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"[ibkr_borrow_fee] Fetching {FILE_NAME} from {FTP_HOST}...")
    raw = _fetch_usa_txt()
    print(f"[ibkr_borrow_fee] Parsing {len(raw)} bytes...")
    df = _parse_usa_txt(raw, fetched_at)
    print(f"[ibkr_borrow_fee] Parsed {len(df)} rows")

    if df.empty:
        print("[ibkr_borrow_fee] WARNING: empty parse result")
        return 0

    filename = f"ibkr_borrow_fee_{date_str}.parquet"
    path = write_partitioned(df, OUTPUT_DIR, filename)
    print(f"[ibkr_borrow_fee] Wrote {path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="Same as default (no history on FTP)")
    args = ap.parse_args()
    sys.exit(main(backfill=args.backfill))