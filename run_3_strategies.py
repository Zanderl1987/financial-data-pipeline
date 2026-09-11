#!/usr/bin/env python3
"""Run 3 remaining unit_tested strategies."""
import multiprocessing as mp
mp.freeze_support()

from strategies.stage3 import dev_cache, load_rule_for, with_price_floor, cost_config, run_strategy, registry_rows_for
from evaluation import trades as ev_trades
from evaluation import stats as ev_stats
from evaluation import registry as ev_registry
import json, os, time
from datetime import datetime, timezone

print('Building dev cache...')
cache = dev_cache()
print('Cache:', len(cache))

primary_cfg = cost_config(10.0)
run_id = ev_registry.new_run_id()
uhash = ev_registry.universe_hash(cache.keys())
date_range = 'dev_split_thru_2017-12-31'
created_at = datetime.now(timezone.utc).isoformat()

test_slugs = ['dual_bollinger_band_cross', 'fractal_memory_strategy', 'fvg_bos_confirmation']

for slug in test_slugs:
    print(f'\n--- {slug} ---')
    
    meta_path = f'storage/tv_scripts/{slug}.meta.json'
    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path, 'r') as f:
            meta = json.load(f)
    
    rule, _ = load_rule_for(slug)
    rule = with_price_floor(rule)
    
    t0 = time.time()
    trades_df = ev_trades.simulate(rule, cache, config=primary_cfg)
    summary = ev_trades.trade_summary(trades_df)
    n_trades = summary['n_trades']
    pnl = summary.get('total_pnl_dollars', 0)
    print(f'Simulate: {time.time()-t0:.0f}s, trades={n_trades}, pnl={pnl}')
    
    t0 = time.time()
    perm = ev_stats.permutation_trades(rule, cache, n_perm=100, seed=0, config=primary_cfg, workers=4)
    pnl_p = perm.get('pnl_p')
    print(f'Permutation: {time.time()-t0:.0f}s, pnl_p={pnl_p}')
    
    row = {'strategy_id': slug, 'n_trades': n_trades,
           'win_rate': summary.get('win_rate_pct'), 'total_pnl_net': pnl,
           'pnl_p': pnl_p, 'pnl_p_5bps': None, 'pnl_p_20bps': None,
           'median_hold': summary.get('median_days_held'),
           'profit_factor': None, 'sharpe': None, 'max_dd': None}
    
    reg_rows = registry_rows_for(row, run_id, uhash, date_range, created_at)
    if not reg_rows.empty:
        ev_registry.append(reg_rows)
        print('Written to registry')

print('\nDone')