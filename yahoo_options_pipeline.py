"""
yahoo_options_pipeline.py — fetch full Yahoo Finance options chain and per-contract historical OHLCV.

Two-phase design:
  Phase 1 — Pull the complete chain for each symbol (all expirations, all strikes, calls + puts)
             and save a dated contract-list CSV to storage/tmp/.
  Phase 2 — Fetch daily OHLCV history for every contract in the list and save to
             storage/raw/options_history/.

The two phases are intentionally split so you can:
  - Inspect or filter the contract list before committing to potentially thousands of API calls.
  - Resume a failed history run with --resume without re-fetching the chain.
  - Run Phase 1 only with --skip-history to capture a clean chain snapshot.

Outputs
  storage/tmp/options_contracts_{SYMBOL}_{YYYYMMDD}.csv
      Contract identifiers + current chain snapshot (strike, bid/ask, IV, OI, volume, etc.)

  storage/raw/options_history/options_history_{SYMBOL}_{YYYYMMDD}.parquet
      Daily OHLCV bars for every contract that had any trade history.
      Schema: contract_symbol, symbol, contract_type, strike_price, expiration_date,
              date, open, high, low, close, volume, fetched_at

Usage
  python yahoo_options_pipeline.py --symbols PLTR
  python yahoo_options_pipeline.py --symbols PLTR,AAPL,MSFT --range 2y
  python yahoo_options_pipeline.py --symbols PLTR --skip-history
  python yahoo_options_pipeline.py --resume storage/tmp/options_contracts_PLTR_20260616.csv
  python yahoo_options_pipeline.py --resume storage/tmp/options_contracts_PLTR_20260616.csv --range max
"""

import argparse
import csv
import datetime
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

import pandas as pd
from storage_utils import write_partitioned

# ── config ────────────────────────────────────────────────────────────────────
CHAIN_URL  = "https://query2.finance.yahoo.com/v7/finance/options/{symbol}"
CRUMB_URL  = "https://query2.finance.yahoo.com/v1/test/getcrumb"
PORTAL_URL = "https://finance.yahoo.com"
CHART_URL  = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_JSON_HEADERS  = {"User-Agent": _UA, "Accept": "application/json"}
_BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

TMP_DIR     = os.path.join("storage", "tmp")
HISTORY_DIR = os.path.join("storage", "raw", "options_history")

REQUEST_INTERVAL = 0.25   # seconds between requests
MAX_RETRIES      = 3
BACKOFF_SECONDS  = 30

HISTORY_COLS = [
    "contract_symbol", "symbol", "contract_type", "strike_price",
    "expiration_date", "date", "open", "high", "low", "close", "volume", "fetched_at",
]


