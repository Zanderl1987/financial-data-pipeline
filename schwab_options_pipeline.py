#!/usr/bin/env python3
"""
Schwab Options Pipeline:
  Options chains with full greeks (delta, gamma, theta, vega, rho) for
  target symbols. The Schwab /chains endpoint returns richer data than
  Yahoo Finance — greeks are always present and reflect real market pricing.

  Fetches the ENTIRE listed chain per symbol: every expiration, every strike,
  calls and puts (widened 2026-10-01 from 40 strikes around ATM / 4 weeks out,
  so the daily snapshots record when exchanges add new strikes beyond the
  existing range). Chains too big for one Schwab response (SPY, QQQ -- Schwab
  answers 502 "Body buffer overflow") are fetched as expiration-date windows,
  bisected until each window fits.

CLI:
  python schwab_options_pipeline.py                         # default symbols, full chains
  python schwab_options_pipeline.py --symbols NVDA TSLA AAPL
  python schwab_options_pipeline.py --expirations 4         # only expirations within 4 weeks

Output:
  storage/raw/schwab/options/schwab_options_{mode}_{YYYYMMDD}.parquet

Schema:
  symbol | contract_symbol | non_standard |
  put_call | expiration_date | days_to_expiration | strike |
  bid | ask | last | mark | volume | open_interest |
  delta | gamma | theta | vega | rho |
  implied_volatility | in_the_money | intrinsic_value | time_value |
  underlying_price | snapshot_date | fetched_at

  snapshot_date (market-time trading day, added 2026-08-11) is part of the
  curated dedup key. Schwab serves no options history, so this table's history
  exists only because each daily run appends another snapshot -- snapshot_date
  is what makes each one a distinct fact rather than a re-fetch of the last.
"""

import os
import time
import datetime
import argparse
from zoneinfo import ZoneInfo

import pandas as pd
import schwabdev
from schwab_auth import preflight
from dotenv import load_dotenv
from storage_utils import write_partitioned
from symbol_universe import get_broad_universe

load_dotenv()

API_KEY      = os.environ["SCHWAB_API_KEY"]
APP_SECRET   = os.environ["SCHWAB_APP_SECRET"]
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH   = os.environ.get("SCHWAB_TOKEN_PATH", "tokens.db")

OUTPUT_DIR = os.path.join("storage", "raw", "schwab", "options")

# snapshot_date is stamped in market time, not UTC. Schwab has no historical
# options endpoint -- this table's history exists ONLY because the daily job
# accumulates snapshots -- so the snapshot's trading day is the fact being
# recorded. A chain pulled at 21:00 ET belongs to that session, but is already
# tomorrow in UTC, so a UTC-derived date would file it under a day the market
# never opened. Deliberately no fallback if tzdata is unavailable: a loud
# ZoneInfo error is far better than silently reverting to UTC dates, which
# would corrupt the key that the whole history depends on.
MARKET_TZ = ZoneInfo("America/New_York")

# Fallback if --symbols isn't passed and the broad universe can't be resolved
# (e.g. no IVV holdings snapshot yet). Real default is the S&P 500 (via
# get_broad_universe(), Schwab has no daily quota to ration against) --
# accepted tradeoff: full chains at this size add up in storage fast.
FALLBACK_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "JPM", "SPY", "QQQ",
]

MAX_RETRIES     = 3
BACKOFF_SECONDS = 30
REQUEST_INTERVAL = 0.5
# schwabdev's default 10s read timeout is too short for full chains (multi-MB
# JSON bodies); measured 2026-10-01: AAPL 4.2MB/3s, TSLA 6.0MB/2.8s.
CLIENT_TIMEOUT = 30
# Farthest expiration requested when a chain has to be split into date windows.
# Equity LEAPS list out ~2.5-3 years; 4 years leaves headroom.
MAX_HORIZON_DAYS = 4 * 365


def _flatten_contracts(contracts: dict, symbol: str, underlying_price: float) -> list[dict]:
    """
    Flatten one side (calls or puts) of the Schwab option_chain expDateMap.

    The nested structure is:
        { "YYYY-MM-DD:DTE": { "strike": [contract, ...], ... }, ... }
    """
    rows = []
    for exp_key, strikes in contracts.items():
        # exp_key format: "2024-03-15:20"  (date:days_to_expiration)
        parts = exp_key.split(":")
        exp_date = parts[0]
        dte = int(parts[1]) if len(parts) > 1 else None

        for strike_str, contract_list in strikes.items():
            for c in contract_list:
                rows.append({
                    "symbol":              symbol,
                    # OCC symbol tells a regular contract from an adjusted one
                    # at the same strike (e.g. "FDX1" after the FedEx Freight
                    # spinoff); without it the two collide on every key column.
                    "contract_symbol":     c.get("symbol"),
                    "non_standard":        c.get("nonStandard"),
                    "put_call":            c.get("putCall"),
                    "expiration_date":     exp_date,
                    "days_to_expiration":  dte,
                    "strike":              float(strike_str),
                    "bid":                 c.get("bid"),
                    "ask":                 c.get("ask"),
                    "last":                c.get("last"),
                    "mark":                c.get("mark"),
                    "volume":              c.get("totalVolume"),
                    "open_interest":       c.get("openInterest"),
                    "delta":               c.get("delta"),
                    "gamma":               c.get("gamma"),
                    "theta":               c.get("theta"),
                    "vega":                c.get("vega"),
                    "rho":                 c.get("rho"),
                    "implied_volatility":  c.get("volatility"),
                    "in_the_money":        c.get("inTheMoney"),
                    "intrinsic_value":     c.get("intrinsicValue"),
                    "time_value":          c.get("timeValue"),
                    "underlying_price":    underlying_price,
                })
    return rows


