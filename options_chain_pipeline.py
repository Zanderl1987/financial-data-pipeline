import pandas as pd
import numpy as np
import schwabdev
import json
import datetime
import time
import os
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
api_key = os.environ["SCHWAB_API_KEY"]
app_secret = os.environ["SCHWAB_APP_SECRET"]
callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
token_path = os.environ.get("SCHWAB_TOKEN_PATH", "tokens.json")

# Target symbols: Fetch DJI components dynamically
try:
    # Attempt to fetch DJI symbols from Wikipedia
    dji_url = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"
    # Using a user-agent to avoid 403 Forbidden
    df_dji = pd.read_html(dji_url, storage_options={'User-Agent': 'Mozilla/5.0'})[2]
    symbol_list = df_dji['Symbol'].tolist()
    print(f"Successfully fetched {len(symbol_list)} DJI symbols.")
except Exception as e:
    print(f"Error fetching DJI list: {e}. Using fallback list.")
    symbol_list = ['AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META']

def process_chain_map(exp_date_map, contract_type, symbol):
    """Flattens the nested expiration date map from Schwab API."""
    rows = []
    if not exp_date_map:
        return rows
        
    for exp_date_str, strikes in exp_date_map.items():
        # exp_date_str is typically "2024-06-21:14"
        expiration = exp_date_str.split(':')[0]
        for strike, contracts in strikes.items():
            for contract in contracts:
                row = {
                    'symbol': symbol,
                    'contract_type': contract_type,
                    'strike_price': contract.get('strikePrice'),
                    'expiration_date': expiration,
                    'days_to_expiration': contract.get('daysToExpiration'),
                    'last_price': contract.get('last'),
                    'mark': contract.get('mark'),
                    'bid': contract.get('bid'),
                    'ask': contract.get('ask'),
                    'volume': contract.get('totalVolume', 0),
                    'open_interest': contract.get('openInterest', 0),
                    'volatility': contract.get('volatility'),
                    'delta': contract.get('delta'),
                    'gamma': contract.get('gamma'),
                    'theta': contract.get('theta'),
                    'vega': contract.get('vega'),
                    'underlying_price': contract.get('underlyingPrice')
                }
                rows.append(row)
    return rows

def capture_daily_metrics(symbol, client):
    """Fetches chain and calculates daily aggregation metrics."""
    print(f"Capturing data for {symbol}...")
    try:
        # Fetch full chain (all strikes, all expirations)
        response = client.option_chains(symbol)
        if response.status_code != 200:
            print(f"Error {response.status_code} for {symbol}: {response.text}")
            return None, None
            
        data = response.json()
        
        # Flatten the maps
        calls = process_chain_map(data.get('callExpDateMap', {}), 'CALL', symbol)
        puts = process_chain_map(data.get('putExpDateMap', {}), 'PUT', symbol)
        
        df = pd.DataFrame(calls + puts)
        if df.empty:
            return None, None
            
        # --- Aggregation Metrics ---
        # 1. Contract Counts (as requested: total, puts, calls)
        call_count = len(df[df['contract_type'] == 'CALL'])
        put_count = len(df[df['contract_type'] == 'PUT'])
        total_contract_count = len(df)
        
        # 2. Volume and OI
        total_vol = df['volume'].sum()
        total_oi = df['open_interest'].sum()
        call_oi = df[df['contract_type'] == 'CALL']['open_interest'].sum()
        put_oi = df[df['contract_type'] == 'PUT']['open_interest'].sum()
        
        # 3. Put-Call Ratios
        pcr_oi = put_oi / call_oi if call_oi > 0 else 0
        pcr_vol = (df[df['contract_type'] == 'PUT']['volume'].sum() / 
                   df[df['contract_type'] == 'CALL']['volume'].sum() 
                   if df[df['contract_type'] == 'CALL']['volume'].sum() > 0 else 0)
        
        # 4. Greek Averages (Optional but useful for analysis)
        avg_delta_calls = df[df['contract_type'] == 'CALL']['delta'].mean()
        
        metrics = {
            'date': datetime.datetime.now().strftime("%Y-%m-%d"),
            'symbol': symbol,
            'underlying_price': df['underlying_price'].iloc[0] if not df.empty else None,
            'total_contracts_available': total_contract_count,
            'calls_available': call_count,
            'puts_available': put_count,
            'total_open_interest': total_oi,
            'total_volume': total_vol,
            'put_call_ratio_oi': round(pcr_oi, 4),
            'put_call_ratio_vol': round(pcr_vol, 4),
            'avg_call_delta': round(avg_delta_calls, 4) if avg_delta_calls else None,
            'timestamp': datetime.datetime.now().isoformat()
        }
        
        return df, metrics
        
    except Exception as e:
        print(f"Failed to process {symbol}: {str(e)}")
        return None, None

def main():
    # Initialize Client
    # Note: Tokens are stored in tokens.json; client handles refresh automatically
    client = schwabdev.Client(app_key=api_key, app_secret=app_secret, callback_url=callback_url, tokens_file=token_path)
    
    all_metrics = []
    
    # Create output directory
    output_dir = "options_history_capture"
    os.makedirs(output_dir, exist_ok=True)
    
    # For initial run, use a subset or the full list
    for symbol in symbol_list:
        full_df, metrics = capture_daily_metrics(symbol, client)
        if metrics:
            all_metrics.append(metrics)
            # Optionally save full raw chain for this symbol in parquet format
            # full_df.to_parquet(f"{output_dir}/{symbol}_{metrics['date']}_raw.parquet")
        
        time.sleep(0.5) # Avoid hitting rate limits
        
    # Save the consolidated daily summary in Parquet format
    if all_metrics:
        summary_df = pd.DataFrame(all_metrics)
        today = datetime.datetime.now().strftime("%Y%m%d")
        filename = f"{output_dir}/options_metrics_{today}.parquet"
        summary_df.to_parquet(filename, index=False)
        print(f"\n--- SUCCESS ---")
        print(f"Saved metrics for {len(all_metrics)} symbols to {filename}")
        print(summary_df[['symbol', 'total_contracts_available', 'put_call_ratio_oi']].head())
    else:
        print("No metrics collected.")

if __name__ == "__main__":
    main()
