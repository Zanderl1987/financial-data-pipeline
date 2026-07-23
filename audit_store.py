#!/usr/bin/env python
"""Comprehensive store audit: empty tables, staleness, dupes, shadowing, schema drift."""

import os, sys, glob, time, json
from pathlib import Path
from datetime import datetime, timezone

import duckdb
import pandas as pd

os.chdir(r"C:\Users\zande\PycharmProjects\financial-data-pipeline")
sys.path.insert(0, r"C:\Users\zande\PycharmProjects\financial-data-pipeline")

from query import CATALOG, USE_CURATED

ROOT = Path(r"C:\Users\zande\PycharmProjects\financial-data-pipeline")
STORAGE = ROOT / "storage"
RAW = STORAGE / "raw"
CURATED = STORAGE / "curated"

con = duckdb.connect(":memory:")

# ── 1. Row counts: raw vs curated ──────────────────────────────────────
print("=" * 80)
print("CHECK 1: Row counts (empty-but-wired detection)")
print("=" * 80)

results = []
for table, pattern in CATALOG.items():
    raw_path = str(pattern)
    curated_path = CURATED / table / f"{table}.parquet"

    raw_count = 0
    curated_count = 0
    raw_files = 0
    curated_exists = curated_path.exists()

    try:
        # Count raw rows via DuckDB glob
        raw_files_list = glob.glob(raw_path.replace("**/*.parquet", "*.parquet"))
        # Also check year/month subdirs
        raw_files_list += glob.glob(raw_path)
        raw_files_list = list(set(raw_files_list))
        raw_files = len([f for f in raw_files_list if f.endswith(".parquet")])

        if raw_files > 0:
            raw_df = con.execute(f"SELECT COUNT(*) as cnt FROM read_parquet('{raw_path}')").fetchone()
            raw_count = raw_df[0]
    except Exception as e:
        raw_count = f"ERR: {e}"

    try:
        if curated_exists:
            cur_df = con.execute(f"SELECT COUNT(*) as cnt FROM '{curated_path}'").fetchone()
            curated_count = cur_df[0]
    except Exception as e:
        curated_count = f"ERR: {e}"

    status = "OK"
    if isinstance(raw_count, int) and isinstance(curated_count, int):
        if raw_count == 0 and curated_count == 0:
            status = "EMPTY"
        elif curated_count == 0 and raw_count > 0:
            status = "NO_CURATED"
    elif isinstance(raw_count, str) and raw_count.startswith("ERR"):
        status = "RAW_ERR"
    elif isinstance(curated_count, str) and curated_count.startswith("ERR"):
        status = "CURATED_ERR"

    results.append({
        "table": table,
        "raw_count": raw_count,
        "curated_count": curated_count,
        "raw_files": raw_files,
        "curated_exists": curated_exists,
        "status": status,
    })

df_results = pd.DataFrame(results)
empty_tables = df_results[df_results["status"].isin(["EMPTY", "NO_CURATED", "RAW_ERR", "CURATED_ERR"])]
print(f"\nTotal tables: {len(df_results)}")
print(f"Tables with issues: {len(empty_tables)}")
if len(empty_tables) > 0:
    print("\nProblem tables:")
    for _, row in empty_tables.iterrows():
        print(f"  {row['table']}: raw={row['raw_count']}, curated={row['curated_count']}, files={row['raw_files']}, status={row['status']}")
else:
    print("  All tables have data!")

# ── 2. Curated freshness vs raw ────────────────────────────────────────
print("\n" + "=" * 80)
print("CHECK 2: Curated freshness (stale snapshot detection)")
print("=" * 80)

stale_tables = []
now = time.time()
for table in CATALOG:
    curated_path = CURATED / table / f"{table}.parquet"
    if not curated_path.exists():
        continue

    curated_mtime = curated_path.stat().st_mtime
    curated_age_days = (now - curated_mtime) / 86400

    # Find newest raw file for this table
    raw_pattern = CATALOG[table]
    raw_files = glob.glob(raw_pattern)
    if not raw_files:
        continue

    newest_raw_mtime = max(os.path.getmtime(f) for f in raw_files)
    newest_raw_age_days = (now - newest_raw_mtime) / 86400

    if curated_mtime < newest_raw_mtime:
        stale_tables.append({
            "table": table,
            "curated_age_days": round(curated_age_days, 1),
            "newest_raw_age_days": round(newest_raw_age_days, 1),
        })

if stale_tables:
    print(f"\nStale curated tables ({len(stale_tables)}):")
    for t in sorted(stale_tables, key=lambda x: x["curated_age_days"], reverse=True):
        print(f"  {t['table']}: curated={t['curated_age_days']}d old, newest raw={t['newest_raw_age_days']}d old")
