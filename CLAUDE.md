# CLAUDE.md — financial-data-pipeline

Free/public-source financial data pipelines → partitioned Parquet → DuckDB query layer →
analytics/factor signals → event backtesting. Owner: Zander (GitHub `Zanderl1987`, private,
default branch `master`).

**148 PASS / 80 NO DATA CATALOG tables, 454 tests passing** as of 2026-07-26. Verify with the
commands below rather than trusting this line if it looks stale.

## Environment

- Python: `C:\ProgramData\anaconda3\python.exe` — ALWAYS use this full path. Bare `python`
  on this machine is a broken MS Store stub.
- Run everything from the repo root (`C:\Users\zande\PycharmProjects\financial-data-pipeline`).
- Secrets in `.env` at repo root (gitignored). Never commit it, never print key values.
  `anthropic` installed (user site) 2026-07-06 — only `fed_sentiment_pipeline.py` still
  needs ANTHROPIC_API_KEY (news sentiment is local VADER, no key).
- (The old warning about `C:\Users\zande` being an accidental git repo is resolved —
  the vestigial empty `.git` was removed 2026-07-11.)

## Commands

```
C:\ProgramData\anaconda3\python.exe -m pytest tests/ -v      # test suite
C:\ProgramData\anaconda3\python.exe run_all.py --dry-run     # show pipeline plan
C:\ProgramData\anaconda3\python.exe run_all.py               # run all (3 dependency stages)
C:\ProgramData\anaconda3\python.exe validate.py              # data health check
C:\ProgramData\anaconda3\python.exe curated.py               # rebuild deduped snapshots
C:\ProgramData\anaconda3\python.exe evaluate.py --adapter sentiment   # unified eval framework (see docs/EVALUATION.md)
C:\ProgramData\anaconda3\python.exe generate_eval_report.py --latest <name>   # HTML report from eval artifacts
```

## Architecture

```
*_pipeline.py (root, one per source)
  └─ storage_utils.write_partitioned() → storage/raw/<category>/year=YYYY/month=MM/*.parquet
       └─ curated.py dedup → storage/curated/<table>/<table>.parquet
            └─ query.py  (DuckDB CATALOG; q.load()/q.sql(); PREFERS curated)
                 └─ analytics/  (features.py PIT panel, signals.py z-scored factors, technical.py TA + tv_rating)
                      └─ backtest.py (quantile portfolios) / event_backtest.py (event studies)
                           └─ evaluation/ + evaluate.py (unified eval framework: 3-tier significance battery, append-only registry — docs/EVALUATION.md)
                           └─ signal_monitor.py (maintained signal-health table, DEGRADED flags)
```

`run_all.py` auto-rebuilds curated after each run. **If you run a pipeline directly, run
`curated.py` afterward** or analytics reads stale data.

## Logging (`logging_utils.py`, added 2026-07-26)

`get_logger(name)` returns a logger writing to console AND a rotating file at
`storage/logs/<name>.log` (5MB x 5 backups, gitignored) — idempotent, safe to call at
module import time. `run_all.py` uses it: every pipeline subprocess's stdout+stderr is
captured, printed (buffered per-pipeline rather than streamed live — a deliberate
tradeoff to guarantee a persisted log even for silent/hung runs), and on FAIL/timeout
written to `storage/logs/failures/<name>_<timestamp>.log`, with that path appended to
the `RunResult.note` so the console summary tells you exactly where to look instead of
just "exit 1". Individual pipeline `*.py` files have NOT been retrofitted to use this
(still use `print()`/their own `logging.basicConfig()`) — this covers the orchestrator
level first; adopt it in a pipeline directly if you want persisted logs from a manual
(non-`run_all.py`) run. Tests: `tests/test_logging.py`.

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

- `EXPERT_BRIEF.md` — the judgment layer: prioritized roadmap with reasoning, strategic
  traps, cross-repo synergy. **Read it before planning any substantial work here.**
- `PROJECT_NOTES.md` — living project-state reference, updated in place (not
  append-only): active automations, verified hard API constraints/quotas, cross-repo
  dependencies. Check this before assuming a constraint is still true.
- `CLAUDE_SESSION_NOTES.md` + `SESSION_NOTES_2026-07-03.md` — running session log with
  per-pipeline API quirks (SimFin v3 codes, EDGAR 13F parsing, Pink Sheet URL rotation…).
- `docs/` — source research notes. `FinancialDataPipeline_Future_Improvements.md` — roadmap.
- `SHORT_INTEREST_SOURCES.md` — short-interest source comparison.

## Open work (as of 2026-07-06)

- `short_pressure` factor ACTIVE (2026-07-06): FINRA's biweekly CDN path 403s everything —
  the dataset moved behind the FINRA Query API, which needs registered client credentials
  from developer.finra.org (the 20-char FINRA_API_KEY in .env does NOT work for OAuth).
  `analytics/features.py` now falls back to the yfinance `short_interest` snapshot table
  (same biweekly filing, watchlist-only coverage) — keep running the pipeline daily so
  filing dates accumulate. Registering FINRA API credentials would restore full-market data.
- `sentiment` factor ACTIVE (2026-07-06): `news_sentiment_pipeline.py` rewritten to local
  VADER + finance lexicon (no ANTHROPIC_API_KEY needed; deterministic, free). Backfilled
  1,235 articles. `fed_sentiment_pipeline.py` still requires ANTHROPIC_API_KEY (Claude API).
- Sentiment evaluation IN PROGRESS (2026-07-06 session 2, uncommitted): `sentiment_eval.py`
  (PIT-safe IC/spread harness) + `finnhub_pipeline.py --news-days N` (deep news backfill,
  5-day chunks). 365-day news pull was running when notes were written — see
  SESSION_NOTES_2026-07-06.md session 2 for the remaining steps (score → curated → eval
  baseline → then decide FinBERT vs lexicon tuning).
- Deep backfills pending: FDIC financials (1992+), Fed SOMA (~2002+, slow), Schwab full
  price history (deferred until storage sized).
- Daily accumulators: `tradingview_pipeline.py`, `schwab_movers_pipeline.py` (snapshot-only
  sources — history only exists if run daily; both in run_all.py).
- Earnings event studies blocked: `earnings_calendar` holds ±6 weeks only; needs historical
  earnings + matching price backfills.
- `analytics/options.py` REPAIRED (2026-07-16): `put_call_ratio` now volume-based from
  `options_history`; `iv_summary` sources `schwab_options` → `options_chain` with column
  normalizer. Both return empty gracefully when no data. `iv_summary` will return results
  once Schwab OAuth lands chain data with `implied_volatility`. See
  SESSION_NOTES_2026-07-12.md (design) and SESSION_NOTES_2026-07-16.md (implementation).
