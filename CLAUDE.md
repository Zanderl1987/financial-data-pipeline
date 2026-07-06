# CLAUDE.md — financial-data-pipeline

Free/public-source financial data pipelines → partitioned Parquet → DuckDB query layer →
analytics/factor signals → event backtesting. Owner: Zander (GitHub `Zanderl1987`, private,
default branch `master`).

**133 CATALOG tables, 234 tests passing (5 skipped)** as of 2026-07-06. Verify with the
commands below rather than trusting this line if it looks stale.

## Environment

- Python: `C:\ProgramData\anaconda3\python.exe` — ALWAYS use this full path. Bare `python`
  on this machine is a broken MS Store stub.
- Run everything from the repo root (`C:\Users\zande\PycharmProjects\financial-data-pipeline`).
- Secrets in `.env` at repo root (gitignored). Never commit it, never print key values.
  `anthropic` is NOT installed in the conda env → sentiment pipelines skip gracefully.
- ⚠️ `C:\Users\zande` (home dir) is itself an accidental git repo — never commit there.

## Commands

```
C:\ProgramData\anaconda3\python.exe -m pytest tests/ -v      # test suite
C:\ProgramData\anaconda3\python.exe run_all.py --dry-run     # show pipeline plan
C:\ProgramData\anaconda3\python.exe run_all.py               # run all (3 dependency stages)
C:\ProgramData\anaconda3\python.exe validate.py              # data health check
C:\ProgramData\anaconda3\python.exe curated.py               # rebuild deduped snapshots
```

## Architecture

```
*_pipeline.py (root, one per source)
  └─ storage_utils.write_partitioned() → storage/raw/<category>/year=YYYY/month=MM/*.parquet
       └─ curated.py dedup → storage/curated/<table>/<table>.parquet
            └─ query.py  (DuckDB CATALOG; q.load()/q.sql(); PREFERS curated)
                 └─ analytics/  (features.py PIT panel, signals.py z-scored factors, technical.py TA + tv_rating)
                      └─ backtest.py (quantile portfolios) / event_backtest.py (event studies)
                           └─ signal_monitor.py (maintained signal-health table, DEGRADED flags)
```

`run_all.py` auto-rebuilds curated after each run. **If you run a pipeline directly, run
`curated.py` afterward** or analytics reads stale data.

## Adding a new pipeline — wiring checklist

1. Standalone `<name>_pipeline.py` at repo root. Free/public sources only.
2. Docstring header (Outputs + Usage), argparse CLI with `--backfill`, rate-limiter +
   retry/backoff on 429.
3. Lowercase snake_case columns + `fetched_at` ISO timestamp; write via
   `storage_utils.write_partitioned()` (snappy).
4. Wire ALL of: `query.py` CATALOG (use filename-prefix globs, e.g.
   `dir/**/name_*.parquet`, to avoid glob collisions — guard test exists),
   `validate.py` SCHEMAS, `run_all.py` PipelineSpec (stage, requires_env, timeout),
   `curated.py` KEYS (natural dedup key), `tests/test_catalog.py` EXPECTED_TABLES,
   `tests/test_pipelines.py` PIPELINE_MODULES. Create the storage dir.
5. Verify live with an incremental sample run, then `validate.py`, then full test suite.

## Hard-won gotchas (violating these has caused real data corruption)

- **Never name a DataFrame column `year` or `month`.** Hive partitioning exposes these as
  virtual columns and silently overwrites your data with the *fetch* date. Use `obs_year`
  or bake a `date` column. (Corrupted fhfa_hpi and fao_* before being caught.)
- **ASCII-only CLI output.** Windows cp1252 terminal crashes on ═ ▶ ✓ etc. Use `= >> + ! X`.
- **Raw store ≠ deduped.** Incremental pipelines re-fetch overlapping windows; raw globs
  were once 42% duplicate rows. Always analyze via `query.py` (curated), never raw globs.
- **Schwab**: `schwabdev` 3.0.4 uses `tokens_db=` (SQLite `tokens.db`), not `tokens_file=`.
  OAuth is interactive — Zander must run it in a real terminal (auth code expires ~30s).
  App has Market Data API only; Trader API (positions/transactions) 401s until enabled at
  developer.schwab.com. Schwab has NO historical options — chains are snapshot-only.
- **`validate.py` printed row counts** spot-check only the alphabetically-latest raw file —
  they look wrong right after a backfill. Trust `curated.py` counts.
- **event_backtest.load_close()** keeps the LONGEST series across price tables so shallow
  watchlist pulls don't shadow deep history — preserve that invariant.
- Alpha Vantage: 25 req/day/key. BLS: daily quota. SEC EDGAR: ≤10 req/s + EDGAR_USER_AGENT.

## Known-broken / dead ends (don't re-attempt without a new angle)

- `nasdaq_data_link_pipeline.py` — Incapsula WAF 403s everything; needs a replacement source.
- USDA_NASS keys in .env return 401 (need fresh key); CENSUS_API_KEY not set.
- Baker Hughes rig count (JS SPA), AAR rail traffic (member-gated), Stooq (JS proof-of-work),
  Motley Fool transcripts (ToS prohibits scraping) — all ruled out.
- IEX_CLOUD_API_KEY is dead (service shut down 2025).

## Where deeper knowledge lives

- `CLAUDE_SESSION_NOTES.md` + `SESSION_NOTES_2026-07-03.md` — running session log with
  per-pipeline API quirks (SimFin v3 codes, EDGAR 13F parsing, Pink Sheet URL rotation…).
- `docs/` — source research notes. `FinancialDataPipeline_Future_Improvements.md` — roadmap.
- `SHORT_INTEREST_SOURCES.md` — short-interest source comparison.

## Open work (as of 2026-07-06)

- Activate dormant factors: run `short_interest_pipeline.py --source finra` (fills
  `finra_short_interest`) and the finnhub → `news_sentiment_pipeline.py` chain (needs
  ANTHROPIC_API_KEY) so `short_pressure` and `sentiment` factors produce values.
- Deep backfills pending: FDIC financials (1992+), Fed SOMA (~2002+, slow), Schwab full
  price history (deferred until storage sized).
- Daily accumulators: `tradingview_pipeline.py`, `schwab_movers_pipeline.py` (snapshot-only
  sources — history only exists if run daily; both in run_all.py).
- Earnings event studies blocked: `earnings_calendar` holds ±6 weeks only; needs historical
  earnings + matching price backfills.
