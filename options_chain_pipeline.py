import pandas as pd
import numpy as np
import schwabdev
import datetime
import time
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ["SCHWAB_API_KEY"]
APP_SECRET = os.environ["SCHWAB_APP_SECRET"]
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH = os.environ.get("SCHWAB_TOKEN_PATH", "tokens.json")

OUTPUT_DIR = os.path.join("storage", "raw", "options")

FALLBACK_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"]

MAX_RETRIES = 3
BACKOFF_SECONDS = 60
REQUEST_INTERVAL = 0.5


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


def fetch_with_backoff(client, symbol):
    for attempt in range(1, MAX_RETRIES + 1):
        response = client.option_chains(symbol)
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


def process_chain_map(exp_date_map, contract_type, symbol):
    rows = []
    if not exp_date_map:
        return rows
    for exp_date_str, strikes in exp_date_map.items():
        expiration = exp_date_str.split(":")[0]
        for strike, contracts in strikes.items():
            for contract in contracts:
                rows.append({
                    "symbol": symbol,
                    "contract_type": contract_type,
                    "strike_price": contract.get("strikePrice"),
                    "expiration_date": expiration,
                    "days_to_expiration": contract.get("daysToExpiration"),
                    "last_price": contract.get("last"),
                    "mark": contract.get("mark"),
                    "bid": contract.get("bid"),
                    "ask": contract.get("ask"),
                    "volume": contract.get("totalVolume", 0),
                    "open_interest": contract.get("openInterest", 0),
                    "volatility": contract.get("volatility"),
                    "delta": contract.get("delta"),
                    "gamma": contract.get("gamma"),
                    "theta": contract.get("theta"),
                    "vega": contract.get("vega"),
                    "underlying_price": contract.get("underlyingPrice"),
                })
    return rows


def capture_daily_metrics(symbol, client):
    print(f"Capturing data for {symbol}...")
    data = fetch_with_backoff(client, symbol)
    if not data:
        return None, None

    calls = process_chain_map(data.get("callExpDateMap", {}), "CALL", symbol)
    puts = process_chain_map(data.get("putExpDateMap", {}), "PUT", symbol)

    df = pd.DataFrame(calls + puts)
    if df.empty:
        return None, None

    call_count = len(df[df["contract_type"] == "CALL"])
    put_count = len(df[df["contract_type"] == "PUT"])
    call_oi = df[df["contract_type"] == "CALL"]["open_interest"].sum()
    put_oi = df[df["contract_type"] == "PUT"]["open_interest"].sum()
    call_vol = df[df["contract_type"] == "CALL"]["volume"].sum()
    put_vol = df[df["contract_type"] == "PUT"]["volume"].sum()
    avg_delta_calls = df[df["contract_type"] == "CALL"]["delta"].mean()

    metrics = {
        "date": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
        "symbol": symbol,
        "underlying_price": df["underlying_price"].iloc[0] if not df.empty else None,
        "total_contracts_available": len(df),
        "calls_available": call_count,
        "puts_available": put_count,
        "total_open_interest": call_oi + put_oi,
        "total_volume": call_vol + put_vol,
        "put_call_ratio_oi": round(put_oi / call_oi, 4) if call_oi > 0 else 0,
        "put_call_ratio_vol": round(put_vol / call_vol, 4) if call_vol > 0 else 0,
        "avg_call_delta": round(avg_delta_calls, 4) if pd.notna(avg_delta_calls) else None,
        "fetched_at": datetime.datetime.utcnow().isoformat(),
    }

    return df, metrics


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    client = schwabdev.Client(
        app_key=API_KEY,
        app_secret=APP_SECRET,
        callback_url=CALLBACK_URL,
        tokens_file=TOKEN_PATH,
    )

    symbols = get_dji_symbols()
    all_metrics = []
    failed = []

    all_chains = []
    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}]", end=" ")
        chain_df, metrics = capture_daily_metrics(symbol, client)
        if metrics:
            all_metrics.append(metrics)
        if chain_df is not None and not chain_df.empty:
            chain_df["date"] = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            chain_df["fetched_at"] = datetime.datetime.utcnow().isoformat()
            all_chains.append(chain_df)
        if not metrics:
            failed.append(symbol)
        time.sleep(REQUEST_INTERVAL)

    if not all_metrics:
        print("No metrics collected.")
        return

    today = datetime.datetime.utcnow().strftime("%Y%m%d")

    summary_df = pd.DataFrame(all_metrics)
    filename = os.path.join(OUTPUT_DIR, f"options_metrics_{today}.parquet")
    summary_df.to_parquet(filename, index=False, compression="snappy")

    if all_chains:
        raw_df = pd.concat(all_chains, ignore_index=True)
        raw_path = os.path.join(OUTPUT_DIR, f"options_chain_raw_{today}.parquet")
        raw_df.to_parquet(raw_path, index=False, compression="snappy")
        print(f"Saved raw chains  ({len(raw_df):,} contracts) → {raw_path}")

    print(f"\n--- COMPLETE ---")
    print(f"Saved metrics for {len(all_metrics)} symbols → {filename}")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}")
    print(summary_df[["symbol", "total_contracts_available", "put_call_ratio_oi"]].to_string(index=False))


if __name__ == "__main__":
    main()
