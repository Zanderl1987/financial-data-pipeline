#!/usr/bin/env python3
"""
Upload the full curated financial data pipeline to HuggingFace.

Usage:
    python upload_huggingface.py [--repo-name financial-data-pipeline] [--private]

Requires HUGGINGFACE_TOKEN or HF_TOKEN env variable.
"""

import os
import sys
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from huggingface_hub import HfApi, login

STORAGE_ROOT = Path(__file__).parent / "storage" / "curated"
README_TEMPLATE = """---
language:
  - en
tags:
  - finance
  - economics
  - alternative-data
  - macro
  - market-data
  - pipeline
task_categories:
  - other
size_categories:
  - 100MB<n<1GB
---

# Financial Data Pipeline — Full Curated Snapshot

A comprehensive financial dataset covering **{n_tables} tables** and **{n_rows:,} rows** across macro, market, and alternative data sources.

## Data Sources

| Category | Tables | Key Sources |
|---|---|---|
| Market Prices | {n_market} | Tiingo, Schwab, Finnhub, CBOE |
| Macro & Economic | {n_macro} | FRED, BLS, BEA, Treasury, EIA |
| Fundamentals | {n_fund} | SEC EDGAR, Finnhub, SimFin, Alpha Vantage |
| Alternative Data | {n_alt} | Congressional trades, insider transactions, patents, OpenFDA |
| Crypto & Forex | {n_crypto} | CoinGecko, Tiingo |
| Index & Holdings | {n_index} | Wikipedia, BlackRock, EdgarTools, OpenFIGI |
| Sentiment & News | {n_sent} | Finnhub, Reddit, Google Trends, Fed speeches |

## Usage

```python
from datasets import load_dataset

# Load entire dataset
ds = load_dataset("{repo_id}", trust_remote_code=True)

# Load specific table
df = ds["{first_table}"].to_pandas()
```

Or load individual parquet files directly:

```python
import pandas as pd

df = pd.read_parquet("path/to/parquet/file.parquet")
```

## Schema

Each table is stored as a separate parquet file. Key columns:

- `date` / `snapshot_date`: Temporal columns (where applicable)
- `symbol` / `ticker`: Security identifiers
- `fetched_at`: UTC timestamp when data was fetched
- `year` / `month`: Hive partition columns (where applicable)

## Build Info

- **Generated**: {generated_date}
- **Pipeline**: financial-data-pipeline (https://github.com/Zanderl1987/financial-data-pipeline)
- **Tables**: {n_tables}
- **Total Rows**: {n_rows:,}
- **Total Size**: {total_size_mb:.1f} MB

## License

CC BY 4.0 — data sourced from public APIs and government databases.
"""


def count_rows(parquet_path: Path) -> int:
    """Count rows in a parquet file without loading full DataFrame."""
    import pyarrow.parquet as pq
    return pq.read_metadata(str(parquet_path)).num_rows


