from strategies.stage5 import run_holdout_for, holdout_cache, N_PERM, SEED
from evaluation.universe import clean_symbols

# Build holdout cache
cache = holdout_cache()
print(f"Holdout cache: {len(cache)} symbols")

# Run holdout test for rsi_bb_inside_strategy with workers=0
print("\n=== Running holdout for rsi_bb_inside_strategy ===")
try:
    row = run_holdout_for("rsi_bb_inside_strategy", cache, n_perm=N_PERM, seed=SEED)
    print(f"pnl_p={row['holdout_pnl_p']}, success={row['holdout_success']}")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")

# Run holdout test for bollinger_bands_simple with workers=0
print("\n=== Running holdout for bollinger_bands_simple ===")
try:
    row = run_holdout_for("bollinger_bands_simple", cache, n_perm=N_PERM, seed=SEED)
    print(f"pnl_p={row['holdout_pnl_p']}, success={row['holdout_success']}")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")