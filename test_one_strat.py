#!/usr/bin/env python3
"""Test one unit_tested strategy with workers=0."""
import sys, time, hashlib
sys.path.insert(0, r'C:\Users\zande\PycharmProjects\financial-data-pipeline')

from strategies.stage3 import dev_cache, load_rule_for, with_price_floor, cost_config
from evaluation import trades as ev_trades
from evaluation import stats as ev_stats
import query as q
from evaluation.universe import clean_symbols

symbols = q.symbols('yfinance_universe_prices')
clean = clean_symbols(symbols, price_table='yfinance_universe_prices')
dev, holdout = [], []
for s in clean:
    h = hashlib.sha256(s.encode()).hexdigest()
    if int(h, 16) % 4 == 0:
        holdout.append(s)
    else:
        dev.append(s)

cache = dev_cache()
primary_cfg = cost_config(10.0)

# Test dual_bollinger_band_cross (stage2, needs full universe test)
slug = 'dual_bollinger_band_cross'
print(f'Testing {slug}')

rule, _ = load_rule_for(slug)
rule = with_price_floor(rule)

t0 = time.time()
trades_df = ev_trades.simulate(rule, cache, config=primary_cfg)
summary = ev_trades.trade_summary(trades_df)
print(f'Dev trades: {summary["n_trades"]}, pnl={summary.get("total_pnl_dollars", 0):.0f} ({time.time()-t0:.1f}s)')

perm = ev_stats.permutation_trades(rule, cache, n_perm=100, seed=0, config=primary_cfg, workers=0)
pnl_p = perm.get('pnl_p')
cost_fragile = perm.get('cost_fragile', False)
print(f'pnl_p: {pnl_p}, cost_fragile: {cost_fragile}')