#!/usr/bin/env python3
"""
Futures pipeline:
  - Continuous front-month OHLCV for 28 contracts via yfinance (free, no key required)
  - CFTC Commitments of Traders weekly positioning data (free, no key required)

Outputs:
  storage/raw/futures/futures_ohlcv_{mode}_{YYYYMMDD}.parquet
  storage/raw/cot/cot_{mode}_{YYYYMMDD}.parquet

Usage:
  python futures_pipeline.py --backfill       # Full history (first run)
  python futures_pipeline.py                  # Recent data only (daily cron)
  python futures_pipeline.py --skip-cot       # yfinance OHLCV only
  python futures_pipeline.py --skip-futures   # COT only
"""

import datetime
import os
import argparse
import time
import warnings

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

FUTURES_DIR = os.path.join("storage", "raw", "futures")
COT_DIR = os.path.join("storage", "raw", "cot")
REQUEST_INTERVAL = 0.5  # courtesy delay between yfinance calls

# Continuous front-month futures — Yahoo Finance [SYMBOL]=F format
FUTURES = {
    # Energy
    "CL=F":  ("WTI Crude Oil",         "energy"),
    "NG=F":  ("Natural Gas",            "energy"),
    "RB=F":  ("RBOB Gasoline",          "energy"),
    "HO=F":  ("Heating Oil",            "energy"),
    # Metals
    "GC=F":  ("Gold",                   "metals"),
    "SI=F":  ("Silver",                 "metals"),
    "HG=F":  ("Copper",                 "metals"),
    "PL=F":  ("Platinum",               "metals"),
    "PA=F":  ("Palladium",              "metals"),
    # Agriculture
    "ZC=F":  ("Corn",                   "agriculture"),
    "ZS=F":  ("Soybeans",               "agriculture"),
    "ZW=F":  ("Wheat",                  "agriculture"),
    "KC=F":  ("Coffee",                 "agriculture"),
    "SB=F":  ("Sugar",                  "agriculture"),
    "CT=F":  ("Cotton",                 "agriculture"),
    # Equity index futures
    "ES=F":  ("S&P 500 E-mini",         "equity_index"),
    "NQ=F":  ("NASDAQ 100 E-mini",      "equity_index"),
    "YM=F":  ("Dow Jones E-mini",       "equity_index"),
    "RTY=F": ("Russell 2000 E-mini",    "equity_index"),
    # Treasury futures
    "ZB=F":  ("30Y T-Bond",             "rates"),
    "ZN=F":  ("10Y T-Note",             "rates"),
    "ZF=F":  ("5Y T-Note",              "rates"),
    "ZT=F":  ("2Y T-Note",              "rates"),
    # FX futures
    "6E=F":  ("EUR/USD",                "fx"),
    "6J=F":  ("JPY/USD",                "fx"),
    "6B=F":  ("GBP/USD",                "fx"),
    "6C=F":  ("CAD/USD",                "fx"),
    "6A=F":  ("AUD/USD",                "fx"),
}

# Explicit column mapping: raw CFTC name -> output name
# Keeping only the "(All)" variants (combined futures+options) to avoid duplication
COT_COLUMN_MAP = {
    "Market and Exchange Names":              "market",
    "As of Date in Form YYYY-MM-DD":          "date",
    "Open Interest (All)":                    "open_interest",
    "Noncommercial Positions-Long (All)":     "noncomm_long",
    "Noncommercial Positions-Short (All)":    "noncomm_short",
    "Noncommercial Positions-Spreading (All)":"noncomm_spread",
    "Commercial Positions-Long (All)":        "comm_long",
    "Commercial Positions-Short (All)":       "comm_short",
    "Nonreportable Positions-Long (All)":     "nonrept_long",
    "Nonreportable Positions-Short (All)":    "nonrept_short",
    "Change in Open Interest (All)":          "change_oi",
    "Change in Noncommercial-Long (All)":     "change_noncomm_long",
    "Change in Noncommercial-Short (All)":    "change_noncomm_short",
    "Change in Commercial-Long (All)":        "change_comm_long",
    "Change in Commercial-Short (All)":       "change_comm_short",
    "% of OI-Noncommercial-Long (All)":       "pct_noncomm_long",
    "% of OI-Noncommercial-Short (All)":      "pct_noncomm_short",
    "% of OI-Commercial-Long (All)":          "pct_comm_long",
    "% of OI-Commercial-Short (All)":         "pct_comm_short",
    "Traders-Total (All)":                    "traders_total",
    "Traders-Noncommercial-Long (All)":       "traders_noncomm_long",
    "Traders-Noncommercial-Short (All)":      "traders_noncomm_short",
    "Traders-Commercial-Long (All)":          "traders_comm_long",
    "Traders-Commercial-Short (All)":         "traders_comm_short",
}