def main(repo_name: str = "financial-data-pipeline", private: bool = False):
    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    if not token:
        print("ERROR: Set HUGGINGFACE_TOKEN or HF_TOKEN env variable.")
        return

    # Scan curated files
    parquet_files = sorted(STORAGE_ROOT.glob("**/*.parquet"))
    print(f"Found {len(parquet_files)} parquet files")

    if not parquet_files:
        print(f"ERROR: No parquet files found under {STORAGE_ROOT} -- "
              f"refusing to publish an empty snapshot.")
        return

    login(token=token)
    api = HfApi()

    repo_id = f"ZanderL1337/{repo_name}"
    print(f"Creating/updating repo: {repo_id} (private={private})")

    api.create_repo(repo_id, repo_type="dataset", private=private, exist_ok=True)

    # create_repo's `private` only applies when it actually creates the repo --
    # with exist_ok=True it silently no-ops on an existing one, so --private
    # would print "private=True" and still publish to a public repo. Found in
    # the shipping pipeline 2026-08-10, where that sent a real upload out
    # publicly. Enforce the requested visibility BEFORE any data is uploaded.
    current = api.dataset_info(repo_id).private
    if current != private:
        print(f"  repo already existed with private={current}; setting private={private}")
        api.update_repo_settings(repo_id=repo_id, repo_type="dataset", private=private)

    # Count rows and categorize
    total_rows = 0
    table_stats = []
    categories = {"market": 0, "macro": 0, "fund": 0, "alt": 0, "crypto": 0, "index": 0, "sent": 0}

    market_prefixes = ("tiingo_", "schwab_", "finnhub_quotes", "finnhub_profile", "finnhub_metrics",
                        "prices", "options_", "synthetic_", "sector_etfs", "market_")
    macro_prefixes = ("fred_", "bls_", "bea_", "treasury_", "eia_", "fed_", "ecb_",
                       "cboe_", "fdic_", "fear_", "oecd_", "wb_", "imf_", "fao_",
                       "noaa_", "world_", "usda_", "usgs_", "shiller_", "ff_")
    fund_prefixes = ("fundamentals_", "simfin_", "sec_edgar_", "sec_filings",
                      "alpha_vantage_", "finnhub_earnings", "finnhub_eps",
                      "finnhub_revenue", "finnhub_peers", "finnhub_executives",
                      "finnhub_ownership", "finnhub_splits", "finnhub_filing",
                      "finnhub_transcripts", "finnhub_company")
    alt_prefixes = ("congressional_", "insider_", "institutional_", "patents",
                     "openfda_", "finra_", "sec_ftd", "short_", "ipo_",
                     "sa_", "finviz_", "tv_", "ais_", "google_trends_")
    crypto_prefixes = ("coingecko_", "crypto_")
    index_prefixes = ("index_", "securities", "fund_holdings", "identifier_")
    sent_prefixes = ("news_", "reddit_", "fed_sentiment", "finnhub_news")

    for pf in parquet_files:
        name = pf.stem
        rows = count_rows(pf)
        total_rows += rows
        table_stats.append((name, rows, pf.stat().st_size))

        # Categorize
        if any(name.startswith(p) for p in market_prefixes):
            categories["market"] += 1
        elif any(name.startswith(p) for p in macro_prefixes):
            categories["macro"] += 1
        elif any(name.startswith(p) for p in fund_prefixes):
            categories["fund"] += 1
        elif any(name.startswith(p) for p in alt_prefixes):
            categories["alt"] += 1
        elif any(name.startswith(p) for p in crypto_prefixes):
            categories["crypto"] += 1
        elif any(name.startswith(p) for p in index_prefixes):
            categories["index"] += 1
        elif any(name.startswith(p) for p in sent_prefixes):
            categories["sent"] += 1
        else:
            categories["fund"] += 1  # default bucket

    total_size_mb = sum(s[2] for s in table_stats) / 1024 / 1024

    print(f"\n{len(parquet_files)} tables, {total_rows:,} rows, {total_size_mb:.1f} MB")
    print(f"  Market: {categories['market']}, Macro: {categories['macro']}, Fund: {categories['fund']}")
    print(f"  Alt: {categories['alt']}, Crypto: {categories['crypto']}, Index: {categories['index']}, Sent: {categories['sent']}")

    # Generate README
    readme = README_TEMPLATE.format(
        repo_id=repo_id,
        n_tables=len(parquet_files),
        n_rows=total_rows,
        total_size_mb=total_size_mb,
        generated_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        first_table=table_stats[0][0] if table_stats else "prices",
        n_market=categories["market"],
        n_macro=categories["macro"],
        n_fund=categories["fund"],
        n_alt=categories["alt"],
        n_crypto=categories["crypto"],
        n_index=categories["index"],
        n_sent=categories["sent"],
    )

    # Write README locally then upload
    readme_path = STORAGE_ROOT / "README.md"
    readme_path.write_text(readme, encoding="utf-8")

    # Upload all files
    print(f"\nUploading to {repo_id}...")
    api.upload_folder(
        folder_path=str(STORAGE_ROOT),
        repo_id=repo_id,
        repo_type="dataset",
        allow_patterns=["*.parquet", "README.md"],
        commit_message=f"Update curated snapshot ({len(parquet_files)} tables, {total_rows:,} rows)",
    )

    print(f"\nDone! Dataset: https://huggingface.co/datasets/{repo_id}")
    print(f"  Load with: ds = load_dataset('{repo_id}')")

    return {
        "repo_id": repo_id,
        "tables": len(parquet_files),
        "rows": total_rows,
        "size_mb": total_size_mb,
        "files": [
            str(pf.relative_to(STORAGE_ROOT)).replace(os.sep, "/")
            for pf in parquet_files
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload curated data to HuggingFace")
    parser.add_argument("--repo-name", default="financial-data-pipeline", help="HF repo name")
    parser.add_argument("--private", action="store_true", help="Make dataset private")
    args = parser.parse_args()
    main(repo_name=args.repo_name, private=args.private)
