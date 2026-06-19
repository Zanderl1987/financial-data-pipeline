# Financial Data Pipeline — Future Improvements

## Completed

### Snappy Compression on All Parquet Outputs
**Status:** Implemented 2026-06-19

All `to_parquet()` calls across every pipeline now pass `compression="snappy"`. This reduces file sizes by roughly 40–60% compared to uncompressed Parquet, with faster read/write speeds than heavier codecs like gzip or brotli. Snappy trades a slightly larger file size for significantly faster decompression — the right default for a pipeline that reads its own output frequently.

Files updated:
- `commodity_macro_pipeline.py`
- `fundamentals_pipeline.py`
- `futures_pipeline.py`
- `gas_price_pipeline.py`
- `options_chain_pipeline.py`
- `price_history_pipeline.py`
- `synthetic_options_pipeline.py`
- `yahoo_options_pipeline.py`

---

## Candidate Improvements

### 1. DuckDB Query Layer
**Priority: High | Effort: Low**

DuckDB can query Parquet files directly with SQL — no database server, no ETL, no catalog. It reads multiple files in one query using glob patterns.

```python
import duckdb
df = duckdb.query("""
    SELECT symbol, date, close
    FROM 'storage/raw/prices/prices_*.parquet'
    WHERE symbol = 'AAPL'
    ORDER BY date
""").df()
```

This would be the single highest-value addition to this pipeline. Useful for:
- Cross-pipeline joins (e.g. options chains joined to fundamentals)
- Backtest data slicing without loading entire files into memory
- Ad hoc analysis without writing pandas boilerplate

**Recommendation:** Add a `query.py` helper or a `notebooks/` directory with example DuckDB queries against the stored Parquet files.

---

### 2. Apache Iceberg Tables
**Priority: Low–Medium | Effort: High**

Apache Iceberg is an open table format that sits on top of Parquet files and adds:
- **Time travel** — query data exactly as it existed at any past snapshot
- **Schema evolution** — add/rename columns without rewriting old files
- **ACID transactions** — safe concurrent writes from multiple processes
- **Partition pruning** — the catalog knows which files contain which date ranges, so queries skip irrelevant files automatically

**Why not yet:** Iceberg requires a catalog backend (SQLite, REST server, AWS Glue, or Hive Metastore) and meaningful operational overhead. The dated filename convention already provides a simpler form of time-travel (`prices_incremental_20260619.parquet`), which is sufficient at current scale.

**When to revisit:** If total stored data exceeds ~50GB, if multiple processes need to write concurrently, or if time-travel queries across all history become a regular need. At that point, [PyIceberg](https://py.iceberg.apache.org/) with a local SQLite catalog is the lowest-friction entry point.

---

### 3. Partition-Aware Storage Layout
**Priority: Medium | Effort: Medium**

Currently files are stored flat with the date encoded in the filename. A Hive-style partition layout would make range queries faster when using DuckDB or Spark:

```
storage/raw/prices/year=2025/month=06/prices.parquet
storage/raw/prices/year=2026/month=06/prices.parquet
```

DuckDB and PyArrow both understand Hive partitioning natively via `dataset.partitioning("hive")`. This is a stepping stone toward Iceberg without the catalog overhead.

---

### 4. Data Validation Layer
**Priority: Medium | Effort: Low–Medium**

Add lightweight schema + freshness checks after each pipeline run:
- Row count plausibility (warn if output is >50% smaller than previous run)
- Expected columns present and correctly typed
- No future dates in the `date` column
- `fetched_at` within the last hour

[Pandera](https://pandera.readthedocs.io/) or a simple hand-rolled check function would work. Catches silent data quality regressions early.

---

### 5. Unified Pipeline Runner
**Priority: Low | Effort: Medium**

A `run_all.py` script (or a `Makefile`) that runs all pipelines in dependency order and logs success/failure per pipeline. Would make daily incremental runs a single command instead of invoking each file individually.