def fetch_futures_ohlcv(backfill: bool) -> pd.DataFrame | None:
    period = "max" if backfill else "90d"
    rows = []
    failed = []
    fetch_ts = datetime.datetime.utcnow().isoformat()
    total = len(FUTURES)

    for i, (symbol, (name, category)) in enumerate(FUTURES.items(), 1):
        print(f"[{i}/{total}] {symbol} - {name}...", end=" ", flush=True)
        try:
            hist = yf.Ticker(symbol).history(period=period, auto_adjust=True)
            if hist.empty:
                print("no data")
                failed.append(symbol)
            else:
                hist = hist.reset_index()
                hist.columns = [c.lower() for c in hist.columns]
                hist["symbol"] = symbol
                hist["name"] = name
                hist["category"] = category
                hist["fetched_at"] = fetch_ts
                keep = ["date", "symbol", "name", "category",
                        "open", "high", "low", "close", "volume", "fetched_at"]
                rows.append(hist[[c for c in keep if c in hist.columns]])
                date_min = hist["date"].min()
                date_max = hist["date"].max()
                if hasattr(date_min, "date"):
                    date_min = date_min.date()
                    date_max = date_max.date()
                print(f"{len(hist)} rows ({date_min} to {date_max})")
        except Exception as e:
            print(f"error: {e}")
            failed.append(symbol)
        time.sleep(REQUEST_INTERVAL)

    if failed:
        print(f"\nFailed/empty ({len(failed)}): {', '.join(failed)}")
    if not rows:
        return None
    return pd.concat(rows, ignore_index=True)


def _select_cot_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Rename and select columns using the explicit COT_COLUMN_MAP."""
    rename = {k: v for k, v in COT_COLUMN_MAP.items() if k in df.columns}
    df = df.rename(columns=rename)
    keep = [v for v in COT_COLUMN_MAP.values() if v in df.columns]
    df = df[keep].copy()

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])

    # Net positioning: speculator/hedger net long (positive = net long)
    if "noncomm_long" in df.columns and "noncomm_short" in df.columns:
        df["net_noncomm"] = df["noncomm_long"] - df["noncomm_short"]
    if "comm_long" in df.columns and "comm_short" in df.columns:
        df["net_comm"] = df["comm_long"] - df["comm_short"]

    return df


def fetch_cot(backfill: bool) -> pd.DataFrame | None:
    try:
        import cot_reports as cot
    except ImportError:
        print("  cot_reports not installed. Run: pip install cot_reports")
        return None

    fetch_ts = datetime.datetime.utcnow().isoformat()

    try:
        if backfill:
            print("Downloading full COT history (1986-present) — may take a few minutes...")
            df = cot.cot_all(cot_report_type="legacy_futopt")
        else:
            year = datetime.datetime.utcnow().year
            print(f"Downloading COT data for {year}...")
            df = cot.cot_year(year=year, cot_report_type="legacy_futopt")
    except Exception as e:
        print(f"  COT download failed: {e}")
        return None

    if df is None or df.empty:
        print("  No COT data returned.")
        return None

    df = _select_cot_cols(df)
    df["fetched_at"] = fetch_ts

    markets = df["market"].nunique() if "market" in df.columns else "?"
    date_range = ""
    if "date" in df.columns:
        date_range = f" ({df['date'].min().date()} to {df['date'].max().date()})"
    print(f"  {len(df)} rows, {markets} markets{date_range}")
    return df


def main(backfill=False, skip_futures=False, skip_cot=False):
    os.makedirs(FUTURES_DIR, exist_ok=True)
    os.makedirs(COT_DIR, exist_ok=True)

    today = datetime.datetime.utcnow().strftime("%Y%m%d")
    mode_tag = "backfill" if backfill else "incremental"
    print(f"Mode: {'BACKFILL (full history)' if backfill else 'INCREMENTAL (recent data only)'}")

    if not skip_futures:
        print("\n=== FUTURES OHLCV (yfinance) ===")
        df = fetch_futures_ohlcv(backfill=backfill)
        if df is not None and not df.empty:
            path = os.path.join(FUTURES_DIR, f"futures_ohlcv_{mode_tag}_{today}.parquet")
            df.to_parquet(path, index=False, compression="snappy")
            print(f"\nFutures -> {path} ({len(df)} rows, {df['symbol'].nunique()} contracts)")
        else:
            print("No futures data written.")

    if not skip_cot:
        print("\n=== CFTC COT POSITIONING ===")
        df = fetch_cot(backfill=backfill)
        if df is not None and not df.empty:
            path = os.path.join(COT_DIR, f"cot_{mode_tag}_{today}.parquet")
            df.to_parquet(path, index=False, compression="snappy")
            print(f"COT -> {path} ({len(df)} rows)")
        else:
            print("No COT data written.")

    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Futures OHLCV + CFTC COT positioning pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full available history (use on first run).")
    parser.add_argument("--skip-futures", action="store_true",
                        help="Skip yfinance futures OHLCV download.")
    parser.add_argument("--skip-cot", action="store_true",
                        help="Skip CFTC COT positioning download.")
    args = parser.parse_args()
    main(backfill=args.backfill, skip_futures=args.skip_futures, skip_cot=args.skip_cot)
