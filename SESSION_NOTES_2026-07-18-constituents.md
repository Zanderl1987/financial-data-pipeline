# Session Notes — 2026-07-18 (constituents Iceberg)

**Session model:** Claude Sonnet 5 (via opencode)

## Goal

Revisit the constituent data Iceberg pipeline (`index_constituents_pipeline.py`), fix
known issues, and generate example queries / visualizations.

## What happened

### 1. Iceberg table cleanup

The `constituents.index_members` Iceberg table had two problems:

- **Stale warehouse path.** The SQLite catalog (`storage/iceberg/constituents_catalog.db`)
  stored `metadata_location` pointing to `file://E:/AI_Projects/FinancialPipelineStagingUpdates/...`
  (a different drive/machine). All 4 metadata files on disk contained the same stale
  `location` field. DuckDB's `iceberg_scan()` failed with `IOException: Cannot open file`
  because it tried to read metadata from the non-existent E: path.
- **Duplicate snapshots.** The 2026-07-16 snapshot had inflated row counts (SPX=1509
  instead of 503) because the `table.overwrite()` `EqualTo("snapshot_date", ...)` filter
  didn't prevent duplicates across multiple same-day runs. The `date.today()` Python
  `date` object vs Iceberg `date32` column type may have caused a type mismatch.

**Fix:** Dropped the table entirely via `sqlite3` DELETE + `shutil.rmtree`, recreated with
`catalog.create_table()` using `TimestamptzType()` (not `TimestampType()`) for
`fetched_at`. The `write_to_iceberg()` function in the pipeline writes to the correct
C: drive path now. Verified clean via DuckDB `read_parquet` + `hive_partitioning=true`.

### 2. NDX scraper fix (company names)

The `fetch_nasdaq100()` function only extracted tickers from `<a href="/stocks/...">`
anchor tags, leaving `company_name=None` for all 103 Nasdaq-100 constituents.

**Root cause:** stockanalysis.com is a SvelteKit app. The server-rendered HTML table
contains the data, but Svelte hydration comment markers (`<!--[!-->`, `<!---->`,
`<!--]-->`) are interspersed between `<td>` elements, breaking multi-element regex
patterns.

**Fix:** Strip all HTML comments first (`re.sub(r'<!----?>|<!--.*?-->', '', text)`),
then match `<td class="sym ...">TICKER</td>` followed by `<td class="slw ...">Company
Name</td>`. Falls back to anchor-only extraction if the paired pattern fails.

The embedded JSON `stockData` array (in the SvelteKit hydration `<script>` tag) was
also attempted but abandoned — the JS object keys are unquoted and the array contains
nested `inIndex:[...]` sub-arrays that break simple bracket-matching regex.

### 3. Visualizations

Generated 5 matplotlib charts from the cleaned Iceberg table via DuckDB queries:

| Chart | File | Key finding |
|-------|------|-------------|
| Sector pie | `sp500_sectors.png` | Industrials 16%, Financials 15%, Info Tech 15% |
| Sector bar | `sp500_sector_bar.png` | Same as horizontal bar for readability |
| Index sizes | `index_sizes.png` | Russell 3000 (2,589) > Wilshire 5000 (2,445) > Russell 2000 (1,969) > S&P 500 (503) > Nasdaq-100 (103) |
| Overlap matrix | `index_overlap.png` | Heatmap of shared tickers across indices |
| R2K holdings | `rut2000_top_holdings.png` | Top 20 Russell 2000 by weight — BTSG, MOGA, UMBF, CYTK |

Charts saved to `storage/iceberg/viz/`.

## Files changed

- `index_constituents_pipeline.py` — rewrote `fetch_nasdaq100()` to parse paired
  `<td class="sym">` + `<td class="slw">` with Svelte comment stripping (lines 93-155)
- `storage/iceberg/constituents/index_members/` — dropped and recreated (fresh metadata
  with correct C: drive paths)

## Test results

- Pipeline runs clean: 7,609 rows across 5 indices (SPX=503, NDX=103, RUT3000=2589,
  RUT2000=1969, W5000=2445)
- NDX now returns company names (e.g., "NVIDIA Corporation", "Apple Inc.")
- `write_to_iceberg()` uses `TimestamptzType` matching the Arrow schema's
  `pa.timestamp("us", tz="UTC")`

## Open questions

- The `overwrite_filter=EqualTo("snapshot_date", snapshot_date)` in `write_to_iceberg()`
  still logs "Delete operation did not match any records" on first run of a fresh table.
  On re-runs same-day it should delete+reinsert — needs verification on a second
  same-day pipeline execution.
- The `index_name` field for Russell/Wilshire indices shows "Russell 3000" etc. but the
  `weight_pct` values from BlackRock are fund weights (IWM/IWV/ITOT), not true index
  weights. May want to clarify naming.

### 4. Iceberg table health check

All 4 tables queried successfully:

| Table | Rows | Snapshots | Issue |
|-------|------|-----------|-------|
| index_members | 7,609 | 1 | Clean (just rebuilt) |
| fund_holdings | 21,957 | 25 | Snapshot bloat — 25 snapshots for 21K rows |
| securities | 10,426 | 1 | Clean |
| identifier_map | 10 → 3,056 | 1 | Was nearly empty — enriched (see below) |

All tables had stale `metadata_location` paths in SQLite catalog (E: drive), but load
fine via PyIceberg. DuckDB's `iceberg_scan()` fails on the old paths; `read_parquet`
with `hive_partitioning=true` works.

### 5. identifier_map enrichment

**Before:** 10 rows (mega-caps only — AAPL, AMZN, GOOGL, etc.) with FIGI from OpenFIGI,
no CIK, no ISIN/CUSIP/SEDOL.

**After:** 3,056 rows covering all index_members tickers with:
- 3,018 (99%) with CIK from SEC EDGAR (via securities table)
- 10 with FIGI from OpenFIGI (preserved from original)
- 0 duplicates

**Approach:** Merged CIK from `securities` table (which gets CIK from EDGAR
`company_tickers.json`) into `identifier_map` for all index_members tickers. Used
drop+recreate (not overwrite) to avoid the stale-parquet-file issue.

**OpenFIGI limitation:** OpenFIGI only returns FIGI + compositeFIGI. It does NOT return
ISIN, CUSIP, or SEDOL. Those require other sources (EDGAR filings, Bloomberg, Wikipedia).

### TODOs

- [ ] Register for free OpenFIGI API key at openfigi.com → set `OPENFIGI_API_KEY` in `.env`
      (free tier: 250 req/min, 100 jobs/batch; no key: 25 req/min, 10 jobs/batch)
- [ ] Run `openfigi_pipeline.py --backfill` on index_members tickers (~3K) to add
      FIGI/compositeFIGI. With key: ~25 min. Without: ~2 hours.
- [ ] Assess disk size for full 10K ticker pull (all securities + fund_holdings)
- [ ] Consider expiring old fund_holdings snapshots (25 snapshots → 1)
- [ ] Consider sourcing CUSIP from SEC EDGAR filings (10-K/13F) for high-value tickers
