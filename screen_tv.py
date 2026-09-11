# Quick screening: 50 symbols, n_perm=20 for all 23
from strategies.stage3 import load_rule_for, with_price_floor, cost_config, build_dev_cache, dev_holdout_symbols
from evaluation import trades as ev_trades
from evaluation import stats as ev_stats
import query as q
from evaluation.universe import clean_symbols
import json, os, time
import multiprocessing as mp

def main():
    symbols = q.symbols('yfinance_universe_prices')
    clean = clean_symbols(symbols, price_table='yfinance_universe_prices')
    dev, _ = dev_holdout_symbols(clean)
    test_symbols = dev[:50]
    cache = build_dev_cache(test_symbols)
    print('Cache:', len(cache))

    remaining = [
        'dual_bollinger_band_cross',
        'fractal_memory_strategy',
        'fvg_bos_confirmation',
        'high_activity_penny_stock',
        'ihvpg6ts_stop_loss_and_take_profit_in_example',
        'ineficient_market_123_pattern',
        'joey_stochrsi_atr',
        'kama_adaptive_strategy',
        'mrr_mean_reversion_range',
        'optimized_doji_breakout_short',
        'optimized_keltner_channels_nifty',
        'ras16l2w_bvol_early_entry',
        'rsi_bb_inside_strategy',
        'rsi_divergence_ema_filter',
        'smoothed_heiken_ashi_strategy',
        'supertrend_entry_tp123',
        'tomukas_sweep_reclaim_scalein',
        'tradleware_hodl',
        'ucgxklvt_ma_crossover_rsi',
        'ultimate_prop_firm_artillery',
        'vegas_channel_tunnel_v11',
        'ymepyslq_nnfx_btc_ssl_qqe',
        'zy1xmx8s_ssl_channel_qqe_strategy',
    ]

    primary_cfg = cost_config(10.0)
    results = []

    for slug in remaining:
        print()
        print('---', slug, '---')
        rule, _ = load_rule_for(slug)
        rule = with_price_floor(rule)
        
        t0 = time.time()
        trades_df = ev_trades.simulate(rule, cache, config=primary_cfg)
        summary = ev_trades.trade_summary(trades_df)
        pnl = summary.get('total_pnl_dollars', 0)
        n_trades = summary['n_trades']
        print('Sim:', time.time()-t0, 's, trades=', n_trades, 'pnl=', pnl)
        
        t0 = time.time()
        perm = ev_stats.permutation_trades(rule, cache, n_perm=20, seed=0, config=primary_cfg, workers=4)
        pnl_p = perm.get('pnl_p')
        print('Perm:', time.time()-t0, 's, pnl_p=', pnl_p)
        
        results.append({
            'slug': slug, 'n_trades': n_trades, 'pnl': pnl, 'pnl_p': pnl_p
        })

    # Sort by P&L
    results.sort(key=lambda x: x['pnl'] if x['pnl'] is not None else -1e9, reverse=True)
    print('\n\n=== RANKED BY P&L ===')
    for r in results:
        print(f"{r['slug']:45s} trades={r['n_trades']:6d} pnl={r['pnl']:>12.0f} pnl_p={r['pnl_p']}")

if __name__ == '__main__':
    mp.freeze_support()
    main()