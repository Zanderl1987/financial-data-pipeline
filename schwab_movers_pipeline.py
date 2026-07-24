#!/usr/bin/env python3
"""
Schwab Movers Pipeline:
  Daily snapshot of the top-10 movers per index from the Schwab /movers
  endpoint, captured for gainers, losers, and volume leaders.

  Movers are snapshot-only (Schwab keeps no history), so run this daily —
  ideally near the close — to accumulate a history usable for
  momentum/reversal event studies ("what happens the week after a stock
  tops the movers list?") via event_backtest.technical_events-style queries.

CLI:
  python schwab_movers_pipeline.py
  python schwab_movers_pipeline.py --indices $SPX $COMPX

Output:
  storage/raw/schwab/movers/year=YYYY/month=MM/schwab_movers_{YYYYMMDD}.parquet

Schema:
  date | index_symbol | sort | rank | symbol | description | last_price |
  net_change | net_pct_change | volume | total_volume | trades |
  market_share | fetched_at
"""

import os
import time
import datetime
import argparse
import pandas as pd
import schwabdev
from dotenv import load_dotenv

from storage_utils import write_partitioned

load_dotenv()

API_KEY      = os.environ["SCHWAB_API_KEY"]
APP_SECRET   = os.environ["SCHWAB_APP_SECRET"]
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH   = os.environ.get("SCHWAB_TOKEN_PATH", "tokens.db")

OUTPUT_DIR = os.path.join("storage", "raw", "schwab", "movers")

DEFAULT_INDICES = ["$SPX", "$COMPX", "$DJI"]
SORTS = ["PERCENT_CHANGE_UP", "PERCENT_CHANGE_DOWN", "VOLUME"]

REQUEST_INTERVAL = 0.5


def _screener_rows(payload: dict, index_symbol: str, sort: str,
                   today: str, fetched_at: str) -> list[dict]:
    """Flatten one movers response; field names vary slightly across docs,
    so read everything defensively."""
    rows = []
    for rank, item in enumerate(payload.get("screeners", []), 1):
        rows.append({
            "date":           today,
            "index_symbol":   index_symbol,
            "sort":           sort,
            "rank":           rank,
            "symbol":         item.get("symbol"),
            "description":    item.get("description"),
            "last_price":     item.get("lastPrice"),
            "net_change":     item.get("netChange"),
            "net_pct_change": item.get("netPercentChange"),
            "volume":         item.get("volume"),
            "total_volume":   item.get("totalVolume"),
            "trades":         item.get("trades"),
            "market_share":   item.get("marketShare"),
            "fetched_at":     fetched_at,
        })
    return rows


def main(indices=None):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    client = schwabdev.Client(
        app_key=API_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        tokens_db=TOKEN_PATH,
    )

    indices = indices or DEFAULT_INDICES
    today = datetime.date.today().isoformat()
    fetched_at = datetime.datetime.utcnow().isoformat()
    print(f"Schwab Movers Pipeline  date={today}  indices={indices}")

    rows = []
    for index_symbol in indices:
        for sort in SORTS:
            response = client.movers(index_symbol, sort=sort)
            if response.status_code != 200:
                print(f"  HTTP {response.status_code} for {index_symbol}/{sort}: "
                      f"{response.text[:120]}")
                time.sleep(REQUEST_INTERVAL)
                continue
            got = _screener_rows(response.json(), index_symbol, sort, today, fetched_at)
            print(f"  {index_symbol} {sort}: {len(got)} movers")
            rows.extend(got)
            time.sleep(REQUEST_INTERVAL)

    if not rows:
        print("No movers returned. Exiting.")
        return

    df = pd.DataFrame(rows)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d")
    filename = write_partitioned(df, OUTPUT_DIR, f"schwab_movers_{stamp}.parquet")

    print(f"\n--- SCHWAB MOVERS PIPELINE COMPLETE ---")
    print(f"Saved {len(df)} rows -> {filename}")
    top = df[df["sort"] == "PERCENT_CHANGE_UP"].nsmallest(5, "rank")
    if not top.empty:
        print(top[["index_symbol", "rank", "symbol", "net_pct_change"]]
              .to_string(index=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Schwab top-movers snapshot pipeline")
    parser.add_argument("--indices", nargs="+", default=None,
                        help="Index symbols (default: $SPX $COMPX $DJI)")
    args = parser.parse_args()
    main(indices=args.indices)
