#!/usr/bin/env python3
"""Test permutation on pre-built dev cache."""
import sys, time, hashlib
sys.path.insert(0, r'C:\Users\zande\PycharmProjects\financial-data-pipeline')

from strategies.stage3 import dev_cache
from evaluation import trades as ev_trades
from evaluation import stats as ev_stats
from strategies.stage3 import cost_config

# Build dev cache (takes ~55s)
print('Building dev cache...')
cache = dev_cache()
primary_cfg = cost_config(10.0)

# Load a strategy rule
from strategies.stage3 import load_rule_for, with_price_floor

slug = 'dual_bollinger_band_cross'
print(f'Loading {slug}...')
rule, _ = load_rule_for(slug)
rule = with_price_floor(rule)

# Simulate
print('Simulating...')
t0 = time.time()
trades_df = ev_trades.simulate(rule, cache, config=primary_cfg)
summary = ev_trades.trade_summary(trades_df)
print(f'Dev trades: {summary["n_trades"]}, pnl={summary.get("total_pnl_dollars", 0):.0f} ({time.time()-t0:.1f}s)')

# Permutation test (workers=0 to avoid multiprocessing)
print('Running permutation test...')
t0 = time.time()
perm = ev_stats.permutation_trades(rule, cache, n_perm=100, seed=0, config=primary_cfg, workers=0)
pnl_p = perm.get('pnl_p')
cost_fragile = perm.get('cost_fragile', False)
print(f'pnl_p: {pnl_p}, cost_fragile: {cost_fragile} ({time.time()-t0:.1f}s)')

print('Done')