else:
    print("  All curated snapshots are fresh!")

# ── 3. Duplicate detection (sampled) ──────────────────────────────────
print("\n" + "=" * 80)
print("CHECK 3: Duplicate detection (sampling top 30 tables by raw size)")
print("=" * 80)

# Sample tables that are likely to have dupes (incremental re-fetches)
sample_tables = ["prices", "fundamentals_annual", "fundamentals_quarterly",
                 "institutional_holdings", "insider_transactions", "earnings",
                 "options_chain", "options_metrics", "simfin_income",
                 "sec_filings", "news", "congressional_trades"]

for table in sample_tables:
    if table not in CATALOG:
        continue
    try:
        raw_path = CATALOG[table]
        df = con.execute(f"SELECT * FROM read_parquet('{raw_path}')").fetchdf()
        total = len(df)
        # Use all columns for dedup (no natural key assumption)
        distinct = df.drop_duplicates().shape[0]
        dupes = total - distinct
        pct = (dupes / total * 100) if total > 0 else 0
        if pct > 1:
            print(f"  {table}: {total} rows, {distinct} distinct, {dupes} dupes ({pct:.1f}%)")
    except Exception as e:
        print(f"  {table}: ERROR - {e}")

# ── 4. Hive partition shadowing ────────────────────────────────────────
print("\n" + "=" * 80)
print("CHECK 4: Hive partition column shadowing (year/month)")
print("=" * 80)

shadowed = []
for table in list(CATALOG.keys())[:50]:  # Sample first 50
    try:
        raw_path = CATALOG[table]
        df = con.execute(f"SELECT * FROM read_parquet('{raw_path}') LIMIT 100").fetchdf()
        if "year" in df.columns and "month" in df.columns:
            # Check if year values are suspiciously uniform (all same year = fetch date shadowing)
            year_vals = df["year"].dropna().unique()
            if len(year_vals) <= 2:  # Suspiciously uniform
                month_vals = df["month"].dropna().unique()
                shadowed.append({
                    "table": table,
                    "year_values": sorted(year_vals.tolist()),
                    "month_values": sorted(month_vals.tolist()),
                    "sample_cols": list(df.columns[:5]),
                })
    except Exception:
        pass

if shadowed:
    print(f"\nPossibly shadowed tables ({len(shadowed)}):")
    for t in shadowed:
        print(f"  {t['table']}: year={t['year_values']}, month={t['month_values']}")
        print(f"    columns: {t['sample_cols']}")
else:
    print("  No suspicious year/month shadowing detected in sample.")

# ── 5. Validate.py check ──────────────────────────────────────────────
print("\n" + "=" * 80)
print("CHECK 5: Schema / validate.py health")
print("=" * 80)

try:
    from validate import SCHEMAS
    validate_tables = set(SCHEMAS.keys())
    catalog_tables = set(CATALOG.keys())
    in_validate_not_catalog = validate_tables - catalog_tables
    in_catalog_not_validate = catalog_tables - validate_tables
    print(f"  validate.py schemas: {len(validate_tables)}")
    print(f"  CATALOG tables: {len(catalog_tables)}")
    if in_validate_not_catalog:
        print(f"  In validate.py but NOT in CATALOG ({len(in_validate_not_catalog)}): {sorted(in_validate_not_catalog)[:10]}...")
    if in_catalog_not_validate:
        print(f"  In CATALOG but NOT in validate.py ({len(in_catalog_not_validate)}): {sorted(in_catalog_not_validate)[:10]}...")
    if not in_validate_not_catalog and not in_catalog_not_validate:
        print("  Perfect alignment!")
except Exception as e:
    print(f"  ERROR importing validate.py: {e}")

# ── Summary ────────────────────────────────────────────────────────────
print("\n" + "=" * 80)
print("AUDIT SUMMARY")
print("=" * 80)

issues = []
for _, row in df_results.iterrows():
    if row["status"] != "OK":
        issues.append(f"  {row['table']}: {row['status']} (raw={row['raw_count']}, curated={row['curated_count']})")

if stale_tables:
    for t in stale_tables:
        issues.append(f"  {t['table']}: STALE curated ({t['curated_age_days']}d) vs raw ({t['newest_raw_age_days']}d)")

if shadowed:
    for t in shadowed:
        issues.append(f"  {t['table']}: POSSIBLE Hive shadowing (year={t['year_values']})")

if issues:
    print(f"\n{len(issues)} issues found:")
    for i in issues:
        print(i)
else:
    print("\nNo issues found! Store is healthy.")

print(f"\nAudit completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
