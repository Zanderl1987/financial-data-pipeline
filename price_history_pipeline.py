import pandas as pd
import numpy as np
import schwabdev
import datetime
import time
import os
import argparse
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ["SCHWAB_API_KEY"]
APP_SECRET = os.environ["SCHWAB_APP_SECRET"]
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH = os.environ.get("SCHWAB_TOKEN_PATH", "tokens.json")

OUTPUT_DIR = os.path.join("storage", "raw", "prices")

FALLBACK_SYMBOLS = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
    "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
]

MAX_RETRIES = 3
BACKOFF_SECONDS = 60
REQUEST_INTERVAL = 0.5  # 120 req/min hard cap


def get_dji_symbols():
    try:
        df = pd.read_html(
            "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
            storage_options={"User-Agent": "Mozilla/5.0"},
        )[2]
        symbols = df["Symbol"].tolist()
        print(f"Fetched {len(symbols)} DJI symbols from Wikipedia.")
        return symbols
    except Exception as e:
        print(f"Wikipedia fetch failed ({e}). Using fallback symbol list.")
        return FALLBACK_SYMBOLS


def fetch_with_backoff(client, symbol, start_ms, end_ms):
    """Fetch price history with retry + exponential backoff on 429."""
    for attempt in range(1, MAX_RETRIES + 1):
        response = client.price_history(
            symbol=symbol,
            periodType="year",
            period=1,
            frequencyType="daily",
            frequency=1,
            startDate=start_ms,
            endDate=end_ms,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code == 429:
            wait = BACKOFF_SECONDS * attempt
            print(f"  429 rate limit hit for {symbol}. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
            time.sleep(wait)
        else:
            print(f"  HTTP {response.status_code} for {symbol}: {response.text[:120]}")
            return None
    print(f"  Giving up on {symbol} after {MAX_RETRIES} attempts.")
    return None


def compute_derived_columns(df):
    df = df.sort_values("date").reset_index(drop=True)
    df["pct_change"] = df["close"].pct_change().round(6)
    df["log_return"] = np.log(df["close"] / df["close"].shift(1)).round(6)
    df["intraday_change"] = (df["close"] - df["open"]).round(4)
    df["intraday_range"] = (df["high"] - df["low"]).round(4)
    df["vwap"] = ((df["high"] + df["low"] + df["close"]) / 3).round(4)
    return df


def fetch_symbol(client, symbol, start_ms, end_ms):
    data = fetch_with_backoff(client, symbol, start_ms, end_ms)
    if not data or not data.get("candles"):
        return None

    df = pd.DataFrame(data["candles"])
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
    df["date"] = df["datetime"].dt.date.astype(str)
    df["symbol"] = symbol
    df = df.rename(columns={"volume": "volume"})[
        ["symbol", "date", "open", "high", "low", "close", "volume"]
    ]
    df = compute_derived_columns(df)
    df["fetched_at"] = datetime.datetime.utcnow().isoformat()
    return df


def main(backfill=False):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    client = schwabdev.Client(
        app_key=API_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        tokens_file=TOKEN_PATH,
    )

    end_dt = datetime.datetime.utcnow()
    if backfill:
        # Full year lookback for initial load
        start_dt = end_dt - datetime.timedelta(days=365)
        print("Mode: BACKFILL (365 days)")
    else:
        # Incremental: last 5 trading days to safely catch the latest close
        start_dt = end_dt - datetime.timedelta(days=7)
        print("Mode: INCREMENTAL (last 7 days)")

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    symbols = get_dji_symbols()
    results = []
    failed = []

    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] Fetching {symbol}...")
        df = fetch_symbol(client, symbol, start_ms, end_ms)
        if df is not None:
            results.append(df)
        else:
            failed.append(symbol)
        time.sleep(REQUEST_INTERVAL)

    if not results:
        print("No data collected. Exiting.")
        return

    combined = pd.concat(results, ignore_index=True)

    today = datetime.datetime.utcnow().strftime("%Y%m%d")
    mode_tag = "backfill" if backfill else "incremental"
    filename = os.path.join(OUTPUT_DIR, f"prices_{mode_tag}_{today}.parquet")
    combined.to_parquet(filename, index=False, compression="snappy")

    print(f"\n--- COMPLETE ---")
    print(f"Saved {len(combined)} rows for {len(results)} symbols → {filename}")
    if failed:
        print(f"Failed symbols ({len(failed)}): {', '.join(failed)}")
    print(combined[["symbol", "date", "close", "pct_change", "log_return"]].tail(10).to_string(index=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Schwab price history pipeline")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch full 365-day history (use on first run). Default is incremental (last 7 days).",
    )
    args = parser.parse_args()
    main(backfill=args.backfill)
