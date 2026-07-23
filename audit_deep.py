#!/usr/bin/env python
"""Deep-dive: Hive shadowing + duplicate rates on populated tables."""

import os, sys, glob, duckdb
from pathlib import Path

os.chdir(r"C:\Users\zande\PycharmProjects\financial-data-pipeline")
sys.path.insert(0, r"C:\Users\zande\PycharmProjects\financial-data-pipeline")

from query import CATALOG

con = duckdb.connect(":memory:")

# === HIVE SHADOWING DEEP DIVE ===
print("=" * 80)
print("HIVE SHADOWING DEEP DIVE")
print("=" * 80)
print("Checking if year/month columns are real data or partition artifacts...\n")

shadow_tables = ["prices", "fundamentals_annual", "bls_cpi", "treasury_debt", "institutional_holdings"]
for t in shadow_tables:
    try:
        raw_path = CATALOG[t]
        df = con.execute(f"SELECT * FROM read_parquet('{raw_path}') LIMIT 5").fetchdf()

        has_date = "date" in df.columns
        has_year = "year" in df.columns
        has_month = "month" in df.columns
        has_fetched_at = "fetched_at" in df.columns

        print(f"{t}:")
        print(f"  Has date col: {has_date}, Has year col: {has_year}, Has month col: {has_month}, Has fetched_at: {has_fetched_at}")

        if has_date and has_year:
            sample = con.execute(f"SELECT date, year, month FROM read_parquet('{raw_path}') LIMIT 3").fetchdf()
            print(f"  Sample rows:")
            for _, row in sample.iterrows():
                print(f"    date={row['date']}, year={row['year']}, month={row['month']}")

            year_stats = con.execute(f"SELECT MIN(year) as min_y, MAX(year) as max_y, COUNT(DISTINCT year) as n_years FROM read_parquet('{raw_path}')").fetchone()
            print(f"  Year stats: min={year_stats[0]}, max={year_stats[1]}, distinct={year_stats[2]}")

        if has_date:
            date_range = con.execute(f"SELECT MIN(date) as min_d, MAX(date) as max_d FROM read_parquet('{raw_path}')").fetchone()
            print(f"  Date range: {date_range[0]} to {date_range[1]}")
        print()
    except Exception as e:
        print(f"{t}: ERROR - {e}\n")

# === DUPLICATE RATES ON POPULATED TABLES ===
print("=" * 80)
print("DUPLICATE RATES ON POPULATED TABLES")
print("=" * 80)

populated = ["prices", "fundamentals_annual", "fundamentals_quarterly",
             "simfin_income", "simfin_balance", "simfin_cashflow",
             "commodities", "macro", "bls_cpi", "bls_ppi", "bls_employment",
             "treasury_debt", "treasury_auctions", "world_bank",
             "earnings_calendar", "insider_transactions", "ipo_calendar",
             "short_interest", "sec_ftd", "tiingo_prices",
             "institutional_holdings", "gas_spot", "gas_retail",
             "eia_petroleum_stocks", "eia_natgas_storage"]

for t in populated:
    if t not in CATALOG:
        continue
    try:
        raw_path = CATALOG[t]
        df = con.execute(f"SELECT * FROM read_parquet('{raw_path}')").fetchdf()
        total = len(df)
        distinct = df.drop_duplicates().shape[0]
        dupes = total - distinct
        pct = (dupes / total * 100) if total > 0 else 0
        print(f"  {t}: {total:,} rows, {distinct:,} distinct, {dupes:,} dupes ({pct:.1f}%)")
    except Exception as e:
        print(f"  {t}: ERROR - {e}")

# === CURATED vs RAW ROW COUNTS ===
print()
print("=" * 80)
print("CURATED vs RAW ROW COUNTS (top populated tables)")
print("=" * 80)

from curated import KEYS
print(f"KEYS configured for {len(KEYS)} tables\n")

for t in ["prices", "fundamentals_annual", "bls_cpi", "institutional_holdings", "tiingo_prices"]:
    if t not in CATALOG:
        continue
    try:
        raw_path = CATALOG[t]
        raw_count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{raw_path}')").fetchone()[0]

        curated_path = Path(r"C:\Users\zande\PycharmProjects\financial-data-pipeline\storage\curated") / t / f"{t}.parquet"
        if curated_path.exists():
            cur_count = con.execute(f"SELECT COUNT(*) FROM '{curated_path}'").fetchone()[0]
            print(f"  {t}: raw={raw_count:,}, curated={cur_count:,}, removed={raw_count - cur_count:,}")
        else:
            print(f"  {t}: raw={raw_count:,}, NO CURATED FILE")
    except Exception as e:
        print(f"  {t}: ERROR - {e}")
