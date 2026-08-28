#!/usr/bin/env python3
"""
DefiLlama protocol fundamentals pipeline — keyless DeFi "fundamentals" data
(TVL, fees/revenue, stablecoin supply) complementing the CoinGecko price
tables already collected.

  CATALOG tables:
    defillama_protocols    — per-protocol current TVL + chain breakdown (snapshot)
    defillama_fees         — per-protocol fees/revenue 24h/7d/30d/1y (snapshot)
    defillama_stablecoins  — per-stablecoin circulating supply, current + trailing (snapshot)

Source notes (probed live 2026-08-26):
  - GET https://api.llama.fi/protocols            — 8,132 protocols, one call.
  - GET https://api.llama.fi/overview/fees          — 2,618 protocols' fees/revenue
    aggregated server-side, one call (no per-protocol iteration needed).
  - GET https://stablecoins.llama.fi/stablecoins?includePrices=true — one call.
  - All keyless, no auth, no rate limit hit during vetting.
  - These are CURRENT-SNAPSHOT-ONLY endpoints (no history param) — same pattern
    as tradingview_pipeline.py / schwab_movers_pipeline.py. Run daily to
    accumulate a time series; --backfill is a no-op alias for the same pull.
  - Not built in v1 (documented follow-up): yields.llama.fi/pools (17k pool
    rows, 11.7MB) — larger scope, lower priority than protocol/fee/stablecoin
    fundamentals.

CLI:
  python defillama_pipeline.py              # pull all 3 snapshots
  python defillama_pipeline.py --backfill   # same (no history exists upstream)

Outputs:
  storage/raw/defillama/protocols/year=YYYY/month=MM/defillama_protocols_{mode}_{date}.parquet
  storage/raw/defillama/fees/year=YYYY/month=MM/defillama_fees_{mode}_{date}.parquet
  storage/raw/defillama/stablecoins/year=YYYY/month=MM/defillama_stablecoins_{mode}_{date}.parquet
"""

import argparse
import datetime
import os
import time

import pandas as pd
import requests
from storage_utils import write_partitioned

BASE_DIR = "storage/raw/defillama"
PROTOCOLS_DIR = os.path.join(BASE_DIR, "protocols")
FEES_DIR = os.path.join(BASE_DIR, "fees")
STABLECOINS_DIR = os.path.join(BASE_DIR, "stablecoins")

PROTOCOLS_URL = "https://api.llama.fi/protocols"
FEES_URL = "https://api.llama.fi/overview/fees"
STABLECOINS_URL = "https://stablecoins.llama.fi/stablecoins?includePrices=true"

RETRIES = 4


def _get_json(url: str) -> dict | list:
    last_exc: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, timeout=120,
                                headers={"User-Agent": "financial-data-pipeline/1.0"})
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 10 * attempt
                print(f"    429 - backing off {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:150]}")
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(3 * attempt)
    raise RuntimeError(f"GET {url} failed after {RETRIES} attempts ({last_exc})")


def fetch_protocols() -> pd.DataFrame:
    data = _get_json(PROTOCOLS_URL)
    rows = []
    for p in data:
        rows.append({
            "protocol_id": p.get("id"),
            "name": p.get("name"),
            "slug": p.get("slug"),
            "symbol": p.get("symbol"),
            "category": p.get("category"),
            "chain": p.get("chain"),
            "num_chains": len(p.get("chains") or []),
            "tvl": p.get("tvl"),
            "change_1h": p.get("change_1h"),
            "change_1d": p.get("change_1d"),
            "change_7d": p.get("change_7d"),
            "gecko_id": p.get("gecko_id"),
            "audits": p.get("audits"),
            "listed_at": p.get("listedAt"),
        })
    return pd.DataFrame(rows)


def fetch_fees() -> pd.DataFrame:
    data = _get_json(FEES_URL)
    rows = []
    for p in data.get("protocols", []):
        rows.append({
            "protocol_id": p.get("defillamaId"),
            "name": p.get("name"),
            "slug": p.get("slug"),
            "category": p.get("category"),
            "protocol_type": p.get("protocolType"),
            "total_24h": p.get("total24h"),
            "total_7d": p.get("total7d"),
            "total_30d": p.get("total30d"),
            "total_1y": p.get("total1y"),
            "total_all_time": p.get("totalAllTime"),
            "change_1d": p.get("change_1d"),
            "change_7d": p.get("change_7d"),
        })
    return pd.DataFrame(rows)


def fetch_stablecoins() -> pd.DataFrame:
    data = _get_json(STABLECOINS_URL)
    rows = []
    for a in data.get("peggedAssets", []):
        circ = a.get("circulating") or {}
        prev_day = a.get("circulatingPrevDay") or {}
        prev_week = a.get("circulatingPrevWeek") or {}
        prev_month = a.get("circulatingPrevMonth") or {}

        def _first_val(d: dict):
            return next(iter(d.values()), None) if d else None

        rows.append({
            "stablecoin_id": a.get("id"),
            "name": a.get("name"),
            "symbol": a.get("symbol"),
            "gecko_id": a.get("gecko_id"),
            "peg_type": a.get("pegType"),
            "peg_mechanism": a.get("pegMechanism"),
            "circulating": _first_val(circ),
            "circulating_prev_day": _first_val(prev_day),
            "circulating_prev_week": _first_val(prev_week),
            "circulating_prev_month": _first_val(prev_month),
        })
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="DefiLlama protocol fundamentals (keyless)")
    parser.add_argument("--backfill", action="store_true",
                        help="No-op alias — these endpoints are snapshot-only")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    today_str = now.strftime("%Y%m%d")
    fetched_at = now.isoformat()
    mode = "backfill" if args.backfill else "incremental"

    print(f"DefiLlama Pipeline  mode={mode}")

    for label, fetch_fn, out_dir, fname_prefix in [
        ("protocols", fetch_protocols, PROTOCOLS_DIR, "defillama_protocols"),
        ("fees", fetch_fees, FEES_DIR, "defillama_fees"),
        ("stablecoins", fetch_stablecoins, STABLECOINS_DIR, "defillama_stablecoins"),
    ]:
        print(f"[{label}]")
        try:
            df = fetch_fn()
        except Exception as exc:
            print(f"  ERROR - {exc}")
            continue
        if df.empty:
            print("  no data")
            continue
        df["fetched_at"] = fetched_at
        out_name = f"{fname_prefix}_{mode}_{today_str}.parquet"
        path = write_partitioned(df, out_dir, out_name)
        print(f"  {len(df):,} rows -> {path}")

    print("--- DEFILLAMA PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
