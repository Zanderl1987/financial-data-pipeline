#!/usr/bin/env python3
"""Test batch of 4 strategies with n_perm=100."""
import multiprocessing as mp
mp.freeze_support()

from strategies.stage3 import dev_cache, load_rule_for, with_price_floor, cost_config, run_strategy
import json, os, time

print('Building dev cache...')
cache = dev_cache()
print('Cache:', len(cache))

test_slugs = [
    'dual_bollinger_band_cross',
    'fractal_memory_strategy',
    'fvg_bos_confirmation',
    'ihvpg6ts_stop_loss_and_take_profit_in_example',
]

primary_cfg = cost_config(10.0)

for slug in test_slugs:
    print(f'\n--- {slug} ---')
    
    meta_path = f'storage/tv_scripts/{slug}.meta.json'
    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path, 'r') as f:
            meta = json.load(f)
    
    t0 = time.time()
    row = run_strategy(slug, meta, 'test', cache, n_perm=100, seed=0, workers=4)
    elapsed = time.time() - t0
    pnl_p = row.get('pnl_p')
    n_trades = row.get('n_trades')
    print(f'Done in {elapsed:.0f}s: pnl_p={pnl_p}, n_trades={n_trades}')