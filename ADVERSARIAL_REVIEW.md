> **Reconciliation note (2026-07-28):** This review was written against a local fork that had
> diverged from `origin/master` for about a month. When the two histories were reconciled, spot
> checks found the fork and `origin/master` had independently fixed some of the same issues while
> others remained open on both. Confirmed during reconciliation:
> - **#1 (news_sentiment os.listdir bug)** — already fixed on `origin/master` (`load_news()` now
>   uses `_glob_mod.glob(..., recursive=True)`).
> - **#3 (run_all.py subprocess output not captured)** — already fixed on `origin/master`
>   (`subprocess.run(..., capture_output=..., text=True)` plus per-pipeline failure logs).
> - **#4 (commodity_macro_pipeline.py import-time KeyError)** — still open as of this
>   reconciliation; `FRED_API_KEY = os.environ["FRED_API_KEY"]` will still crash at import time.
> - **#5 (futures_pipeline.py global `warnings.filterwarnings("ignore")`)** — still open.
>
> The remaining findings below were **not** re-verified against the current `origin/master` code
> and may already be fixed, partially fixed, or still fully open — treat line numbers as
> approximate and confirm against the current file before acting. `pipeline_logging/` (referenced
> in finding #21) no longer exists in this branch; that specific reference is stale.
>
> The **"Missing Data Sources" table** in the Expansion Plan below is also the source of the
> "9 new pipelines" batch from the same fork. During reconciliation, 6 of those 9 turned out to
> already be covered by `origin/master`'s own pipelines or to rely on FRED series IDs that don't
> match any real series (see `SESSION_NOTES.md`, 2026-07-28 entry). Treat any remaining unbuilt
> item in that table with the same skepticism — verify the data actually exists before building
> against it.

# Adversarial Code Review — Financial Data Pipeline

**Reviewer:** big-pickle (automated adversarial analysis)
**Date:** 2026-07-27
**Scope:** run_all.py, query.py, storage_utils.py, validate.py, pricing_models.py, pipeline_logging/, and 15 pipeline scripts

---

## Issue Index

| # | Severity | File | Line(s) | Summary |
|---|----------|------|---------|---------|
| 1 | CRITICAL | news_sentiment_pipeline.py | 131-142 | `load_news()` uses `os.listdir()` — misses Hive-partitioned files from finnhub_pipeline |
| 2 | CRITICAL | synthetic_options_pipeline.py | 356 | Hardcoded 4% rate fallback silently misprices all options |
| 3 | CRITICAL | run_all.py | 594-606 | Subprocess stdout/stderr not captured — pipeline errors invisible |
| 4 | CRITICAL | commodity_macro_pipeline.py | 15 | Import-time `KeyError` crashes before run_all.py env check |
| 5 | CRITICAL | futures_pipeline.py | 32 | Global `warnings.filterwarnings("ignore")` suppresses all warnings |
| 6 | HIGH | price_history_pipeline.py | 37-39 | Hardcoded `[2]` table index for Wikipedia scraping |
| 7 | HIGH | news_sentiment_pipeline.py | 93-96 | f-string JSON construction — headlines with `"` break Claude prompts |
| 8 | HIGH | synthetic_options_pipeline.py | 176-181 | `_latest_concat` loads ALL historical parquet files into memory |
| 9 | HIGH | run_all.py | 731-739 | Stage failures don't block dependent stages |
| 10 | HIGH | sector_etf_pipeline.py | 153 | Zero-data result returns PASS instead of FAIL |
| 11 | HIGH | query.py | 227-235 | Global DuckDB connection is not thread-safe |
| 12 | HIGH | ais_pipeline.py | 225 | Row-wise `df.apply()` is extremely slow for position data |
| 13 | MEDIUM | coingecko_pipeline.py | 84-100 | Mutable global rate-limit state persists across calls |
| 14 | MEDIUM | validate.py | 796-819 | Row-count check reads only latest file, not full table |
| 15 | MEDIUM | validate.py | 900-906 | `validate_table` reads latest file by filename sort, not by date |
| 16 | MEDIUM | run_all.py | 778 | Exit code ignores validation warnings |
| 17 | MEDIUM | schwab_options_pipeline.py | 207 | Filename always says "incremental" regardless of data scope |
| 18 | MEDIUM | storage_utils.py | 23-32 | No atomic writes — killed processes leave corrupt files |
| 19 | LOW | query.py | 343 | `LIMIT` clause is string-interpolated, not parameterized |
| 20 | LOW | reddit_pipeline.py | 80-82 | Ticker regex can match common English words (mitigated by watchlist) |
| 21 | LOW | pipeline_logging/run_tracker.py | 304 | `import subprocess` at module level creates unnecessary dependency |
| 22 | LOW | validate.py | 857 | `_check_value_ranges` treats NaN as in-range (silent pass) |

---

## Detailed Findings

### 1. CRITICAL — `load_news()` cannot find Hive-partitioned parquet files

**File:** `news_sentiment_pipeline.py:131-142`
**Impact:** The news sentiment pipeline will never find any finnhub news data after the first run, because `finnhub_pipeline.py` writes to `storage/raw/finnhub/news/year=YYYY/month=MM/` via `write_partitioned()`, but `load_news()` uses `os.listdir()` which only sees directory names like `year=2025`, not the `.parquet` files nested inside them.

```python
# BUG: os.listdir only sees top-level entries: ['year=2025']
# After write_partitioned, actual files are at: year=2025/month=07/news_*.parquet
files = [
    os.path.join(news_dir, f)
    for f in os.listdir(news_dir)       # <-- sees 'year=2025', not .parquet
    if f.endswith(".parquet")           # <-- filters out everything
]
```

**Contrast:** The `load_already_scored()` function on line 159 correctly uses `_glob_mod.glob(..., recursive=True)` — the same approach should be used here.

**Fix:** Replace `os.listdir` with recursive glob:
```python
import glob as _glob_mod
files = _glob_mod.glob(os.path.join(news_dir, "**", "*.parquet"), recursive=True)
```

---

### 2. CRITICAL — Hardcoded 4% rate fallback silently misprices options

**File:** `synthetic_options_pipeline.py:356`
**Impact:** When the treasury yield curve cannot be loaded (missing macro parquet, Yahoo API failure), every contract is priced with a 4% risk-free rate. In a 4-5% rate environment this is tolerable, but rates have been as low as 0.25% (2020-2022). Using 4% instead of 0.25% would:
- Misprice deep OTM puts by 30-50%
- Produce delta/gamma estimates with the wrong curvature
- Make the entire `synthetic_options` table unreliable for backtesting

There is no warning logged when the fallback is used — the pipeline reports `PASS`.

```python
base["r"] = base["r"].fillna(0.04)  # last-resort flat fallback
# ^ No log.warning() when this actually fills data
```

**Fix:** Log a warning when the fallback activates, and set `r` to the most recent available rate (from the latest date in the curve) rather than a hardcoded constant:
```python
fallback_rate = base["r"].ffill().bfill().mean()  # or latest available
n_filled = base["r"].isna().sum()
if n_filled > 0:
    log.warning(f"Rate fallback: filling {n_filled} rows with {fallback_rate:.4f}")
base["r"] = base["r"].fillna(fallback_rate)
```

---

### 3. CRITICAL — Subprocess output is completely invisible

**File:** `run_all.py:594-606`
**Impact:** `subprocess.run()` is called without `capture_output=True` or `stdout`/`stderr` redirection. When a pipeline fails, the run summary shows only `"exit {returncode}"` — no tracebacks, no error messages, no validation output. The operator has no way to diagnose the failure without manually finding and reading log files.

```python
result = subprocess.run(cmd, timeout=spec.timeout,
                        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
# stdout/stderr go to terminal but are not captured for the summary
```

**Fix:** Capture output and include it in the RunResult:
```python
result = subprocess.run(cmd, timeout=spec.timeout, capture_output=True, text=True,
                        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
note = result.stdout[-500:] if result.returncode != 0 else ""
```

---

### 4. CRITICAL — Import-time KeyError crashes before env validation

**File:** `commodity_macro_pipeline.py:15`
**Impact:** `FRED_API_KEY = os.environ["FRED_API_KEY"]` crashes with an unhelpful `KeyError` at import time. When `run_all.py` imports this module (or even checks `_check_env()`), the crash happens before the env-var check can produce a clean SKIP message. All other well-behaved pipelines use `os.environ.get()` or check inside `main()`.

**Fix:** Use `os.environ.get()` and check inside `main()`:
```python
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
# ... in main():
if not FRED_API_KEY:
    log.critical("FRED_API_KEY not set")
    return
```

---

### 5. CRITICAL — Global warning suppression hides data quality issues

**File:** `futures_pipeline.py:32`
**Impact:** `warnings.filterwarnings("ignore")` at module scope suppresses ALL Python warnings for the entire process. This includes:
- `pandas.errors.SettingWithCopyWarning` — could indicate unintended data mutation
- `numpy.RuntimeWarning` — division by zero, overflow in pricing
- `FutureWarning` — deprecated API usage that will break on upgrade

This is a process-wide side effect that persists even after the pipeline finishes.

**Fix:** Use a context manager or filter only specific warnings:
```python
import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")
```

---

### 6. HIGH — Wikipedia table scraping uses hardcoded index

**File:** `price_history_pipeline.py:37-39`
**Impact:** `pd.read_html(...)[2]` assumes the DJI components table is always at index 2. Wikipedia page structure changes over time (ads, new sections, layout edits). When this breaks, the pipeline silently uses `FALLBACK_SYMBOLS` (line 45) — a static list that may be stale.

The `finnhub_pipeline.py` (line 70-92) correctly searches for the right table by column name. `price_history_pipeline.py` should do the same.

**Fix:** Port the table-search logic from `finnhub_pipeline.py`:
```python
tables = pd.read_html(...)
for df in tables:
    col = next((c for c in df.columns if str(c).strip().lower() in ("symbol", "ticker")), None)
    if col is not None and 25 <= len(df) <= 35:
        symbols = df[col].tolist()
        return symbols
```

---

### 7. HIGH — Headline injection breaks Claude JSON parsing

**File:** `news_sentiment_pipeline.py:93-96`
**Impact:** Article headlines are interpolated into JSON using f-strings:
```python
f'"headline": "{item["headline"]}", "summary": "{summary}"'
```
If a headline contains `"` (common in financial news: `"Apple beats Q4 estimates"`), the JSON becomes malformed. Claude may:
- Return an error or empty response
- Parse incorrectly and misclassify articles
- Silently drop the batch

The `summary` is also truncated at 300 chars (line 92) but not escaped, so a `"` at position 300 would also break parsing.

**Fix:** Use `json.dumps()` for proper escaping:
```python
article = json.dumps({
    "id": item["id"],
    "symbol": item["symbol"],
    "headline": item["headline"],
    "summary": summary,
})
lines.append(article)
```

---

### 8. HIGH — Memory explosion on backfill

**File:** `synthetic_options_pipeline.py:176-181`
**Impact:** `_latest_concat()` loads ALL parquet files matching a glob pattern into memory:
```python
frames = [pd.read_parquet(f) for f in files]
return pd.concat(frames, ignore_index=True)
```
For `prices_*.parquet` after months of daily runs, this could be hundreds of files × 30 symbols × ~500 rows = millions of rows loaded into a single DataFrame. On a 16GB machine, this would OOM.

**Fix:** For incremental runs, only load the most recent file (or use DuckDB's view layer instead of pandas):
```python
files = sorted(glob.glob(pattern))
if not files:
    return None
if len(files) > 10:
    # Use DuckDB for efficient filtering instead of loading everything
    return pd.read_parquet(files[-1])  # latest snapshot only
```

---

### 9. HIGH — Stage failures don't block dependent stages

**File:** `run_all.py:731-739`
**Impact:** When Stage 1 pipelines fail, the runner prints a warning but still executes Stage 2/3 pipelines. This means:
- `synthetic_options` (Stage 3) runs with stale prices data from a failed `prices` pipeline
- `news_sentiment` (Stage 3) runs with stale finnhub_news from a failed `finnhub` pipeline
- `alpha_vantage` (Stage 3) runs with potentially stale data

The dependent pipelines will `PASS` (they produced output) but the output is based on stale inputs — a silent data quality failure.

**Fix:** Track stage failures and skip dependent pipelines:
```python
if spec.stage > current_stage and stage_failures.get(current_stage, 0) > 0:
    # Skip this pipeline
    result = RunResult(spec.name, "SKIP", 0.0, f"Stage {current_stage} had failures")
```

---

### 10. HIGH — Zero-data result returns PASS

**File:** `sector_etf_pipeline.py:153`
**Impact:** When zero ETFs are collected (all fail), the pipeline reports PASS:
```python
if not frames:
    log.warning("No data collected. Exiting.")
    log.pipeline_end(status="PASS")  # <-- should be FAIL
    return
```
This same pattern appears in `price_history_pipeline.py:141`. The run_all.py summary shows these as successes, hiding a complete data collection failure.

**Fix:** Change to `status="FAIL"` or `status="NO DATA"`.

---

### 11. HIGH — Global DuckDB connection is not thread-safe

**File:** `query.py:227-235`
**Impact:** The global `_CON` DuckDB connection has no thread-safety guards. If `query.py` is used from a web server, Jupyter notebook with multiprocessing, or any concurrent context, two threads could execute queries simultaneously on the same connection, causing data corruption or crashes.

**Fix:** Use a threading lock, or create per-thread connections:
```python
import threading
_con_lock = threading.Lock()

def _con():
    global _CON
    with _con_lock:
        if _CON is None:
            _CON = duckdb.connect()
            _register_views(_CON)
        return _CON
```

---

### 12. HIGH — Row-wise `apply()` is O(n) Python calls

**File:** `ais_pipeline.py:225`
**Impact:** `df.apply(_enrich, axis=1)` calls a Python function for every row. For 10,000+ position reports (common in a 10-minute window), this takes 5-10 seconds when a vectorized `merge()` would take <100ms.

**Fix:** Pre-build a metadata DataFrame and merge:
```python
meta_df = pd.DataFrame.from_dict(self.vessel_meta, orient='index')
meta_df.index.name = 'mmsi'
df = df.merge(meta_df, left_on='mmsi', right_index=True, how='left')
```

---

### 13. MEDIUM — Mutable global rate-limit state

**File:** `coingecko_pipeline.py:84-100`
**Impact:** `_rate_limit_hits` and `_current_interval` are module-level globals. If the pipeline is called multiple times in a session (tests, re-runs), the rate limit interval keeps increasing and never resets, eventually stalling execution.

**Fix:** Reset state at pipeline start:
```python
def main():
    global _rate_limit_hits, _current_interval
    _rate_limit_hits = 0
    _current_interval = None
    ...
```

---

### 14. MEDIUM — Row-count check compares snapshot, not full table

**File:** `validate.py:796-819`
**Impact:** `_check_row_count()` reads only the most recent file (`existing[-1]`) to compare against the new DataFrame. In a Hive-partitioned store, the latest file might be a small daily increment (1 row per symbol) while the full table has years of data. The comparison is meaningless — a pipeline writing 30 rows would appear as a "data loss" compared to a full table.

**Fix:** Compare against the cumulative row count of all existing files, or compare the new file's row count against the same file from the previous day.

---

### 15. MEDIUM — File sort is alphabetical, not chronological

**File:** `validate.py:900-901`
**Impact:** `sorted(_glob_mod.glob(...))` sorts filenames alphabetically. Since filenames include dates like `20250727`, this happens to work. But if naming conventions change (e.g., switching to ISO dates), the "latest file" would be wrong, causing validation to check stale data.

**Fix:** Sort by file modification time:
```python
files = sorted(_glob_mod.glob(...), key=os.path.getmtime)
```

---

### 16. MEDIUM — Exit code ignores validation warnings

**File:** `run_all.py:778`
**Impact:** The exit code only checks for PASS/SKIP/DRY_RUN status, ignoring `val_warnings`. A pipeline can produce data with critical validation warnings (50%+ null rates, future dates, out-of-range values) and the run still returns success.

**Fix:** Include validation warnings in the exit code decision:
```python
total_warnings = sum(r.val_warnings for r in results)
return 0 if all(r.status in ("PASS", "SKIP", "DRY RUN") for r in results) and total_warnings == 0 else 1
```

---

### 17. MEDIUM — Filename always says "incremental"

**File:** `schwab_options_pipeline.py:207`
**Impact:** The output filename is hardcoded to `"schwab_options_incremental_{today_str}.parquet"` regardless of the actual data scope. When run_all.py runs with `--backfill`, the filename still says "incremental", making it impossible to distinguish backfill from incremental data by filename alone.

**Fix:** Accept a `--backfill` argument and use it in the filename:
```python
mode_tag = "backfill" if args.backfill else "incremental"
filename = f"schwab_options_{mode_tag}_{today_str}.parquet"
```

---

### 18. MEDIUM — Non-atomic Parquet writes

**File:** `storage_utils.py:38-44`
**Impact:** `df.to_parquet()` is not atomic. If the process is killed (SIGKILL, power loss, OOM) mid-write, the file is left in a corrupt state. DuckDB's `read_parquet()` will fail on the corrupt file, causing the entire table's view to fail to register.

The cleanup on exception (lines 41-43) only handles Python exceptions, not OS-level kills.

**Fix:** Write to a temporary file first, then rename (atomic on most filesystems):
```python
temp_path = filepath + ".tmp"
df.to_parquet(temp_path, index=False, compression="snappy")
os.replace(temp_path, filepath)  # atomic on POSIX/NTFS
```

---

### 19. LOW — LIMIT clause is string-interpolated

**File:** `query.py:343`
**Impact:** `f"LIMIT {limit}"` directly interpolates the `limit` parameter. While it's typed as `int | None`, Python won't enforce this at runtime. If a string were passed, it would be a SQL injection vector. The `load()` function is documented as public API, so external callers could trigger this.

**Fix:** Validate limit is an integer:
```python
if limit is not None:
    if not isinstance(limit, int) or limit < 0:
        raise ValueError(f"limit must be a non-negative integer, got {limit!r}")
    limit_clause = f"LIMIT {limit}"
```

---

### 20. LOW — Ticker regex matches common English words

**File:** `reddit_pipeline.py:80-82`
**Impact:** The regex matches 2-5 uppercase letter words, then filters against the watchlist. While the watchlist mitigates false positives, words like "CEO", "IPO" (not in watchlist) are correctly filtered out, but legitimate 3-letter words like "GAS", "OIL" that appear frequently in finance subreddits would match if added to the watchlist in the future.

**Mitigation:** Current watchlist is conservative. No action needed unless the watchlist expands to include common English words.

---

### 21. LOW — Unnecessary subprocess import at module level

**File:** `pipeline_logging/run_tracker.py:366`
**Impact:** `import subprocess` is placed at the bottom of the file solely for `subprocess.TimeoutExpired` (line 304). This creates an unnecessary module-level dependency. The import should be at the top of the file or the exception type should be referenced differently.

**Fix:** Move `import subprocess` to the top of the file, or use `TimeoutError` (a built-in) as a fallback.

---

### 22. LOW — NaN passes value range checks silently

**File:** `validate.py:784-785`
**Impact:** In `_check_value_ranges()`:
```python
numeric = pd.to_numeric(df[col], errors="coerce")
out_of_range = int(((numeric < lo) | (numeric > hi)).sum())
```
When `numeric` is NaN (from `errors="coerce"`), `NaN < lo` and `NaN > hi` both return False. So NaN values silently pass range checks. If a column is supposed to be strictly positive but is full of NaN, no warning is raised.

**Fix:** Add a NaN check alongside range checks:
```python
nan_count = int(numeric.isna().sum())
if nan_count > 0:
    results.append(CheckResult(f"range:{col}", Severity.WARNING,
                               f"{nan_count} values are NaN (not numeric)"))
```

---

## Design Smells

### DS-1 — Duplicate code across pipelines

The following patterns are copy-pasted across 10+ pipelines with minor variations:
- `get_with_backoff()` — identical retry/backoff logic in finnhub, commodity_macro, eia, coingecko
- `get_dji_symbols()` — Wikipedia scraping in finnhub and price_history (different quality)
- `compute_derived_columns()` — identical in price_history and sector_etf
- VWAP calculation — duplicated in price_history and sector_etf

**Recommendation:** Extract shared HTTP helpers into a `http_utils.py` module. Extract Wikipedia scraping into a `dji_utils.py` module.

### DS-2 — Inconsistent error handling patterns

Some pipelines call `log.pipeline_end(status="FAIL")` and `raise`, others call `log.pipeline_end(status="FAIL")` and `return`, and some never call `pipeline_end` at all (e.g., `reddit_pipeline.py:200-203` on missing env vars). The runner can't reliably detect pipeline failures.

### DS-3 — No idempotency guarantees

Running a pipeline twice on the same day produces duplicate files in the same Hive partition. DuckDB's `union_by_name=True` handles this at query time (duplicates are returned), but they inflate row counts and can cause incorrect aggregations if a `GROUP BY` or `DISTINCT` isn't used.

**Recommendation:** Check for existing files before writing, or use upsert logic (delete+rewrite for the same date).

### DS-4 — Mixed abstraction levels in validate.py

`validate.py` imports `query.py` to access `CATALOG`, creating a circular-like dependency (validate → query → storage → validate). The validation schema is tightly coupled to the CATALOG keys.

---

## EXPANSION PLAN

### 1. Missing Data Sources

| Source | Signal Value | API/Method |
|--------|-------------|------------|
| **Options Flow (Unusual Activity)** | Detect institutional positioning before price moves | CBOE, Barchart, or Trade Alert API |
| **ETF Flows** | Money flow into/out of sectors as rotation signal | ETF.com or IAFI API |
| **CDS Spreads** | Credit risk pricing for individual names and sovereigns | CMA/Markit via Bloomberg or FRED |
| **Dark Pool Volume** | Institutional hidden order flow | FINRA ATS data (public, weekly) |
| **Semiconductor Supply Chain** | Leading indicator for tech earnings | TSMC monthly revenue, ASML orders |
| **Shipping Freight Rates** | Baltic Dry Index, container rates | Baltic Exchange, Freightos |
| **Housing Market** | Zillow Home Value Index, building permits | Zillow API, Census Bureau |
| **Credit Card Spending** | Real-time consumer health proxy | Bank earnings calls, Facteus |
| **Insider Transaction Sentiment** | Clustered insider buying as contrarian signal | SEC Form 4 via EDGAR (expand finnhub_events) |
| **Municipal Bond Market** | Stress signal for local government finances | MSRB EMMA data |
| **Leveraged Loan Market** | Credit stress indicator | LCD/S&P Leverage Loan Index |
| **Twitter/X Finance Sentiment** | Real-time retail sentiment | Academic Twitter API or stocktwits |

### 2. Cross-Pipeline Analytics

- **Macro Factor Model:** Multi-factor regression of sector returns against macro variables (rates, credit spreads, oil, USD). Build a live "what's driving the market" decomposition.
- **Correlation Regime Detection:** Rolling cross-asset correlation matrix. Alert when correlations spike (risk-on/risk-off regime shift).
- **Options-Implied Macro View:** Use the volatility surface from synthetic_options + schwab_options to extract market-implied expectations for earnings and rates.
- **Supply Chain Disruption Composite:** Combine AIS vessel counts, freight rates, port congestion, and semiconductor supply into a single leading indicator.
- **Geopolitical Risk Scoring:** Combine congressional trades, defense stock moves, oil supply chokepoints (AIS), and news sentiment into a geopolitical risk index.
- **Retail vs Institutional Divergence:** Compare Reddit/Google Trends sentiment (retail) with insider trades, 13F holdings, and dark pool volume (institutional). Divergence signals potential reversals.
- **Yield Curve Health Monitor:** Beyond simple 10Y-2Y spread, track the full curve shape (2s10s, 3m10y, forward rates) and classify curve regimes.

### 3. Monitoring & Alerting Gaps

- **Pipeline Failure Alerts:** No Slack/email/SMS notification on pipeline failures. Currently only logs to file. Add a `pipeline_logging/alerts.py` module with configurable channels.
- **Data Freshness Dashboard:** No automated check for stale data. A table should be flagged if `MAX(fetched_at)` is older than expected (e.g., prices should be updated daily, not monthly).
- **API Quota Tracking:** No tracking of API usage against quotas. Finnhub (60 req/min), EIA (suspends keys on abuse), Alpha Vantage (25 calls/day free tier). Add per-pipeline quota counters.
- **Cost Tracking:** Claude API calls in news_sentiment cost money. Add token counting and cost estimation to the pipeline log.
- **Storage Size Monitoring:** No monitoring of Parquet store growth. Over years, `storage/raw/` could grow to terabytes. Add cleanup policies for old partitions.
- **Anomaly Detection:** No automated detection of data quality anomalies (sudden drops, impossible values, schema drift). The validation layer is static — add statistical process control.

### 4. Test Coverage Gaps

| Area | Current Coverage | Gap |
|------|-----------------|-----|
| `pricing_models.py` | None visible | Need unit tests for BSM, BS2002, implied_vol, interp_rate. Property tests: no-arbitrage bounds (0 < price < S for calls), put-call parity, Greeks signs. |
| `query.py` round-trip | Not tested | Need tests that write via `write_partitioned`, register views, and verify `load()` returns correct data. |
| Rate limiter correctness | Not tested | Need tests that verify Finnhub RateLimiter enforces minimum interval. |
| Wikipedia scraping | Not tested | Need tests for both finnhub and price_history symbol extraction, including mock Wikipedia HTML. |
| Hive partitioning | Not tested | Need tests that verify data written to `year=YYYY/month=MM/` is correctly read by DuckDB with `hive_partitioning=True`. |
| Backward compatibility | Not tested | Need tests that verify new pipeline additions don't break existing CATALOG entries or SCHEMAS. |
| Memory usage under load | Not tested | Need load tests for `synthetic_options_pipeline._latest_concat()` with 100+ parquet files. |

### 5. Architecture Improvements

- **Centralize HTTP helpers:** Extract retry/backoff logic into `http_utils.py` with consistent logging, timeout defaults, and rate limiting.
- **Pipeline base class:** Create a `BasePipeline` class with standard lifecycle (start → fetch → validate → write → end), reducing boilerplate across 45+ pipeline scripts.
- **Configuration-driven pipelines:** Move pipeline registry from hardcoded `PIPELINES` list in `run_all.py` to a YAML/JSON config file, making it easier to add new pipelines without editing Python code.
- **Idempotent writes:** Add date-aware deduplication to `write_partitioned()` — if data for the same date already exists in the partition, replace it rather than duplicating.
- **Streaming validation:** Run `validate_df()` in real-time during data collection (not just post-hoc), so corrupted API responses are caught before writing to disk.
