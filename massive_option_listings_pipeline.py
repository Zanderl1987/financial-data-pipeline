#!/usr/bin/env python3
"""
Massive Option Listings Pipeline:
  Daily history of which option contracts were LISTED for each symbol, from
  Massive's (formerly Polygon) contracts endpoint with `as_of`. Input to the
  strike-introduction study (docs/superpowers/specs/2026-10-01-strike-intro-study-design.md).

  Stores only changes: each symbol's first day is the full list
  (change='initial'), then contracts added/removed vs the previous trading
  day. Standard contracts only (100 shares, root == symbol, no adjusted
  deliverables). A day that returns nothing while the previous day had
  contracts is recorded as status='missing' and skipped, not diffed.

Outputs:
  storage/raw/massive/option_listing_changes/year=YYYY/month=MM/option_listing_changes_<SYM>_<first>_<last>.parquet
  storage/raw/massive/option_chain_summary/year=YYYY/month=MM/option_chain_summary_<SYM>_<first>_<last>.parquet

Usage:
  python massive_option_listings_pipeline.py                      # advance universe to latest day
  python massive_option_listings_pipeline.py --symbols AAPL --start 2026-08-01 --end 2026-08-31 --out-root C:/tmp/trial
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

import pandas as pd
import requests
from dotenv import load_dotenv

from storage_utils import write_partitioned

CHANGE_COLS = ["symbol", "date", "contract_ticker", "strike", "expiration_date",
               "put_call", "shares_per_contract", "change"]
_FRAME_COLS = ["contract_ticker", "strike", "expiration_date", "put_call",
               "shares_per_contract"]
_ROOT = re.compile(r"^O:([A-Z.]+?)\d{6}[CP]\d{8}$")


def standard_frame(results: list[dict], symbol: str) -> pd.DataFrame:
    """Contracts from one or more result pages, standard contracts only."""
    root = symbol.replace(".", "")
    rows = []
    for c in results:
        m = _ROOT.match(c.get("ticker", ""))
        if not m or m.group(1) != root:
            continue                      # adjusted root (FDX1) or malformed
        if c.get("shares_per_contract") != 100 or c.get("additional_underlyings"):
            continue
        rows.append({
            "contract_ticker":     c["ticker"],
            "strike":              float(c["strike_price"]),
            "expiration_date":     c["expiration_date"],
            "put_call":            c["contract_type"].upper(),
            "shares_per_contract": int(c["shares_per_contract"]),
        })
    df = pd.DataFrame(rows, columns=_FRAME_COLS)
    return df.drop_duplicates("contract_ticker").reset_index(drop=True)


def live_on(df: pd.DataFrame, as_of: str) -> pd.DataFrame:
    return df[df["expiration_date"] >= as_of].reset_index(drop=True)


def diff_contracts(prev, curr: pd.DataFrame, symbol: str, date: str) -> pd.DataFrame:
    if prev is None:
        out = curr.assign(change="initial")
    else:
        added = curr[~curr["contract_ticker"].isin(prev["contract_ticker"])]
        removed = prev[~prev["contract_ticker"].isin(curr["contract_ticker"])]
        out = pd.concat([added.assign(change="added"),
                         removed.assign(change="removed")], ignore_index=True)
    out = out.assign(symbol=symbol, date=date)
    return out[CHANGE_COLS].reset_index(drop=True)


def summarize(curr: pd.DataFrame, symbol: str, date: str, status: str) -> dict:
    empty = curr.empty
    return {
        "symbol": symbol, "date": date, "status": status,
        "n_contracts":   int(len(curr)),
        "n_expirations": int(curr["expiration_date"].nunique()),
        "min_strike":    None if empty else float(curr["strike"].min()),
        "max_strike":    None if empty else float(curr["strike"].max()),
    }


def replay_live_set(changes: pd.DataFrame, date: str) -> set[str]:
    """Contract tickers live on `date`, rebuilt from stored changes."""
    live: set[str] = set()
    upto = changes[changes["date"] <= date]
    for _, day in upto.groupby("date", sort=True):
        if (day["change"] == "initial").any():
            live = set()
        live |= set(day.loc[day["change"].isin(["initial", "added"]), "contract_ticker"])
        live -= set(day.loc[day["change"] == "removed", "contract_ticker"])
    return live


BASE_URL = "https://api.massive.com"          # confirmed by the Task 1 gate
CONTRACTS_PATH = "/v3/reference/options/contracts"
REQUEST_INTERVAL = 12.5                        # free tier: 5 requests/minute
PAGE_LIMIT = 1000
MAX_RETRIES = 5
BACKOFF_SECONDS = 60
# Set from the Task 1 gate result: (None,) if as_of alone returns contracts
# live on that date; ("false", "true") if since-expired ones need expired=true.
EXPIRED_QUERIES = (None,)


class MassiveClient:
    def __init__(self, api_key, session=None, interval=REQUEST_INTERVAL, sleep=time.sleep):
        self.api_key, self.interval, self.sleep = api_key, interval, sleep
        self.session = session or requests.Session()
        self._last = 0.0

    def get(self, url: str, params: dict) -> dict:
        params = {**params, "apiKey": self.api_key}   # next_url omits the key
        for attempt in range(1, MAX_RETRIES + 1):
            wait = self.interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                print(f"  request error (attempt {attempt}): {type(e).__name__}", flush=True)
                self.sleep(BACKOFF_SECONDS)
                continue
            if r.status_code == 200:
                return r.json()
            print(f"  HTTP {r.status_code} (attempt {attempt})", flush=True)
            self.sleep(BACKOFF_SECONDS * attempt if r.status_code == 429 else BACKOFF_SECONDS)
        # url only, never params: they carry the API key
        raise RuntimeError(f"Massive request failed {MAX_RETRIES}x: {url.split('?')[0]}")


def fetch_contracts(client: MassiveClient, symbol: str, as_of: str) -> pd.DataFrame:
    """Standard contracts live on `as_of`, across all pages and EXPIRED_QUERIES."""
    results = []
    for expired in EXPIRED_QUERIES:
        params = {"underlying_ticker": symbol, "as_of": as_of, "limit": PAGE_LIMIT}
        if expired is not None:
            params["expired"] = expired
        url = BASE_URL + CONTRACTS_PATH
        while url:
            body = client.get(url, params)
            results += body.get("results", [])
            url, params = body.get("next_url"), {}
    return live_on(standard_frame(results, symbol), as_of)


FLUSH_EVERY = 20                 # trading days per output chunk / checkpoint
HISTORY_DAYS = 725               # free tier keeps 2 years; stay inside it
UNIVERSE_FILE = os.path.join("experiments", "strike_intro_universe.csv")


def _paths(out_root: str):
    raw = os.path.join(out_root, "raw", "massive")
    return (os.path.join(raw, "option_listing_changes"),
            os.path.join(raw, "option_chain_summary"),
            os.path.join(out_root, "state", "massive_listings"))


def _load_state(state_dir: str, symbol: str):
    meta = os.path.join(state_dir, f"{symbol}.json")
    if not os.path.exists(meta):
        return None, None
    with open(meta, encoding="utf-8") as f:
        last = json.load(f)["last_date"]
    live = pd.read_parquet(os.path.join(state_dir, f"{symbol}.parquet"))
    return last, live


def _save_state(state_dir: str, symbol: str, last: str, live: pd.DataFrame):
    os.makedirs(state_dir, exist_ok=True)
    live.to_parquet(os.path.join(state_dir, f"{symbol}.parquet"), index=False)
    with open(os.path.join(state_dir, f"{symbol}.json"), "w", encoding="utf-8") as f:
        json.dump({"last_date": last}, f)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()


def advance_symbol(client, symbol, days, out_root, flush_every=FLUSH_EVERY,
                   fetch=fetch_contracts) -> int:
    """Process the trading days after this symbol's checkpoint; return how many."""
    changes_dir, summary_dir, state_dir = _paths(out_root)
    last, live = _load_state(state_dir, symbol)
    todo = [d for d in days if last is None or d > last]
    changes, summaries = [], []
    done = 0

    def flush(upto):
        if summaries:
            tag = f"{symbol}_{summaries[0]['date']}_{upto}"
            now = _now()
            if changes:
                write_partitioned(pd.concat(changes, ignore_index=True).assign(fetched_at=now),
                                  changes_dir, f"option_listing_changes_{tag}.parquet")
            write_partitioned(pd.DataFrame(summaries).assign(fetched_at=now),
                              summary_dir, f"option_chain_summary_{tag}.parquet")
        # State AFTER outputs: a crash in between re-processes the chunk, and
        # curated's key absorbs the duplicate rows.
        _save_state(state_dir, symbol, upto, live)
        changes.clear()
        summaries.clear()

    for d in todo:
        curr = fetch(client, symbol, d)
        if curr.empty and live is not None and not live.empty:
            summaries.append(summarize(curr, symbol, d, "missing"))
        else:
            ch = diff_contracts(live, curr, symbol, d)
            if not ch.empty:
                changes.append(ch)
            summaries.append(summarize(curr, symbol, d, "ok"))
            live = curr
        done += 1
        if done % flush_every == 0:
            flush(d)
    if summaries:
        flush(todo[-1])
    return done


