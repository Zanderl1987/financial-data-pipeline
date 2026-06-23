#!/usr/bin/env python3
"""
CoinGecko Crypto Pipeline.

Fetches cryptocurrency market data from CoinGecko's public API:
  - Market snapshot — top 250 coins: price, market cap, volume, 24h change
  - Historical OHLCV — daily candlestick data for top 50 coins

Optionally uses COINGECKO_API_KEY from .env for higher rate limits.
Without a key the demo tier is used (free, ~30 req/min).

Register free at https://www.coingecko.com/en/api

CLI:
  python coingecko_pipeline.py             # snapshot + 90-day history
  python coingecko_pipeline.py --backfill  # snapshot + 365-day history

Outputs:
  storage/raw/crypto/market/coingecko_market_{mode}_{YYYYMMDD}.parquet
  storage/raw/crypto/history/coingecko_history_{mode}_{YYYYMMDD}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from storage_utils import write_partitioned

load_dotenv()

COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
BASE_URL    = "https://api.coingecko.com/api/v3"
MARKET_DIR  = os.path.join("storage", "raw", "crypto", "market")
HISTORY_DIR = os.path.join("storage", "raw", "crypto", "history")

REQUEST_INTERVAL = 2.5 if not COINGECKO_API_KEY else 1.0
MAX_RETRIES  = 3
TOP_N_MARKET = 250
TOP_N_HISTORY = 50


def _headers():
    h = {"accept": "application/json"}
    if COINGECKO_API_KEY:
        h["x-cg-demo-api-key"] = COINGECKO_API_KEY
    return h


def _get(url, params=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=_headers(), timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  429 rate limit. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(30 * attempt)
    return None


def fetch_market_snapshot():
    rows = []
    for page in range(1, 4):
        data = _get(f"{BASE_URL}/coins/markets", params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 100,
            "page": page,
            "sparkline": "false",
            "price_change_percentage": "1h,24h,7d,30d",
        })
        if not data:
            break
        rows.extend(data)
        time.sleep(REQUEST_INTERVAL)
        if len(rows) >= TOP_N_MARKET:
            break
    return rows[:TOP_N_MARKET]


def market_to_df(rows, fetched_at):
    records = []
    for r in rows:
        records.append({
            "coin_id":               r.get("id"),
            "symbol":                (r.get("symbol") or "").upper(),
            "name":                  r.get("name"),
            "market_cap_rank":       r.get("market_cap_rank"),
            "price_usd":             r.get("current_price"),
            "market_cap_usd":        r.get("market_cap"),
            "volume_24h_usd":        r.get("total_volume"),
            "high_24h_usd":          r.get("high_24h"),
            "low_24h_usd":           r.get("low_24h"),
            "price_change_pct_1h":   r.get("price_change_percentage_1h_in_currency"),
            "price_change_pct_24h":  r.get("price_change_percentage_24h_in_currency"),
            "price_change_pct_7d":   r.get("price_change_percentage_7d_in_currency"),
            "price_change_pct_30d":  r.get("price_change_percentage_30d_in_currency"),
            "circulating_supply":    r.get("circulating_supply"),
            "total_supply":          r.get("total_supply"),
            "ath_usd":               r.get("ath"),
            "ath_date":              r.get("ath_date"),
            "fetched_at":            fetched_at,
        })
    return pd.DataFrame(records)


def fetch_ohlcv(coin_id, days):
    data = _get(f"{BASE_URL}/coins/{coin_id}/ohlc", params={
        "vs_currency": "usd",
        "days": str(days),
    })
    time.sleep(REQUEST_INTERVAL)
    return data


def ohlcv_to_df(coin_id, symbol, name, raw, fetched_at):
    if not raw:
        return pd.DataFrame()
    rows = []
    for entry in raw:
        ts_ms, o, h, l, c = entry
        date = datetime.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
        rows.append({
            "coin_id":    coin_id,
            "symbol":     symbol,
            "name":       name,
            "date":       date,
            "open":       o,
            "high":       h,
            "low":        l,
            "close":      c,
            "fetched_at": fetched_at,
        })
    return pd.DataFrame(rows).drop_duplicates(subset=["coin_id", "date"])


def main():
    parser = argparse.ArgumentParser(description="CoinGecko crypto market pipeline")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch 365-day history (default: 90 days)")
    args = parser.parse_args()

    now        = datetime.datetime.utcnow()
    today_str  = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode       = "backfill" if args.backfill else "incremental"
    days       = 365 if args.backfill else 90

    key_info = f"key={'yes' if COINGECKO_API_KEY else 'no (add COINGECKO_API_KEY to .env for higher limits)'}"
    print(f"CoinGecko Pipeline  mode={mode}  history={days}d  {key_info}")

    os.makedirs(MARKET_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)

    # ── Market snapshot ────────────────────────────────────────────────────────
    print(f"\n[crypto_market]  Fetching top {TOP_N_MARKET} coins by market cap...")
    raw_market = fetch_market_snapshot()
    if raw_market:
        df_market = market_to_df(raw_market, fetched_at)
        path = write_partitioned(df_market, MARKET_DIR,
                                 f"coingecko_market_{mode}_{today_str}.parquet")
        print(f"  -> {path}  ({len(df_market)} coins)")
        top_coins = df_market[["coin_id", "symbol", "name"]].head(TOP_N_HISTORY).to_dict("records")
    else:
        print("  No market data returned.")
        top_coins = []

    # ── Historical OHLCV ───────────────────────────────────────────────────────
    print(f"\n[crypto_history]  Fetching {days}-day OHLCV for top {len(top_coins)} coins...")
    history_frames = []
    for i, coin in enumerate(top_coins, 1):
        cid = coin["coin_id"]
        sym = coin["symbol"]
        raw = fetch_ohlcv(cid, days)
        if raw:
            df_h = ohlcv_to_df(cid, sym, coin["name"], raw, fetched_at)
            if not df_h.empty:
                history_frames.append(df_h)
        if i % 10 == 0:
            print(f"  {i}/{len(top_coins)} coins processed...")

    if history_frames:
        df_hist = pd.concat(history_frames, ignore_index=True)
        path = write_partitioned(df_hist, HISTORY_DIR,
                                 f"coingecko_history_{mode}_{today_str}.parquet")
        print(f"  -> {path}  ({len(df_hist):,} rows, {df_hist['coin_id'].nunique()} coins)")
    else:
        print("  No historical data returned.")

    print("\n--- COINGECKO PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
