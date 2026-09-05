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

A comprehensive financial dataset covering **226 tables** and **131,784,934 rows** across macro, market, and alternative data sources.

## Data Sources

| Category | Tables | Key Sources |
|---|---|---|
| Market Prices | 18 | Tiingo, Schwab, Finnhub, CBOE |
| Macro & Economic | 87 | FRED, BLS, BEA, Treasury, EIA |
| Fundamentals | 75 | SEC EDGAR, Finnhub, SimFin, Alpha Vantage |
| Alternative Data | 32 | Congressional trades, insider transactions, patents, OpenFDA |
| Crypto & Forex | 8 | CoinGecko, Tiingo |
| Index & Holdings | 4 | Wikipedia, BlackRock, EdgarTools, OpenFIGI |
| Sentiment & News | 2 | Finnhub, Reddit, Google Trends, Fed speeches |

## Usage

```python
from datasets import load_dataset

# Load entire dataset
ds = load_dataset("ZanderL1337/financial-data-pipeline", trust_remote_code=True)

# Load specific table
df = ds["alpha_vantage_dividends"].to_pandas()
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

## Engineering & data quality

This snapshot is the output of a tested pipeline, not a one-off scrape:

- **761 automated tests**, including guard tests that fail the suite if a pipeline
  is added but not wired into the query catalog, schema registry, and curated-table
  dedup step — so a table can't silently go missing from downstream queries.
- **Schema/null-rate/range validation** (`validate.py`) runs as an operational health
  check against every table after each pipeline run.
- **Raw vs. curated separation**: pipelines write Hive-partitioned raw Parquet, which
  can contain overlapping re-fetches (measured up to ~42% duplicate rows on some raw
  tables); a dedup step (`curated.py`) produces the deduplicated tables published here.
- **Point-in-time discipline**: downstream feature joins use filing/publication date
  with explicit lags rather than observation date, so a backtest can't accidentally
  see information before it was actually available. `fetched_at` records ingestion
  time separately from the business dates already present in the source data (e.g.
  dividend tables carry `declaration_date`/`record_date`/`payment_date`/
  `ex_dividend_date`, which are four different points in time for the same event).
- **Iceberg pilot**: a subset of tables (`prices`, `macro`, `fundamentals_annual`,
  `fundamentals_quarterly`) are additionally mirrored into an Apache Iceberg warehouse
  with real snapshot-based reads and automated snapshot expiration (30-day retention),
  as a pilot toward full lakehouse-style versioning of the rest of the store.

Full source, tests, and architecture docs: https://github.com/Zanderl1987/financial-data-pipeline

## Build Info

- **Generated**: 2026-09-05
- **Pipeline**: financial-data-pipeline (https://github.com/Zanderl1987/financial-data-pipeline)
- **Tables**: 226
- **Total Rows**: 131,784,934
- **Total Size**: 4763.4 MB

## License

CC BY 4.0 — data sourced from public APIs and government databases.
