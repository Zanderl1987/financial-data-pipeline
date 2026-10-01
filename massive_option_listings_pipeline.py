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

import re
import time

import pandas as pd
import requests

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
