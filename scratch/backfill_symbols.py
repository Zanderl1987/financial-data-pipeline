"""
One-time backfill: populate the 'symbol' column in existing full-market
fundamentals parquets using the cached CIK map.
"""
import json
import os
import glob
import pandas as pd

CIK_MAP_PATH = os.path.join("storage", "raw", "fundamentals", "cik_map.json")
FUNDAMENTALS_DIR = os.path.join("storage", "raw", "fundamentals")


def load_cik_to_ticker():
    from fundamentals_pipeline import load_cik_map, build_cik_to_ticker
    return build_cik_to_ticker(load_cik_map())


def backfill_file(path, cik_to_ticker):
    df = pd.read_parquet(path)

    # Re-derive symbol from CIK for every row so corrections (e.g. GOOGN->GOOGL)
    # overwrite previously written values, not just blank ones.
    df["symbol"] = df["cik"].map(cik_to_ticker).fillna("")

    blank = df["symbol"].eq("").sum()
    filled = df["symbol"].ne("").sum()
    df.to_parquet(path, index=False, compression="snappy")
    print(f"  {os.path.basename(path)}: {filled:,} rows with ticker, {blank:,} blank (private/foreign)")


def main():
    cik_to_ticker = load_cik_to_ticker()
    print(f"Loaded {len(cik_to_ticker):,} CIK->ticker mappings.")

    targets = sorted(glob.glob(os.path.join(FUNDAMENTALS_DIR, "**", "fundamentals_full_*.parquet"), recursive=True))
    if not targets:
        print("No full-market parquet files found to backfill.")
        return

    for path in targets:
        print(f"Patching {path}...")
        backfill_file(path, cik_to_ticker)

    print("\nDone.")


if __name__ == "__main__":
    main()
