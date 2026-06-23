"""Patch fundamentals parquets with JPM and GS data using bank-specific XBRL concepts."""
import os
import glob
import pandas as pd
from fundamentals_pipeline import fetch_company_facts, extract_company, load_cik_map

OUTPUT_DIR = os.path.join("storage", "raw", "fundamentals")
cik_map = load_cik_map()

BANKS = {"JPM": cik_map["JPM"], "GS": cik_map["GS"]}

annual_files  = sorted(glob.glob(os.path.join(OUTPUT_DIR, "annual",    "**", "*.parquet"), recursive=True))
quarterly_files = sorted(glob.glob(os.path.join(OUTPUT_DIR, "quarterly", "**", "*.parquet"), recursive=True))

if not annual_files:
    raise SystemExit("No annual parquet files found.")

ann_df  = pd.concat([pd.read_parquet(f) for f in annual_files],  ignore_index=True)
qtr_df  = pd.concat([pd.read_parquet(f) for f in quarterly_files], ignore_index=True) if quarterly_files else pd.DataFrame()

# Normalize period_end to string so new rows (str) concat cleanly with existing (datetime)
for _df in (ann_df, qtr_df):
    if "period_end" in _df.columns:
        _df["period_end"] = pd.to_datetime(_df["period_end"], errors="coerce").dt.strftime("%Y-%m-%d")

for sym, cik in BANKS.items():
    print(f"\nFetching {sym} (CIK {cik})...")
    data = fetch_company_facts(cik)
    if not data:
        print(f"  ERROR: no data for {sym}")
        continue

    new_annual, new_quarterly = extract_company(data, symbol=sym)
    new_ann = pd.DataFrame(new_annual)
    new_qtr = pd.DataFrame(new_quarterly)

    if new_ann.empty:
        print(f"  No annual data extracted for {sym}")
    else:
        ann_df = ann_df[ann_df["symbol"] != sym]
        ann_df = pd.concat([ann_df, new_ann], ignore_index=True)
        print(f"  Added {len(new_ann):,} annual rows for {sym}")

    if not new_qtr.empty and not qtr_df.empty:
        qtr_df = qtr_df[qtr_df["symbol"] != sym]
        qtr_df = pd.concat([qtr_df, new_qtr], ignore_index=True)
        print(f"  Added {len(new_qtr):,} quarterly rows for {sym}")

# Write back to the first (or only) file — same convention as prior patches
ann_out  = annual_files[0]
ann_df.to_parquet(ann_out, index=False, compression="snappy")
print(f"\nSaved annual   -> {ann_out} ({len(ann_df):,} total rows)")

if not qtr_df.empty and quarterly_files:
    qtr_out = quarterly_files[0]
    qtr_df.to_parquet(qtr_out, index=False, compression="snappy")
    print(f"Saved quarterly -> {qtr_out} ({len(qtr_df):,} total rows)")

print("\nDone.")