class _TooBig(Exception):
    """Schwab refused the response as too large (502 protocol.http.TooBigBody)."""


def _request_chain(client, symbol: str, from_dt=None, to_dt=None) -> dict | None:
    """One /chains call: all strikes, optionally limited to an expiration-date
    window. Returns the JSON body, None on a non-retryable failure, and raises
    _TooBig when the window must be split."""
    kwargs = {}
    if from_dt is not None:
        kwargs["fromDate"] = from_dt.isoformat()
    if to_dt is not None:
        kwargs["toDate"] = to_dt.isoformat()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.option_chains(
                symbol=symbol,
                contractType="ALL",
                range="ALL",           # every strike, not a window around ATM
                includeUnderlyingQuote=True,
                **kwargs,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") != "SUCCESS":
                    print(f"  {symbol}: API status={data.get('status')}")
                    return None
                return data

            if resp.status_code == 502 and "TooBigBody" in resp.text:
                raise _TooBig()
            if resp.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 rate limit for {symbol}. Backing off {wait}s.")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code} for {symbol}: {resp.text[:120]}")
                return None

        except _TooBig:
            raise
        except Exception as e:
            print(f"  Error fetching {symbol} (attempt {attempt}): {e}")
            time.sleep(BACKOFF_SECONDS)

    return None


def _request_window(client, symbol: str, from_dt, to_dt) -> list[dict] | None:
    """Fetch [from_dt, to_dt], bisecting the date range while Schwab says the
    response is too big. Returns the list of chain bodies, or None if any
    piece failed -- a partial chain would look like strikes being delisted."""
    try:
        data = _request_chain(client, symbol, from_dt, to_dt)
        return None if data is None else [data]
    except _TooBig:
        if from_dt >= to_dt:
            print(f"  {symbol}: single expiration day {from_dt} still too big")
            return None
        mid = from_dt + (to_dt - from_dt) // 2
        time.sleep(REQUEST_INTERVAL)
        left = _request_window(client, symbol, from_dt, mid)
        if left is None:
            return None
        time.sleep(REQUEST_INTERVAL)
        right = _request_window(client, symbol, mid + datetime.timedelta(days=1), to_dt)
        if right is None:
            return None
        return left + right


def fetch_option_chain(
    client,
    symbol: str,
    weeks_out: int | None = None,
) -> pd.DataFrame | None:
    """Fetch the options chain for a symbol and return a flat DataFrame.

    weeks_out=None (default) takes every listed expiration; an int limits the
    snapshot to expirations within that many weeks."""
    if weeks_out is None:
        try:
            data = _request_chain(client, symbol)
            bodies = None if data is None else [data]
        except _TooBig:
            today = datetime.date.today()
            print(f"    chain too big for one response -- splitting by expiration")
            bodies = _request_window(
                client, symbol, today, today + datetime.timedelta(days=MAX_HORIZON_DAYS)
            )
    else:
        today = datetime.date.today()
        bodies = _request_window(
            client, symbol, today, today + datetime.timedelta(weeks=weeks_out)
        )

    if not bodies:
        return None

    rows = []
    for data in bodies:
        underlying_price = (
            data.get("underlyingPrice") or
            (data.get("underlying") or {}).get("last", 0.0)
        )
        rows += _flatten_contracts(data.get("callExpDateMap", {}), symbol, underlying_price)
        rows += _flatten_contracts(data.get("putExpDateMap",  {}), symbol, underlying_price)

    if not rows:
        return None

    df = pd.DataFrame(rows)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    # Keep fetched_at byte-identical to what it always was (naive UTC
    # isoformat) -- changing it would alter a column downstream code
    # already parses.
    df["fetched_at"] = now_utc.replace(tzinfo=None).isoformat()
    df["snapshot_date"] = now_utc.astimezone(MARKET_TZ).date().isoformat()
    return df


def main():
    parser = argparse.ArgumentParser(description="Schwab options chain pipeline with greeks")
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Symbols to fetch (default: S&P 500 via IVV holdings)"
    )
    parser.add_argument(
        "--expirations", type=int, default=None,
        help="Only expirations within this many weeks (default: all expirations)"
    )
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    preflight()

    client = schwabdev.Client(
        app_key=API_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        tokens_db=TOKEN_PATH,
        timeout=CLIENT_TIMEOUT,
    )

    symbols  = [s.upper() for s in (args.symbols or get_broad_universe(extra=FALLBACK_SYMBOLS))]
    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    horizon = (f"{args.expirations} weeks out" if args.expirations
               else "full chain, all expirations")
    print(f"Fetching options chains for {len(symbols)} symbols ({horizon})...")

    frames = []
    failed = []

    for i, symbol in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] {symbol}...")
        df = fetch_option_chain(client, symbol, weeks_out=args.expirations)
        if df is not None:
            frames.append(df)
            print(f"    {len(df):,} contracts")
        else:
            failed.append(symbol)
        time.sleep(REQUEST_INTERVAL)

    if not frames:
        print("No data collected. Exiting.")
        return

    combined = pd.concat(frames, ignore_index=True)
    out_path = write_partitioned(combined, OUTPUT_DIR, f"schwab_options_incremental_{today_str}.parquet")

    print(f"\n--- COMPLETE ---")
    print(f"Saved {len(combined):,} contracts for {len(frames)} symbols -> {out_path}")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}")

    sample = combined[combined["delta"].notna()][
        ["symbol", "put_call", "expiration_date", "strike",
         "mark", "delta", "implied_volatility", "open_interest"]
    ].head(10)
    print(sample.to_string(index=False))


if __name__ == "__main__":
    main()
