from evaluation import registry as ev_registry
from evaluation import stats as ev_stats
import pandas as pd
import json
import os

# Load registry
reg = ev_registry.load()

# Filter for stage3 pnl_p entries
stage3 = reg[reg['evaluation'] == 'tv_strategy_catalog_stage3']
pnl_p_entries = stage3[stage3['statistic'] == 'pnl_p']

# Build records for BH-FDR
records = []
for _, row in pnl_p_entries.iterrows():
    sid = row['input_name'].replace('pine_', '')
    records.append({'strategy_id': sid, 'p': float(row['value'])})

# Compute BH FDR
fdr_result = ev_stats.bh_fdr(records, alpha=0.10, p_key='p')
print(f"Computed FDR for {len(fdr_result)} strategies")
print(fdr_result[["strategy_id", "p", "p_adj", "reject"]].to_string())

# Get the maps
p_adj_map = dict(zip(fdr_result['strategy_id'], fdr_result['p_adj']))
reject_map = dict(zip(fdr_result['strategy_id'], fdr_result['reject']))

# Append bh_q and fdr_pass rows to the registry
created_at = pd.Timestamp.now(tz='UTC').isoformat()
run_id = ev_registry.new_run_id()

new_rows = []
for _, row in pnl_p_entries.iterrows():
    sid = row['input_name'].replace('pine_', '')
    p_adj = p_adj_map.get(sid)
    reject_val = reject_map.get(sid)
    if p_adj is not None and reject_val is not None:
        new_rows.append({
            "run_id": run_id,
            "input_name": row['input_name'],
            "input_type": "trade_rule",
            "evaluation": "tv_strategy_catalog_stage4",
            "horizon": -1,
            "statistic": "bh_q",
            "value": float(p_adj),
            "n": row.get('n', 0),
            "universe_hash": None,
            "date_range": None,
            "created_at": created_at,
            "execution_hash": None,
        })
        new_rows.append({
            "run_id": run_id,
            "input_name": row['input_name'],
            "input_type": "trade_rule",
            "evaluation": "tv_strategy_catalog_stage4",
            "horizon": -1,
            "statistic": "fdr_pass",
            "value": bool(reject_val),
            "n": row.get('n', 0),
            "universe_hash": None,
            "date_range": None,
            "created_at": created_at,
            "execution_hash": None,
        })

if new_rows:
    reg_df = pd.DataFrame(new_rows, columns=ev_registry.COLUMNS)
    # Check which columns exist
    existing_cols = [c for c in reg_df.columns if c in ev_registry.COLUMNS]
    reg_df = reg_df[existing_cols]
    ev_registry.append(reg_df)
    print(f"\nAppended {len(new_rows)} rows to registry")
else:
    print("No rows appended")

# Verify
reg2 = ev_registry.load()
stage4 = reg2[reg2['evaluation'] == 'tv_strategy_catalog_stage4']
print(f"\nStage 4 registry rows: {len(stage4)}")
if 'fdr_pass' in reg2.columns:
    fdr_values = reg2[reg2['evaluation'] == 'tv_strategy_catalog_stage4']['value']
    print(f"fdr_pass values: {fdr_values.unique()}")
else:
    print("fdr_pass column not in registry")

# Check catalog
from strategies.catalog import build_catalog_rows
catalog = build_catalog_rows()
print(f"\nCatalog shape: {catalog.shape}")
if 'fdr_pass' in catalog.columns:
    survivors = catalog[catalog['fdr_pass'] == True]
    print(f"FDR survivors in catalog ({len(survivors)}):")
    print(survivors[['strategy_id', 'pnl_p', 'bh_q', 'fdr_pass', 'stage']].to_string())
else:
    print("fdr_pass column not in catalog")