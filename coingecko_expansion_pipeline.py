"""
CoinGecko Expansion Pipeline -- global market cap, top coins, trending,
categories, derivatives, and exchange rates.

Distinct from the existing coingecko_pipeline.py (top-250 crypto snapshot,
crypto_market/crypto_history tables) -- this pipeline covers additional
CoinGecko endpoints and writes to its own coingecko_* tables.

Uses the CoinGecko Demo API (free, no key required for basic use).
Pass COINGECKO_DEMO_API_KEY env var for higher rate limits (30 RPM).

CLI:
  python coingecko_expansion_pipeline.py             # incremental (last 90 days)
  python coingecko_expansion_pipeline.py --backfill  # full available history

Output:
  storage/raw/coingecko/global_market/
  storage/raw/coingecko/coins_markets/
  storage/raw/coingecko/trending/
  storage/raw/coingecko/categories/
  storage/raw/coingecko/derivatives/
  storage/raw/coingecko/exchange_rates/
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

COINGECKO_DEMO_KEY = os.environ.get("COINGECKO_DEMO_API_KEY")
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

BASE_DIR = os.path.join("storage", "raw", "coingecko")

REQUEST_INTERVAL = 2.0
MAX_RETRIES = 3
BACKOFF_SECONDS = 60


def get_headers():
    headers = {"Accept": "application/json"}
    if COINGECKO_DEMO_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_DEMO_KEY
    return headers


def get_with_backoff(url, params=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=get_headers(), timeout=30)
            if resp.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  Rate limited, waiting {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES:
                raise
            wait = BACKOFF_SECONDS * attempt
            print(f"  Error: {e}, retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
    return None


def fetch_global_market():
    print("Fetching global crypto market data...")
    data = get_with_backoff(f"{COINGECKO_BASE}/global")
    if not data or "data" not in data:
        print("  No data returned")
        return pd.DataFrame()
    g = data["data"]
    row = {
        "active_cryptocurrencies": g.get("active_cryptocurrencies"),
        "upcoming_icos": g.get("upcoming_icos"),
        "ended_icos": g.get("ended_icos"),
        "markets": g.get("markets"),
        "total_market_cap_usd": g.get("total_market_cap", {}).get("usd"),
        "total_volume_24h_usd": g.get("total_volume", {}).get("usd"),
        "bitcoin_dominance_pct": g.get("market_cap_percentage", {}).get("btc"),
        "ethereum_dominance_pct": g.get("market_cap_percentage", {}).get("eth"),
        "btc_market_cap_change_24h_pct": g.get("market_cap_change_percentage_24h_usd"),
        "global_defi_market_cap_usd": g.get("defi_market_cap", {}).get("usd"),
        "global_defi_volume_24h_usd": g.get("defi_volume_24h", {}).get("usd"),
        "stablecoin_market_cap_usd": g.get("stablecoin_market_cap", {}).get("usd"),
        "stablecoin_volume_24h_usd": g.get("stablecoin_volume_24h", {}).get("usd"),
        "eth_gas_price_gwei": None,
    }
    df = pd.DataFrame([row])
    df["snapshot_date"] = datetime.date.today().isoformat()
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  1 record (global market snapshot)")
    return df


def fetch_coins_markets(vs_currency="usd", per_page=100, pages=None):
    print(f"Fetching coins/markets (top {per_page} coins)...")
    all_rows = []
    if pages is None:
        pages = [1]
    for page in pages:
        params = {
            "vs_currency": vs_currency,
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "sparkline": "false",
            "price_change_percentage": "1h,24h,7d,30d",
        }
        data = get_with_backoff(f"{COINGECKO_BASE}/coins/markets", params=params)
        if not data:
            continue
        for coin in data:
            row = {
                "coin_id": coin.get("id"),
                "symbol": coin.get("symbol"),
                "name": coin.get("name"),
                "market_cap_rank": coin.get("market_cap_rank"),
                "current_price_usd": coin.get("current_price"),
                "market_cap_usd": coin.get("market_cap"),
                "total_volume_24h_usd": coin.get("total_volume"),
                "high_24h_usd": coin.get("high_24h"),
                "low_24h_usd": coin.get("low_24h"),
                "price_change_24h_usd": coin.get("price_change_24h"),
                "price_change_pct_24h": coin.get("price_change_percentage_24h"),
                "price_change_pct_1h": coin.get("price_change_percentage_1h_in_currency"),
                "price_change_pct_7d": coin.get("price_change_percentage_7d_in_currency"),
                "price_change_pct_30d": coin.get("price_change_percentage_30d_in_currency"),
                "circulating_supply": coin.get("circulating_supply"),
                "total_supply": coin.get("total_supply"),
                "max_supply": coin.get("max_supply"),
                "ath_usd": coin.get("ath"),
                "ath_change_pct": coin.get("ath_change_percentage"),
                "ath_date": coin.get("ath_date"),
                "atl_usd": coin.get("atl"),
                "atl_change_pct": coin.get("atl_change_percentage"),
                "atl_date": coin.get("atl_date"),
                "fully_diluted_valuation_usd": coin.get("fully_diluted_valuation"),
                "last_updated": coin.get("last_updated"),
            }
            all_rows.append(row)
        time.sleep(REQUEST_INTERVAL)
    df = pd.DataFrame(all_rows)
    df["snapshot_date"] = datetime.date.today().isoformat()
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} coins fetched")
    return df


def fetch_trending():
    print("Fetching trending coins/NFTs/categories...")
    data = get_with_backoff(f"{COINGECKO_BASE}/search/trending")
    if not data:
        return pd.DataFrame()
    rows = []
    for item in data.get("coins", []):
        coin = item.get("item", {})
        coin_data = coin.get("data") or {}
        row = {
            "type": "coin",
            "coin_id": coin.get("id"),
            "name": coin.get("name"),
            "symbol": coin.get("symbol"),
            "market_cap_rank": coin.get("market_cap_rank"),
            "price_btc": coin.get("price_btc"),
            "score": coin.get("score"),
            "data_price_usd": coin_data.get("price"),
            "data_market_cap_usd": coin_data.get("market_cap"),
            "data_total_volume_24h_usd": coin_data.get("total_volume"),
            "data_price_change_24h_pct": (coin_data.get("price_change_percentage_24h") or {}).get("usd"),
            "data_market_cap_change_24h_pct": coin_data.get("market_cap_change_percentage_24h"),
            "data_sparkline_7d": None,
        }
        sparkline = coin_data.get("sparkline", {})
        if sparkline and isinstance(sparkline, dict):
            row["data_sparkline_7d"] = str(sparkline.get("price", []))
        elif sparkline and isinstance(sparkline, str):
            row["data_sparkline_7d"] = sparkline
        rows.append(row)
    for item in data.get("nfts", []):
        item_data = item.get("data") or {}
        row = {
            "type": "nft",
            "coin_id": item.get("id"),
            "name": item.get("name"),
            "symbol": item.get("symbol"),
            "market_cap_rank": None,
            "price_btc": None,
            "score": item.get("score"),
            "data_price_usd": item_data.get("price"),
            "data_market_cap_usd": item_data.get("market_cap"),
            "data_total_volume_24h_usd": item_data.get("total_volume"),
            "data_price_change_24h_pct": (item_data.get("price_change_percentage_24h") or {}).get("usd"),
            "data_market_cap_change_24h_pct": None,
            "data_sparkline_7d": None,
        }
        rows.append(row)
    for item in data.get("categories", []):
        row = {
            "type": "category",
            "coin_id": None,
            "name": item.get("name"),
            "symbol": None,
            "market_cap_rank": None,
            "price_btc": None,
            "score": item.get("score"),
            "data_price_usd": None,
            "data_market_cap_usd": None,
            "data_total_volume_24h_usd": None,
            "data_price_change_24h_pct": None,
            "data_market_cap_change_24h_pct": None,
            "data_sparkline_7d": None,
        }
        rows.append(row)
    df = pd.DataFrame(rows)
    df["snapshot_date"] = datetime.date.today().isoformat()
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} trending items ({len(data.get('coins', []))} coins, {len(data.get('nfts', []))} NFTs, {len(data.get('categories', []))} categories)")
    return df


def fetch_categories():
    print("Fetching coin categories with market data...")
    params = {
        "order": "market_cap_desc",
    }
    data = get_with_backoff(f"{COINGECKO_BASE}/coins/categories", params=params)
    if not data:
        return pd.DataFrame()
    rows = []
    for cat in data:
        raw_top3 = cat.get("top_3_coins", [])
        top3_ids = []
        top3_names = []
        top3_prices = []
        top3_mcaps = []
        top3_vols = []
        top3_changes = []
        for c in raw_top3:
            if isinstance(c, dict):
                top3_ids.append(c.get("id", ""))
                top3_names.append(c.get("name", ""))
                top3_prices.append(str(c.get("price", "")))
                top3_mcaps.append(str(c.get("market_cap", "")))
                top3_vols.append(str(c.get("volume_24h", "")))
                top3_changes.append(str(c.get("market_cap_change_24h", "")))
            else:
                top3_ids.append(str(c))
                top3_names.append("")
                top3_prices.append("")
                top3_mcaps.append("")
                top3_vols.append("")
                top3_changes.append("")
        row = {
            "category_id": cat.get("id"),
            "name": cat.get("name"),
            "market_cap_usd": cat.get("market_cap"),
            "market_cap_change_24h_pct": cat.get("market_cap_change_24h"),
            "volume_24h_usd": cat.get("volume_24h"),
            "volume_24h_change_24h_pct": cat.get("volume_24h_change_24h"),
            "top_3_coins_id": ",".join(top3_ids),
            "top_3_coins_names": ",".join(top3_names),
            "top_3_coins_prices": ",".join(top3_prices),
            "top_3_coins_market_caps": ",".join(top3_mcaps),
            "top_3_coins_24h_vol": ",".join(top3_vols),
            "top_3_coins_24h_change": ",".join(top3_changes),
            "coins_count": cat.get("coins_count"),
            "sparkline": str(cat.get("sparkline", [])),
        }
        rows.append(row)
    df = pd.DataFrame(rows)
    df["snapshot_date"] = datetime.date.today().isoformat()
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} categories")
    return df


def fetch_derivatives():
    print("Fetching derivatives tickers...")
    all_rows = []
    params = {"per_page": 100, "page": 1}
    data = get_with_backoff(f"{COINGECKO_BASE}/derivatives", params=params)
    if not data:
        return pd.DataFrame()
    for ticker in data:
        exchange = ticker.get("exchange", {})
        row = {
            "symbol": ticker.get("symbol"),
            "base": ticker.get("base"),
            "target": ticker.get("target"),
            "trade_volume_24h_btc": ticker.get("trade_volume_24h_btc"),
            "trade_volume_24h_btc_normalized": ticker.get("trade_volume_24h_btc_normalized"),
            "bid_ask_spread_percentage": ticker.get("bid_ask_spread_percentage"),
            "funding_rate": ticker.get("funding_rate"),
            "price": ticker.get("price"),
            "index_price": ticker.get("index_price"),
            "index_price_date": ticker.get("index_price_date"),
            "exchange_name": exchange.get("name"),
            "exchange_logo": exchange.get("logo"),
            "exchange_centralization": exchange.get("centralization"),
            "exchange_trust_score_rank": exchange.get("trust_score_rank"),
            "exchange_country": exchange.get("country"),
            "exchange_established_year": exchange.get("year_established"),
            "last_updated": ticker.get("last_updated"),
        }
        all_rows.append(row)
    df = pd.DataFrame(all_rows)
    df["snapshot_date"] = datetime.date.today().isoformat()
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} derivatives tickers")
    return df


def fetch_exchange_rates():
    print("Fetching BTC exchange rates...")
    data = get_with_backoff(f"{COINGECKO_BASE}/exchange_rates")
    if not data or "rates" not in data:
        return pd.DataFrame()
    rows = []
    for currency, info in data["rates"].items():
        row = {
            "currency": currency,
            "name": info.get("name"),
            "unit": info.get("unit"),
            "value": info.get("value"),
            "type": info.get("type"),
        }
        rows.append(row)
    df = pd.DataFrame(rows)
    df["snapshot_date"] = datetime.date.today().isoformat()
    df["fetched_at"] = pd.Timestamp.utcnow().isoformat()
    print(f"  {len(df)} exchange rates")
    return df


def run_pipeline(backfill=False):
    today = datetime.date.today()
    run_ts = today.isoformat()

    print("=" * 60)
    print("CoinGecko Crypto Market Pipeline")
    print(f"  Mode: {'backfill' if backfill else 'incremental'}")
    print(f"  Date: {run_ts}")
    print("=" * 60)

    tables = {
        "global_market":       fetch_global_market(),
        "coins_markets":       fetch_coins_markets(vs_currency="usd", per_page=100, pages=[1, 2, 3] if backfill else [1]),
        "trending":            fetch_trending(),
        "categories":          fetch_categories(),
        "derivatives":         fetch_derivatives(),
        "exchange_rates":      fetch_exchange_rates(),
    }

    mode = "backfill" if backfill else "incremental"

    for table_name, df in tables.items():
        if df.empty:
            print(f"  Skipping {table_name} (no data)")
            continue
        output_dir = os.path.join(BASE_DIR, table_name)
        filename = f"coingecko_{table_name}_{mode}_{today:%Y%m%d}.parquet"
        out = write_partitioned(
            df,
            output_dir=output_dir,
            filename=filename,
        )
        print(f"  Wrote {table_name}: {out}")

    print("=" * 60)
    print("CoinGecko pipeline complete.")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="CoinGecko Crypto Market Pipeline")
    parser.add_argument("--backfill", action="store_true", help="Full history vs incremental")
    args = parser.parse_args()
    run_pipeline(backfill=args.backfill)


if __name__ == "__main__":
    main()
