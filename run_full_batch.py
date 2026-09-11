# Full campaign run: top 6 promising strategies, full universe, n_perm=100
from strategies.stage3 import load_rule_for, with_price_floor, cost_config, dev_cache, registry_rows_for
from evaluation import trades as ev_trades
from evaluation import stats as ev_stats
from evaluation import registry as ev_registry
import json, os, time
from datetime import datetime, timezone
import multiprocessing as mp

def main():
    print('Building full dev cache...')
    t0 = time.time()
    cache = dev_cache()
    print('Cache built:', len(cache), 'in', time.time()-t0, 's')

    # Top 6 from screening
    slugs = [
        'rsi_bb_inside_strategy',
        'kama_adaptive_strategy',
        'vegas_channel_tunnel_v11',
        'optimized_keltner_channels_nifty',
        'joey_stochrsi_atr',
        'rsi_divergence_ema_filter',
    ]

    primary_cfg = cost_config(10.0)
    
    uhash = ev_registry.universe_hash(cache.keys())
    date_range = f"dev_split_thru_2017-12-31 ({len(cache)} symbols)"
    created_at = datetime.now(timezone.utc).isoformat()
    run_id = ev_registry.new_run_id()

    print(f'Run ID: {run_id}')
    print(f'Universe: {len(cache)} symbols')
    print(f'n_perm=100, workers=4')
    print('=' * 60)

    for i, slug in enumerate(slugs):
        print(f'\n[{i+1}/{len(slugs)}] {slug}')
        
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
        print(f'  Simulate: {time.time()-t0:.0f}s, trades={summary["n_trades"]}, pnl={summary.get("total_pnl_dollars", 0):.0f}')

        t0 = time.time()
        perm = ev_stats.permutation_trades(rule, cache, n_perm=100, seed=0, config=primary_cfg, workers=4)
        pnl_p = perm.get('pnl_p')
        print(f'  Permute: {time.time()-t0:.0f}s, pnl_p={pnl_p}')

        row = {
            'strategy_id': slug,
            'n_trades': summary.get('n_trades', 0),
            'win_rate': summary.get('win_rate_pct'),
            'profit_factor': None,
            'sharpe': None,
            'max_dd': None,
            'median_hold': summary.get('median_days_held'),
            'total_pnl_net': summary.get('total_pnl_dollars'),
            'pnl_p': pnl_p,
            'pnl_p_5bps': None,
            'pnl_p_20bps': None,
            'median_hold': summary.get('median_days_held'),
            'profit_factor': None, 'sharpe': None, 'max_dd': None,
        }

        reg_rows = registry_rows_for(row, run_id, uhash, date_range, created_at)
        if not reg_rows.empty:
            ev_registry.append(reg_rows)
            print(f'  Written to registry')

    print('\n' + '=' * 60)
    print('Full campaign batch complete!')

if __name__ == '__main__':
    mp.freeze_support()
    main()