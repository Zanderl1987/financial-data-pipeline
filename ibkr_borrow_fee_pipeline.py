#!/usr/bin/env python3
"""
IBKR Borrow Fee Pipeline:

Pulls Interactive Brokers' public stock-loan database (usa.txt) via FTP.
The feed is keyless (shared login 'shortstock', no password), published by IBKR
for their Short-Securities Availability / Short Sale Cost tools.

Hosts: ftp2.interactivebrokers.com works; ftp3 (the original host) stopped
answering on every port. That, not a local FTP block, is why this pipeline
never stored a row between 2026-09-05 and 2026-09-29 -- other FTP servers
connect fine from this machine. Both are tried, ftp2 first.

File format (verified live 2026-09-29), pipe-delimited, trailing '|':
  #BOF|2026.09.29|11:46:04
  #SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|
  ...one row per security...
  #EOF|19914                      <- data row count, checked on parse
- REBATERATE and FEERATE are ANNUAL PERCENT, not bps: easy-to-borrow names
  (AAPL, MSFT) show fee 0.25 and rebate + fee = the benchmark rate (3.88 on
  09-29); hard-to-borrow BYND shows 29.38. event_backtest's loaders convert
  to bps.
- CON is IBKR's contract id (stored as `conid`).
- AVAILABLE is shares available to borrow, capped at the literal
  ">10000000"; stored as 10000000 with available_capped=True.
- NAME carries HTML entities ("S&amp;P"); unescaped.

This is snapshot-only (no history on FTP) — same daily-accumulator pattern as
tradingview_pipeline.py / schwab_movers_pipeline.py. Run daily to accumulate a
per-symbol borrow-fee history usable by backtest.py / event_backtest.py cost models.

CLI:
  python ibkr_borrow_fee_pipeline.py              # single daily pull
  python ibkr_borrow_fee_pipeline.py --backfill   # same as default (no history)

Output:
  storage/raw/ibkr/borrow_fee/year=YYYY/month=MM/ibkr_borrow_fee_{YYYYMMDD}.parquet

Schema:
  date | symbol | currency | name | conid | isin | figi | rebate_rate |
  fee_rate | available | available_capped | fetched_at
  (rebate_rate, fee_rate: annual percent)
"""

import argparse
import datetime as dt
import ftplib
import html
import io
import os
import sys
import time

import pandas as pd
from storage_utils import write_partitioned

FTP_HOSTS = ("ftp2.interactivebrokers.com", "ftp3.interactivebrokers.com")
FTP_USER = "shortstock"
FTP_PASS = ""  # keyless
FILE_NAME = "usa.txt"
OUTPUT_DIR = os.path.join("storage", "raw", "ibkr", "borrow_fee")

# IBKR header field -> our column. Mapped by name, so a new or reordered
# field can't silently shift values into the wrong column.
COLUMN_MAP = {
    "SYM": "symbol", "CUR": "currency", "NAME": "name", "CON": "conid",
    "ISIN": "isin", "FIGI": "figi", "REBATERATE": "rebate_rate",
    "FEERATE": "fee_rate", "AVAILABLE": "available",
}
REQUIRED_FIELDS = ("SYM", "FEERATE")
OUT_COLUMNS = ["symbol", "currency", "name", "conid", "isin", "figi",
               "rebate_rate", "fee_rate", "available", "available_capped"]

TIMEOUT = 30
RETRIES = 3
RETRY_DELAY = 5


def _fetch_usa_txt() -> str:
    """Download usa.txt, trying each IBKR FTP host per attempt. Returns raw text."""
    errors = []
    for attempt in range(1, RETRIES + 1):
        for host in FTP_HOSTS:
            try:
                with ftplib.FTP(host, timeout=TIMEOUT) as ftp:
                    ftp.login(FTP_USER, FTP_PASS)
                    buf = io.BytesIO()
                    ftp.retrbinary(f"RETR {FILE_NAME}", buf.write)
                    print(f"[ibkr_borrow_fee] Fetched from {host}")
                    return buf.getvalue().decode("utf-8", errors="replace")
            except Exception as e:  # noqa: BLE001 - try the next host
                errors.append(f"{host}: {e}")
        if attempt < RETRIES:
            time.sleep(RETRY_DELAY)
    raise RuntimeError(
        f"IBKR FTP fetch failed after {RETRIES} attempts on {len(FTP_HOSTS)} hosts: "
        + " | ".join(errors[-len(FTP_HOSTS):])
    )


def _parse_usa_txt(raw: str, fetched_at: str) -> pd.DataFrame:
    """Parse usa.txt (#BOF / #SYM header / rows / #EOF count) into a DataFrame."""
    header = None
    eof_count = None
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            fields = line[1:].rstrip("|").split("|")
            if fields[0] == "SYM":
                header = fields
            elif fields[0] == "EOF" and len(fields) > 1:
                eof_count = int(fields[1])
            continue
        if header is None:
            raise ValueError(f"IBKR data row before the #SYM header: {line[:80]}")
        parts = line.rstrip("|").split("|")
        parts = (parts + [""] * len(header))[:len(header)]
        rows.append(parts)

    if header is None:
        raise ValueError(f"No #SYM header in IBKR file (starts: {raw[:80]!r})")
    missing = [f for f in REQUIRED_FIELDS if f not in header]
    if missing:
        raise ValueError(f"IBKR header lacks {missing}: {'|'.join(header)}")
    if eof_count is not None and eof_count != len(rows):
        raise ValueError(f"IBKR file truncated? #EOF says {eof_count} rows, parsed {len(rows)}")

    df = pd.DataFrame(rows, columns=header)
    df = df[[c for c in header if c in COLUMN_MAP]].rename(columns=COLUMN_MAP)
    for col in OUT_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    for col in ("rebate_rate", "fee_rate"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    avail = df["available"].astype("string").str.strip()
    df["available_capped"] = avail.str.startswith(">").fillna(False).astype(bool)
    df["available"] = pd.to_numeric(avail.str.lstrip(">"), errors="coerce").astype("Int64")
    df["name"] = df["name"].map(lambda s: html.unescape(s) if isinstance(s, str) else s)
    df = df[OUT_COLUMNS].copy()
    df["fetched_at"] = fetched_at
    df["date"] = pd.Timestamp(fetched_at).date().isoformat()
    return df


def main(backfill: bool = False):
    # backfill is a no-op (no history on FTP) but accepted for CLI compatibility
    now = dt.datetime.now(dt.timezone.utc)
    fetched_at = now.isoformat()
    date_str = now.strftime("%Y%m%d")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"[ibkr_borrow_fee] Fetching {FILE_NAME} from {', '.join(FTP_HOSTS)}...")
    raw = _fetch_usa_txt()
    print(f"[ibkr_borrow_fee] Parsing {len(raw)} bytes...")
    df = _parse_usa_txt(raw, fetched_at)
    print(f"[ibkr_borrow_fee] Parsed {len(df)} rows")

    if df.empty:
        print("[ibkr_borrow_fee] FAIL: empty parse result")
        return 1

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