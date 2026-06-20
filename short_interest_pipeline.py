#!/usr/bin/env python3
"""
Short Interest Pipeline — three complementary data sources:

  1. yfinance snapshot (default, daily-runnable)
     Pulls 9 fields from Yahoo Finance for every target symbol:
     shares short, prior-month shares short, % of float, days to cover,
     float shares, shares outstanding, and the as-of filing date.
     Accumulates into a dated time series when run regularly.

  2. FINRA biweekly Reg SHO short interest (--source finra)
     Official regulatory filing published ~24×/year.
     Full market coverage; pipe-delimited files from FINRA's CDN.
     Fields: symbol, company, market, shares_short, days_to_cover,
             change_shares, settlement_date.

  3. SEC Fails-to-Deliver (--source ftd)
     Published twice monthly. High FTD relative to float signals
     potential naked shorting pressure (see: GME, AMC 2021).
     Fields: settlement_date, cusip, symbol, shares_failed, price,
             description.

CLI:
  python short_interest_pipeline.py                      # yfinance snapshot
  python short_interest_pipeline.py --source finra       # latest FINRA file(s)
  python short_interest_pipeline.py --source ftd         # latest SEC FTD
  python short_interest_pipeline.py --source all         # all three

Outputs:
  storage/raw/short_interest/short_interest_{mode}_{YYYYMMDD}.parquet
  storage/raw/finra_short_interest/finra_short_{YYYYMMDD}.parquet
  storage/raw/sec_ftd/sec_ftd_{YYYYMMDD}.parquet

Key signals:
  short_pct_float > 0.20  → heavily shorted (possible squeeze fuel or fundamental concern)
  short_pct_float > 0.30  → extreme; meme-stock territory
  days_to_cover   > 5     → elevated short-squeeze risk (days of volume to cover all shorts)
  Rising short interest + falling price → bearish confirmation
  Rising FTD vs rising short interest  → possible naked-short pressure
"""

import os
import io
import time
import zipfile
import datetime
import argparse
import requests
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from finnhub_pipeline import get_dji_symbols

load_dotenv()

# ── Storage directories ────────────────────────────────────────────────────────
DIR_YF    = os.path.join("storage", "raw", "short_interest")
DIR_FINRA = os.path.join("storage", "raw", "finra_short_interest")
DIR_FTD   = os.path.join("storage", "raw", "sec_ftd")

# ── Sector ETFs to include alongside DJI ──────────────────────────────────────
SECTOR_ETFS = [
    "XLK", "XLF", "XLE", "XLV", "XLY",
    "XLI", "XLC", "XLRE", "XLP", "XLU", "XLB",
    "SPY", "QQQ", "IWM", "DIA",
]

REQUEST_INTERVAL = 0.4
MAX_RETRIES      = 3
BACKOFF_SECONDS  = 30


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 1 — yfinance snapshot
# ════════���═══════════════════════════════��═════════════════════════════════════

_YF_FIELDS = {
    "sharesShort":          "shares_short",
    "sharesShortPriorMonth":"shares_short_prior_month",
    "shortPercentOfFloat":  "short_pct_float",
    "shortRatio":           "days_to_cover",
    "floatShares":          "float_shares",
    "sharesOutstanding":    "shares_outstanding",
    "dateShortInterest":    "filing_date_unix",
}


def _fetch_yf_short(symbol: str) -> dict | None:
    try:
        info = yf.Ticker(symbol).info
        if not info or info.get("shortPercentOfFloat") is None:
            return None
        row = {"symbol": symbol}
        for yf_key, col in _YF_FIELDS.items():
            row[col] = info.get(yf_key)
        # Convert unix timestamp to date string
        if row.get("filing_date_unix"):
            row["filing_date"] = datetime.datetime.utcfromtimestamp(
                row["filing_date_unix"]
            ).strftime("%Y-%m-%d")
        else:
            row["filing_date"] = None
        del row["filing_date_unix"]
        row["snapshot_date"] = datetime.date.today().isoformat()
        row["fetched_at"]    = datetime.datetime.utcnow().isoformat()
        return row
    except Exception as e:
        print(f"    yfinance error for {symbol}: {e}")
        return None


