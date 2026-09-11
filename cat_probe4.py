#!/usr/bin/env python3
import sys
sys.path.insert(0, r"C:\Users\zande\PycharmProjects\financial-data-pipeline")
from strategies.catalog import load_catalog_table
cat = load_catalog_table()
print("rows:", len(cat), " stages:", cat["stage"].value_counts().to_dict())
print(cat[cat["holdout_pnl_p"].notna()][["strategy_id","stage","pnl_p","holdout_pnl_p","provisional"]].to_string(index=False))
print("fdr contains any:", int(cat["bh_q"].notna().sum()))