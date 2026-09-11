#!/usr/bin/env python3
"""Stage 5 holdout tests on FDR survivors - single process mode."""
import hashlib, json, os, time, yfinance as yf
from datetime import datetime, timezone
import sys
sys.path.insert(0, r'C:\Users\zande\PycharmProjects\financial-data-pipeline')

from strategies.stage3 import dev_cache, load_rule_for, with_price_floor, cost_config
from evaluation import trades as ev_trades
from evaluation import stats as ev_stats
import query as q
from evaluation.universe import clean_symbols

# Build dev and holdout splits using SHA-256
symbols = q.symbols('yfinance_universe_prices')
clean = clean_symbols(symbols, price_table='yfinance_universe_prices')
dev, holdout = [], []
for s in clean:
    h = hashlib.sha256(s.encode()).hexdigest()
    if int(h, 16) % 4 == 0:
        holdout.append(s)
    else:
        dev.append(s)

print(f'Dev: {len(dev)} symbols, Holdout: {len(holdout)} symbols')

# Build dev cache
cache = dev_cache()
primary_cfg = cost_config(10.0)

# Test the 2 FDR survivors
survivors = ['rsi_bb_inside_strategy', 'bollinger_bands_simple']

for slug in survivors:
    print(f'\n--- {slug} holdout test ---')
    
    meta_path = f'storage/tv_scripts/{slug}.meta.json'
    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path, 'r') as f:
            meta = json.load(f)
    
    rule, _ = load_rule_for(slug)
    rule = with_price_floor(rule)
    
    # Simulate on dev symbols
    t0 = time.time()
    trades_df = ev_trades.simulate(rule, cache, config=primary_cfg)
    dev_summary = ev_trades.trade_summary(trades_df)
    print(f'Dev trades: {dev_summary["n_trades"]}, pnl={dev_summary.get("total_pnl_dollars", 0):.0f}')
    
    # Permutation test on dev symbols (single process, workers=0)
    t0 = time.time()
    perm = ev_stats.permutation_trades(rule, cache, n_perm=100, seed=0, config=primary_cfg, workers=0)
    dev_pnl_p = perm.get('pnl_p')
    print(f'Dev permutation pnl_p: {dev_pnl_p} ({time.time()-t0:.1f}s)')
    
    # Holdout test: simulate on holdout symbols with data capped at DEV_END
    holdout_end = '2017-12-31'
    holdout_cache = {}
    for s in holdout:
        try:
            df = yf.download(s, period='max', progress=False)
            if df is not None and not df.empty:
                holdout_cache[s] = df[df.index <= holdout_end]
        except:
            pass
    holdout_count = len([s for s in holdout_cache if holdout_cache[s] is not None and not holdout_cache[s].empty])
    print(f'Holdout symbols with data (capped at {holdout_end}): {holdout_count}')
    
    # Run holdout test: simulate on holdout symbols
    if holdout_count > 0:
        holdout_trades = ev_trades.simulate(rule, holdout_cache, config=primary_cfg)
        holdout_summary = ev_trades.trade_summary(holdout_trades)
        print(f'Holdout trades: {holdout_summary["n_trades"]}, pnl={holdout_summary.get("total_pnl_dollars", 0):.0f}')
        
        # Run permutation test on holdout (single process)
        t0 = time.time()
        holdout_perm = ev_stats.permutation_trades(rule, holdout_cache, n_perm=100, seed=0, config=primary_cfg, workers=0)
        holdout_pnl_p = holdout_perm.get('pnl_p')
        print(f'Holdout permutation pnl_p: {holdout_pnl_p} ({time.time()-t0:.1f}s)')
    else:
        print('No holdout data available')
        holdout_pnl_p = None
    
    print(f'\n=== {slug} holdout results ===')
    print(f'Dev pnl_p: {dev_pnl_p}')
    print(f'Holdout pnl_p: {holdout_pnl_p}')
    print(f'Holdout success (pnl_p < 0.05): {holdout_pnl_p is not None and holdout_pnl_p < 0.05}')

print('\nDone')