# ── session (needed for chain endpoint; chart endpoint works without) ──────────
def init_session() -> Tuple[urllib.request.OpenerDirector, str]:
    """Establish a Yahoo Finance cookie session and return (opener, crumb)."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # Page visit to acquire session cookies
    init_req = urllib.request.Request(PORTAL_URL, headers={
        "User-Agent": _UA,
        "Accept": _BROWSER_ACCEPT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    })
    opener.open(init_req, timeout=15)

    # Exchange cookies for a crumb token
    crumb_req = urllib.request.Request(CRUMB_URL, headers={"User-Agent": _UA, "Accept": "*/*"})
    crumb = opener.open(crumb_req, timeout=15).read().decode()
    print(f"  Session ready. Crumb: {crumb!r}")
    return opener, crumb


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _get_chain(opener: urllib.request.OpenerDirector, crumb: str,
               url: str, label: str = "") -> Optional[dict]:
    """GET a chain endpoint using the cookie session + crumb."""
    sep = "&" if "?" in url else "?"
    full_url = url + sep + "crumb=" + urllib.parse.quote(crumb)
    tag = f" ({label})" if label else ""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(full_url, headers=_JSON_HEADERS)
            with opener.open(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"    429{tag}. Sleeping {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            elif e.code == 404:
                return None
            else:
                print(f"    HTTP {e.code}{tag}: {e.reason}")
                return None
        except Exception as exc:
            print(f"    Error{tag}: {exc}")
            return None
    return None


def _get(url: str, label: str = "") -> Optional[dict]:
    """GET a public endpoint (chart API — no session needed)."""
    tag = f" ({label})" if label else ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=_JSON_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"    429{tag}. Sleeping {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            elif e.code == 404:
                return None
            else:
                print(f"    HTTP {e.code}{tag}: {e.reason}")
                return None
        except Exception as exc:
            print(f"    Error{tag}: {exc}")
            return None
    return None


# ── Phase 1: chain ────────────────────────────────────────────────────────────
def _ts_to_date(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def _parse_option_block(block: dict, underlying: str, fetched_at: str) -> list[dict]:
    rows = []
    exp_date = _ts_to_date(block.get("expirationDate"))
    for side, ctype in (("calls", "CALL"), ("puts", "PUT")):
        for c in block.get(side, []):
            rows.append({
                "contract_symbol":    c.get("contractSymbol"),
                "symbol":             underlying,
                "contract_type":      ctype,
                "strike_price":       c.get("strike"),
                "expiration_date":    exp_date,
                "currency":           c.get("currency"),
                "last_price":         c.get("lastPrice"),
                "bid":                c.get("bid"),
                "ask":                c.get("ask"),
                "volume":             c.get("volume"),
                "open_interest":      c.get("openInterest"),
                "implied_volatility": c.get("impliedVolatility"),
                "in_the_money":       c.get("inTheMoney"),
                "last_trade_date":    _ts_to_date(c.get("lastTradeDate")),
                "contract_size":      c.get("contractSize"),
                "fetched_at":         fetched_at,
            })
    return rows


def fetch_chain(symbol: str, opener: urllib.request.OpenerDirector, crumb: str) -> list[dict]:
    fetched_at = datetime.datetime.utcnow().isoformat()
    print(f"  Fetching chain for {symbol}...")

    data = _get_chain(opener, crumb, CHAIN_URL.format(symbol=symbol), symbol)
    if not data:
        return []

    result = ((data.get("optionChain") or {}).get("result") or [None])[0]
    if not result:
        print(f"  No chain result for {symbol}.")
        return []

    expiration_dates = result.get("expirationDates", [])
    if not expiration_dates:
        print(f"  No expirations found for {symbol}.")
        return []

    print(f"  {len(expiration_dates)} expirations found.")
    all_contracts = []

    # First expiration already embedded in the initial response
    for block in result.get("options", []):
        all_contracts.extend(_parse_option_block(block, symbol, fetched_at))
    time.sleep(REQUEST_INTERVAL)

    # Fetch remaining expirations
    for i, ts in enumerate(expiration_dates[1:], 2):
        url = CHAIN_URL.format(symbol=symbol) + f"?date={ts}"
        data2 = _get_chain(opener, crumb, url, f"{symbol} exp {i}/{len(expiration_dates)}")
        if data2:
            result2 = ((data2.get("optionChain") or {}).get("result") or [None])[0]
            if result2:
                for block in result2.get("options", []):
                    all_contracts.extend(_parse_option_block(block, symbol, fetched_at))
        time.sleep(REQUEST_INTERVAL)

    print(f"  Chain complete: {len(all_contracts):,} contracts ({len(expiration_dates)} expirations).")
    return all_contracts


def save_contracts_csv(contracts: list[dict], symbol: str, today: str) -> str:
    os.makedirs(TMP_DIR, exist_ok=True)
    path = os.path.join(TMP_DIR, f"options_contracts_{symbol}_{today}.csv")
    if not contracts:
        return path
    fieldnames = list(contracts[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(contracts)
    print(f"  Saved {len(contracts):,} contract identifiers -> {path}")
    return path


# ── Phase 2: history ──────────────────────────────────────────────────────────
def fetch_contract_history(contract_symbol: str, range_str: str) -> pd.DataFrame:
    url = CHART_URL.format(symbol=contract_symbol) + f"?interval=1d&range={range_str}"
    data = _get(url)
    if not data:
        return pd.DataFrame()

    results = (data.get("chart") or {}).get("result") or []
    if not results:
        return pd.DataFrame()

    result = results[0]
    timestamps = result.get("timestamp") or []
    if not timestamps:
        return pd.DataFrame()

    q = result["indicators"]["quote"][0]
    opens   = q.get("open",   [None] * len(timestamps))
    highs   = q.get("high",   [None] * len(timestamps))
    lows    = q.get("low",    [None] * len(timestamps))
    closes  = q.get("close",  [None] * len(timestamps))
    volumes = q.get("volume", [None] * len(timestamps))

    rows = []
    for i, ts in enumerate(timestamps):
        if closes[i] is None:
            continue
        rows.append({
            "date":   _ts_to_date(ts),
            "open":   opens[i],
            "high":   highs[i],
            "low":    lows[i],
            "close":  closes[i],
            "volume": volumes[i],
        })
    return pd.DataFrame(rows)


def fetch_all_histories(contracts: list[dict], symbol: str, range_str: str, today: str,
                        min_oi: int = 0) -> None:
    os.makedirs(HISTORY_DIR, exist_ok=True)

    if min_oi > 0:
        before = len(contracts)
        contracts = [c for c in contracts if (c.get("open_interest") or 0) >= min_oi]
        print(f"  --min-oi {min_oi}: {before:,} -> {len(contracts):,} contracts after filter.")

    total = len(contracts)
    all_frames = []
    hits = 0
    empty = 0
    fetched_at = datetime.datetime.utcnow().isoformat()

    print(f"  Fetching history for {total:,} contracts (range={range_str})...")

    for i, c in enumerate(contracts, 1):
        csym = c.get("contract_symbol")
        if not csym:
            continue

        if i == 1 or i % 100 == 0:
            print(f"    [{i}/{total}] {hits} with data so far...")

        df = fetch_contract_history(csym, range_str)
        if not df.empty:
            df["contract_symbol"] = csym
            df["symbol"]          = c["symbol"]
            df["contract_type"]   = c["contract_type"]
            df["strike_price"]    = c["strike_price"]
            df["expiration_date"] = c["expiration_date"]
            df["fetched_at"]      = fetched_at
            all_frames.append(df[HISTORY_COLS])
            hits += 1
        else:
            empty += 1

        time.sleep(REQUEST_INTERVAL)

    print(f"  {hits:,} contracts had history, {empty:,} had none.")

    if not all_frames:
        print("  No history data to save.")
        return

    out = pd.concat(all_frames, ignore_index=True)
    path = write_partitioned(out, HISTORY_DIR, f"options_history_{symbol}_{today}.parquet")
    print(f"  Saved {len(out):,} daily bars -> {path}")


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Fetch Yahoo Finance options chain and per-contract daily OHLCV history."
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--symbols", type=str,
        help="Comma-separated underlying tickers, e.g. PLTR,AAPL,MSFT",
    )
    src.add_argument(
        "--resume", type=str, metavar="CSV_PATH",
        help="Path to an existing options_contracts_*.csv — skips Phase 1, goes straight to history.",
    )
    p.add_argument(
        "--range", dest="range_str", default="1y",
        help=(
            "Yahoo chart range for historical OHLCV. "
            "Options: 1d 5d 1mo 3mo 6mo 1y 2y 5y 10y ytd max. Default: 1y"
        ),
    )
    p.add_argument(
        "--skip-history", action="store_true",
        help="Phase 1 only: fetch chain and save contract list; skip historical OHLCV.",
    )
    p.add_argument(
        "--min-oi", dest="min_oi", type=int, default=0,
        help=(
            "Skip history fetch for contracts with open interest below this threshold. "
            "Useful for large chains (e.g. --min-oi 100 cuts ~30%% of NVDA contracts). Default: 0 (fetch all)."
        ),
    )
    args = p.parse_args()

    today = datetime.datetime.utcnow().strftime("%Y%m%d")

    if args.resume:
        print(f"Resuming from: {args.resume}")
        df = pd.read_csv(args.resume)
        # Contract CSVs written before 2026-07 used "underlying" for the ticker column
        df = df.rename(columns={"underlying": "symbol"})
        for symbol, grp in df.groupby("symbol"):
            print(f"\n{'='*60}\n[{symbol}] {len(grp):,} contracts")
            fetch_all_histories(grp.to_dict("records"), symbol, args.range_str, today,
                                min_oi=args.min_oi)
        return

    # Phase 1 needs a session; Phase 2 (history) does not
    print("Initializing Yahoo Finance session...")
    opener, crumb = init_session()

    if args.range_str != "max":
        print(
            f"\nNOTE: running with --range {args.range_str}. "
            "To capture the full available history for each contract, rerun with --range max."
        )

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    for symbol in symbols:
        print(f"\n{'='*60}\n[{symbol}]")

        contracts = fetch_chain(symbol, opener, crumb)
        if not contracts:
            print(f"  No chain data for {symbol}. Skipping.")
            continue
        save_contracts_csv(contracts, symbol, today)

        if not args.skip_history:
            fetch_all_histories(contracts, symbol, args.range_str, today, min_oi=args.min_oi)

    print("\nDone.")


if __name__ == "__main__":
    main()
