---
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

A comprehensive financial dataset covering **114 tables** and **9,995,770 rows** across macro, market, and alternative data sources.

## Data Sources

| Category | Tables | Key Sources |
|---|---|---|
| Market Prices | 11 | Tiingo, Schwab, Finnhub, CBOE |
| Macro & Economic | 56 | FRED, BLS, BEA, Treasury, EIA |
| Fundamentals | 29 | SEC EDGAR, Finnhub, SimFin, Alpha Vantage |
| Alternative Data | 6 | Congressional trades, insider transactions, patents, OpenFDA |
| Crypto & Forex | 6 | CoinGecko, Tiingo |
| Index & Holdings | 4 | Wikipedia, BlackRock, EdgarTools, OpenFIGI |
| Sentiment & News | 2 | Finnhub, Reddit, Google Trends, Fed speeches |

## Usage

```python
from datasets import load_dataset

# Load entire dataset
ds = load_dataset("ZanderL1337/financial-data-pipeline", trust_remote_code=True)

# Load specific table
df = ds["alpha_vantage_forex"].to_pandas()
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

- **Generated**: 2026-07-18
- **Pipeline**: financial-data-pipeline (https://github.com/ZanderL1337/financial-data-pipeline)
- **Tables**: 114
- **Total Rows**: 9,995,770
- **Total Size**: 223.6 MB

## License

CC BY 4.0 — data sourced from public APIs and government databases.