def trading_days(start: str, end: str) -> list[str]:
    """SPY trading dates from the price store (the market calendar)."""
    import query as q
    df = q.sql(f"""SELECT DISTINCT CAST(date AS VARCHAR) AS d FROM prices
                   WHERE symbol = 'SPY' AND date BETWEEN '{start}' AND '{end}'
                   ORDER BY d""")
    return [d[:10] for d in df["d"]]


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description="Massive option listings (as_of) history")
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--universe-file", default=UNIVERSE_FILE)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--out-root", default="storage")
    args = p.parse_args()

    key = os.environ.get("MASSIVE_API_KEY")
    if not key:
        sys.exit("MASSIVE_API_KEY not set in .env")
    today = datetime.date.today()
    start = args.start or (today - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    end = args.end or today.isoformat()
    symbols = args.symbols or pd.read_csv(args.universe_file)["symbol"].tolist()
    days = trading_days(start, end)
    client = MassiveClient(key)
    print(f"{len(symbols)} symbols x {len(days)} trading days ({start}..{end})", flush=True)
    for i, sym in enumerate(symbols, 1):
        n = advance_symbol(client, sym, days, args.out_root)
        print(f"[{i}/{len(symbols)}] {sym}: {n} new days", flush=True)


if __name__ == "__main__":
    main()
