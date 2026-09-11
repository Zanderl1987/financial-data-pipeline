from strategies.stage5 import run_holdout_for, holdout_cache, N_PERM, SEED
from evaluation.universe import clean_symbols
import pandas as pd

# Build holdout cache (25% symbols restricted to 2018+)
cache = holdout_cache()
symbols = clean_symbols(
    list(cache.keys()), price_table="yfinance_universe_prices"
)
print(f"Holdout cache: {len(cache)} symbols")

# Run holdout test for rsi_bb_inside_strategy
print("\n=== Running holdout for rsi_bb_inside_strategy ===")
try:
    row = run_holdout_for("rsi_bb_inside_strategy", cache, n_perm=N_PERM, seed=SEED)
    print(f"pnl_p={row['holdout_pnl_p']}, success={row['holdout_success']}")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")

# Run holdout test for bollinger_bands_simple
print("\n=== Running holdout for bollinger_bands_simple ===")
try:
    row = run_holdout_for("bollinger_bands_simple", cache, n_perm=N_PERM, seed=SEED)
    print(f"pnl_p={row['holdout_pnl_p']}, success={row['holdout_success']}")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")