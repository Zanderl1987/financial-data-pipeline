"""Lossless check: replaying stored changes must reproduce a direct as_of list.

Usage: python scripts/massive_lossless_check.py <out_root>
"""
import glob
import os
import sys

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import massive_option_listings_pipeline as mol

root = sys.argv[1]
load_dotenv()
ch = pd.concat([pd.read_parquet(f) for f in glob.glob(
    f"{root}/raw/massive/option_listing_changes/**/*.parquet", recursive=True)],
    ignore_index=True)
summ = pd.concat([pd.read_parquet(f) for f in glob.glob(
    f"{root}/raw/massive/option_chain_summary/**/*.parquet", recursive=True)],
    ignore_index=True)
ok_days = summ[summ["status"] == "ok"]
client = mol.MassiveClient(os.environ["MASSIVE_API_KEY"])
bad = 0
for _, row in ok_days.sample(3, random_state=7).iterrows():
    sym, d = row["symbol"], row["date"]
    replayed = mol.replay_live_set(ch[ch["symbol"] == sym], d)
    direct = set(mol.fetch_contracts(client, sym, d)["contract_ticker"])
    diff = len(replayed ^ direct)
    bad += diff > 0
    print(f"{sym} {d}: replayed={len(replayed)} direct={len(direct)} mismatches={diff}", flush=True)
print("LOSSLESS OK" if bad == 0 else f"LOSSLESS FAIL ({bad}/3)")
