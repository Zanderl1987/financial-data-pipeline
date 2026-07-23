#!/usr/bin/env python3
"""
Omkar Cloud Commodity Price Pipeline.

Fetches real-time commodity futures prices from CME/NYMEX via Omkar Cloud API.
Free tier: 100 queries/month. Covers 30 commodities (precious metals, energy,
agriculture, livestock, industrial metals including lumber).

API: https://commodity-price-api.omkar.cloud/commodity-price

Output:
  storage/raw/omkar_commodity/omkar_commodity_{mode}_{YYYYMMDD}.parquet

CLI:
  python omkar_commodity_pipeline.py             # all commodities (1 API call)
  python omkar_commodity_pipeline.py --backfill  # same (no historical on free tier)
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

OMKAR_API_KEY = os.environ.get("OMKAR_API_KEY", "")
BASE_URL = "https://commodity-price-api.omkar.cloud/commodity-price"
STORAGE_DIR = os.path.join("storage", "raw", "omkar_commodity")
REQUEST_INTERVAL = 1.0

# All 30 supported commodity names
COMMODITIES = [
    # Precious metals
    "gold", "silver", "platinum", "palladium", "micro_gold", "micro_silver",
    # Energy
    "crude_oil", "brent_crude_oil", "natural_gas", "gasoline_rbob", "heating_oil",
    # Agriculture
    "wheat", "corn", "soybean", "soybean_oil", "soybean_meal", "oat",
    "rough_rice", "lumber", "coffee", "cocoa", "sugar", "cotton", "orange_juice",
    # Livestock
    "live_cattle", "feeder_cattle", "lean_hogs", "class_3_milk",
    # Industrial metals
    "copper", "aluminum",
]


def fetch_price(commodity: str) -> dict | None:
    """Fetch current price for a single commodity."""
    if not OMKAR_API_KEY:
        return None
    try:
        r = requests.get(
            BASE_URL,
            params={"name": commodity},
            headers={"API-Key": OMKAR_API_KEY},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            print(f"    Rate limited on {commodity}. Stopping.")
            return None
        print(f"    HTTP {r.status_code} for {commodity}")
        return None
    except requests.RequestException as e:
        print(f"    Request error for {commodity}: {e}")
        return None


def main(backfill: bool = False) -> None:
    if not OMKAR_API_KEY:
        print("ERROR: OMKAR_API_KEY not set in .env")
        print("Sign up at https://www.omkar.cloud/auth/sign-up")
        print("Then get your key at https://www.omkar.cloud/api-key")
        return

    os.makedirs(STORAGE_DIR, exist_ok=True)
    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    mode = "backfill" if backfill else "incremental"

    print(f"Omkar Commodity Pipeline  mode={mode}")
    print(f"Fetching {len(COMMODITIES)} commodities...\n")

    rows = []
    for i, commodity in enumerate(COMMODITIES, 1):
        print(f"[{i}/{len(COMMODITIES)}] {commodity}...")
        data = fetch_price(commodity)
        if data:
            rows.append({
                "commodity":     commodity,
                "commodity_name": data.get("commodity_name", ""),
                "exchange":      data.get("exchange", ""),
                "price_usd":     data.get("price_usd"),
                "updated_at":    data.get("updated_at", ""),
                "fetched_at":    now.isoformat(),
            })
            print(f"  {data.get('commodity_name', commodity)}: ${data.get('price_usd')} ({data.get('exchange', '')})")
        else:
            print(f"  No data")
        time.sleep(REQUEST_INTERVAL)

    if not rows:
        print("\nNo data fetched.")
        return

    df = pd.DataFrame(rows)
    path = write_partitioned(df, STORAGE_DIR, f"omkar_commodity_{mode}_{today_str}.parquet")
    print(f"\n-> {path}")
    print(f"   {len(df)} commodities fetched")
    print("\n--- COMPLETE ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Omkar Cloud commodity prices (CME/NYMEX)")
    parser.add_argument("--backfill", action="store_true",
                        help="Same as incremental (no historical on free tier)")
    args = parser.parse_args()
    main(backfill=args.backfill)