def run_yfinance(symbols: list[str]) -> None:
    os.makedirs(DIR_YF, exist_ok=True)
    print(f"\n[yfinance] Fetching short interest snapshot for {len(symbols)} symbols...")

    rows = []
    failed = []
    for i, symbol in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] {symbol}...")
        row = _fetch_yf_short(symbol)
        if row:
            rows.append(row)
        else:
            failed.append(symbol)
        time.sleep(REQUEST_INTERVAL)

    if not rows:
        print("  No data collected.")
        return

    df = pd.DataFrame(rows)
    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    out_path  = os.path.join(DIR_YF, f"short_interest_snapshot_{today_str}.parquet")
    df.to_parquet(out_path, index=False, compression="snappy")

    print(f"\n  Saved {len(df)} rows → {out_path}")
    if failed:
        print(f"  No short data for ({len(failed)}): {', '.join(failed)}")

    cols = [c for c in ["symbol", "short_pct_float", "days_to_cover",
                        "shares_short", "filing_date"] if c in df.columns]
    df_sorted = df.sort_values("short_pct_float", ascending=False, na_position="last")
    print(df_sorted[cols].head(15).to_string(index=False))


# ══════���═══════════════════════════════���═══════════════════════════════════════
# SOURCE 2 — FINRA biweekly Reg SHO short interest
# ══════════════════════════��═══════════════════════════════════════════════════

# FINRA publishes consolidated NMS short interest ~24 times/year.
# Settlement dates fall around the 15th and last business day of each month.
# The CDN URL uses the settlement date; we probe the last few candidates.
FINRA_CDN = "https://cdn.finra.org/equity/regsho/biweekly/CNMSshvol{date}.txt"
FINRA_USER_AGENT = "Mozilla/5.0 (financial-data-pipeline research use)"


def _candidate_settlement_dates(n: int = 6) -> list[str]:
    """
    Generate the last N plausible FINRA settlement dates (15th and last
    business day of each month going backwards from today).
    """
    dates = []
    today = datetime.date.today()
    for months_back in range(3):
        year  = today.year
        month = today.month - months_back
        if month <= 0:
            month += 12
            year  -= 1
        # Mid-month candidate: 15th (move to previous Friday if weekend)
        mid = datetime.date(year, month, 15)
        while mid.weekday() >= 5:
            mid -= datetime.timedelta(days=1)
        dates.append(mid.strftime("%Y%m%d"))
        # End-of-month candidate: last business day
        if month == 12:
            next_month_first = datetime.date(year + 1, 1, 1)
        else:
            next_month_first = datetime.date(year, month + 1, 1)
        eom = next_month_first - datetime.timedelta(days=1)
        while eom.weekday() >= 5:
            eom -= datetime.timedelta(days=1)
        dates.append(eom.strftime("%Y%m%d"))
    return sorted(set(dates), reverse=True)[:n]


def _download_finra_file(date_str: str) -> pd.DataFrame | None:
    url = FINRA_CDN.format(date=date_str)
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": FINRA_USER_AGENT},
            timeout=30,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            print(f"    HTTP {resp.status_code} for {url}")
            return None

        content = resp.text.strip()
        if not content:
            return None

        # FINRA files are pipe-delimited; first line is header
        df = pd.read_csv(
            io.StringIO(content),
            sep="|",
            dtype=str,
            on_bad_lines="skip",
        )

        # Normalise column names regardless of FINRA header casing
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

        rename = {
            "symbol":                   "symbol",
            "issue_name":               "company",
            "issuer_name":              "company",
            "market":                   "market",
            "market_category":          "market",
            "short_interest":           "shares_short",
            "current_short_interest":   "shares_short",
            "days_to_cover":            "days_to_cover",
            "change_in_short_interest": "change_shares",
            "change":                   "change_shares",
            "settlement_date":          "settlement_date",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

        # Coerce numeric columns
        for col in ("shares_short", "days_to_cover", "change_shares"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if "settlement_date" not in df.columns:
            df["settlement_date"] = date_str[:4] + "-" + date_str[4:6] + "-" + date_str[6:]

        df["fetched_at"] = datetime.datetime.utcnow().isoformat()
        return df

    except Exception as e:
        print(f"    Error downloading FINRA file for {date_str}: {e}")
        return None


def run_finra() -> None:
    os.makedirs(DIR_FINRA, exist_ok=True)
    print("\n[FINRA] Probing latest biweekly short interest files...")

    candidates = _candidate_settlement_dates(n=8)
    df = None
    found_date = None

    for date_str in candidates:
        print(f"  Trying {date_str}...", end=" ")
        result = _download_finra_file(date_str)
        if result is not None and not result.empty:
            df = result
            found_date = date_str
            print(f"✓ ({len(df):,} rows)")
            break
        else:
            print("not found")

    if df is None or df.empty:
        print("  Could not retrieve any FINRA short interest file.")
        return

    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    out_path  = os.path.join(DIR_FINRA, f"finra_short_{today_str}.parquet")
    df.to_parquet(out_path, index=False, compression="snappy")
    print(f"  Saved {len(df):,} rows (settlement {found_date}) → {out_path}")

    if "symbol" in df.columns and "shares_short" in df.columns:
        top = df.nlargest(10, "shares_short")[
            [c for c in ["symbol", "company", "market", "shares_short", "days_to_cover"]
             if c in df.columns]
        ]
        print(top.to_string(index=False))


# ═════════════════���════════════════════════════════════════════════════════════
# SOURCE 3 — SEC Fails-to-Deliver
# ═════════════════════════════════���════════════════════════════════════════════

# SEC publishes FTD data in two files per month: {YYYY}{MM}a (days 1-15) and
# {YYYY}{MM}b (days 16-end). Files are zipped pipe-delimited CSV.
SEC_FTD_URL = (
    "https://www.sec.gov/files/data/fails-deliver-data/"
    "cnsfails{year}{month:02d}{half}.zip"
)
SEC_USER_AGENT = "financial-data-pipeline zander.s.luke@gmail.com"

# Standard FTD column names (may vary slightly by vintage)
FTD_RENAME = {
    "settlement date":  "settlement_date",
    "cusip":            "cusip",
    "symbol":           "symbol",
    "quantity (fails)": "shares_failed",
    "description":      "description",
    "price":            "price",
}


def _download_ftd(year: int, month: int, half: str) -> pd.DataFrame | None:
    url = SEC_FTD_URL.format(year=year, month=month, half=half)
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": SEC_USER_AGENT},
            timeout=60,
            stream=True,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as f:
                df = pd.read_csv(
                    f,
                    sep="|",
                    dtype=str,
                    on_bad_lines="skip",
                    encoding="utf-8",
                )

        df.columns = [c.strip().lower() for c in df.columns]
        df = df.rename(columns={k: v for k, v in FTD_RENAME.items() if k in df.columns})

        # Remove SEC totals / blank rows
        if "symbol" in df.columns:
            df = df[df["symbol"].notna() & (df["symbol"].str.strip() != "")]

        for col in ("shares_failed", "price"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df["fetched_at"] = datetime.datetime.utcnow().isoformat()
        return df

    except Exception as e:
        print(f"    Error downloading FTD {year}{month:02d}{half}: {e}")
        return None


def run_ftd() -> None:
    os.makedirs(DIR_FTD, exist_ok=True)
    print("\n[SEC FTD] Fetching latest Fails-to-Deliver data...")

    today  = datetime.date.today()
    # Try current month halves, then prior month
    candidates = []
    for months_back in range(2):
        year  = today.year
        month = today.month - months_back
        if month <= 0:
            month += 12
            year  -= 1
        candidates.append((year, month, "b"))
        candidates.append((year, month, "a"))

    frames = []
    for year, month, half in candidates:
        label = f"{year}{month:02d}{half}"
        print(f"  Trying {label}...", end=" ")
        df = _download_ftd(year, month, half)
        if df is not None and not df.empty:
            frames.append(df)
            print(f"✓ ({len(df):,} rows)")
            if len(frames) >= 2:   # grab both halves of the most recent complete month
                break
        else:
            print("not found")
        time.sleep(0.5)

    if not frames:
        print("  Could not retrieve any SEC FTD file.")
        return

    combined  = pd.concat(frames, ignore_index=True).drop_duplicates()
    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    out_path  = os.path.join(DIR_FTD, f"sec_ftd_{today_str}.parquet")
    combined.to_parquet(out_path, index=False, compression="snappy")
    print(f"  Saved {len(combined):,} FTD rows → {out_path}")

    if "symbol" in combined.columns and "shares_failed" in combined.columns:
        top = combined.nlargest(10, "shares_failed")[
            [c for c in ["settlement_date", "symbol", "description",
                         "shares_failed", "price"] if c in combined.columns]
        ]
        print(top.to_string(index=False))


# ════════════════════════���═════════════════════════════════════════════════════
# Main
# ════��═════════════════════════════════════════════��═══════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Short interest data pipeline (yfinance + FINRA + SEC FTD)"
    )
    parser.add_argument(
        "--source",
        choices=["yfinance", "finra", "ftd", "all"],
        default="yfinance",
        help="Data source to fetch (default: yfinance)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Symbols for yfinance mode (default: DJI + sector ETFs)",
    )
    args = parser.parse_args()

    if args.source in ("yfinance", "all"):
        if args.symbols:
            symbols = [s.upper() for s in args.symbols]
        else:
            dji     = get_dji_symbols()
            symbols = sorted(set(dji + SECTOR_ETFS))
        run_yfinance(symbols)

    if args.source in ("finra", "all"):
        run_finra()

    if args.source in ("ftd", "all"):
        run_ftd()

    print("\n--- PIPELINE RUN COMPLETE ---")


if __name__ == "__main__":
    main()
