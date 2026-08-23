# CLAUDE.md — financial-data-pipeline

Free/public-source financial data pipelines → partitioned Parquet → DuckDB query layer →
analytics/factor signals → event backtesting. Owner: Zander (GitHub `Zanderl1987`, private,
default branch `master`).

**148 PASS / 80 NO DATA CATALOG tables, 466 tests passing** as of 2026-07-26. Verify with the
commands below rather than trusting this line if it looks stale.


## Session notes and task list live in a separate repo

Session notes and the task list for this project are NOT in this repo. They live in the
private `work-notes` repo, cloned as a sibling, at `work-notes/financial-data-pipeline/`:

    C:\Users\zande\PycharmProjects\work-notes\financial-data-pipeline\

When Zander asks to update session notes or the task list, edit the files there, not here.
This repo keeps only durable documentation (this file, `docs/`), so it can be public
without a visitor scrolling through a working log. See `work-notes/CLAUDE.md` for the
convention.

## Environment

- Python: `C:\ProgramData\anaconda3\python.exe` — ALWAYS use this full path. Bare `python`
  on this machine is a broken MS Store stub.
- Run everything from the repo root (`C:\Users\zande\PycharmProjects\financial-data-pipeline`).
- Secrets in `.env` at repo root (gitignored). Never commit it, never print key values.
  `anthropic` installed (user site) 2026-07-06 — only `fed_sentiment_pipeline.py` still
  needs ANTHROPIC_API_KEY (news sentiment is local VADER, no key).
- (The old warning about `C:\Users\zande` being an accidental git repo is resolved —
  the vestigial empty `.git` was removed 2026-07-11.)
- **This repo gets worked on from multiple sessions/devices.** `git status` only shows
  "behind" once you've fetched — run `git fetch origin` at the start of any session before
  trusting ahead/behind counts, or you can push into a rejection (or worse, stay unaware
  origin has moved). Happened 2026-07-30: a session pushed 2 commits (ETF holdings
  pipeline) while this clone was mid-session; the next session's `git status` looked like
  a clean "ahead 1" until `git push` was rejected. Merged fine (no file overlap) once
  caught — see work-notes/financial-data-pipeline/SESSION_NOTES_2026-07-30.md.

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

## Iceberg tables (`constituents/`, `shipping/`) — local, regenerable state

`storage/iceberg/` holds a few tables (fund_holdings, etf_holdings, securities,
index_members, identifier_map, shipping_gscpi, shipping_freight_ppi) via a local
PyIceberg SQL catalog (`storage/iceberg/constituents_catalog.db`, SQLite). As of
2026-07-31, the metadata (`**/metadata/*.json`, `*.avro`) AND the catalog DB itself
are gitignored, same as the parquet data — **the whole Iceberg warehouse is local,
regenerable state, not git-synced**, matching `storage/raw/`.

**Why**: a table's `location` is baked into its metadata at creation time as an
absolute path. This machine has two clones of this repo (`PycharmProjects\
financial-data-pipeline`, canonical, and a stale `C:\Users\zande\financial-data-
pipeline`, see docs/PROJECT_NOTES.md). `etf_holdings` was first created/populated from
the other clone, then its small metadata files got git-committed and merged into
this one — but the actual parquet data (gitignored, same as any raw table) never
followed. The result: this clone's catalog *looked* fully populated (readable
snapshot history, no errors) but every query against it silently returned zero
rows, because the metadata pointed at a directory that only exists on the other
machine/clone. Discovered + fixed 2026-07-31 (migrated the real 7,723-row dataset
into this clone, recreated the table here, then gitignored metadata everywhere so
this failure mode can't recur). If you ever see an Iceberg table with real-looking
snapshot history but `query.py`/`validate.py` reads zero rows, check
`table.location()` via pyiceberg against the clone you're actually in before
assuming the data is gone — see `work-notes/financial-data-pipeline/SESSION_NOTES_2026-07-30.md` session 2 for the
full diagnostic trail.

**Practical implications:**
- A fresh clone (or the other machine's clone) starts with NO Iceberg data.
  Table-creation scripts (`create_securities_table.py`, `create_shipping_tables.py`)
  plus a real pipeline run are required to repopulate locally.
- When writing to one of these tables per-partition in a loop (e.g. per
  `fund_ticker`), batch all `overwrite()`/`append()` calls into a single
  `with table.transaction() as txn:` block. One `table.overwrite()` per item
  in a loop creates one new `metadata.json` snapshot version PER ITEM per run —
  `fund_holdings` hit 467 metadata files this way after only a handful of runs
  before being fixed. See `fund_holdings_pipeline.py`/`etf_holdings_pipeline.py`'s
  `write_to_iceberg()` and `iceberg_utils.expire_old_snapshots()` (called after
  every write, retains 30 days of snapshot history — trims the logical snapshot
  log, though pyiceberg 0.11.1's Python client has no on-disk orphan-file GC yet,
  so it doesn't reclaim old data/manifest files already written).
- The Bash tool's working directory persists across tool calls in a session. A
  stray `cd` into the other clone (e.g. to check its git log) will silently
  redirect every subsequent *relative*-path command to the wrong repo. Use
  absolute paths for any command touching Iceberg state if there's been a `cd`
  earlier in the session, or re-run `pwd` before trusting a "this repo" result.

## Iceberg pilot tables (`pilot/`) — DuckDB-readable mirrors (2026-08-04)

`prices`, `macro`, `fundamentals_annual`, `fundamentals_quarterly` are mirrored
into `storage/iceberg/pilot/` (catalog `storage/iceberg/pilot_catalog.db`) so
`query.py` reads them via **real `iceberg_scan`** — the only tables that do.
`query.py` prefers `iceberg_scan(<latest metadata.json>)` for these 4, falls
back to curated parquet when the mirror is absent.

**The Windows URI gotcha that blocks every other Iceberg table here:** pyiceberg's
default `PyArrowFileIO` writes two-slash `file://C:/...` URIs into table
`location` + manifest paths, which DuckDB's `iceberg_scan` cannot open on
Windows ("Cannot open file"). The FIX is to load the catalog with
`py-io-impl=pyiceberg.io.fsspec.FsspecFileIO` AND a **three-slash** warehouse
(`file:///C:/...`); then pyiceberg writes URIs BOTH tools read. Existing
`constituents.*`/`shipping.*` tables predate this and still scan-fail in DuckDB
(they're read via parquet globs in query.py instead). Don't create new Iceberg
tables with the 2-slash pattern.

Tooling: `iceberg_pilot.py` (catalog loader + `latest_metadata()` +
`replace_from_parquet()` full-replace sync) and `migrate_pilot.py` — run the
latter after `curated.py` to refresh the mirrors (manual by design):
`C:\ProgramData\anaconda3\python.exe migrate_pilot.py` (or `--only prices,...`).

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
  OAuth re-auth: use `scripts\schwab_reauth.py` — it binds an HTTPS listener on the
  callback URL (127.0.0.1:8182), captures the browser redirect in-process well inside the
  ~30s code window, so Zander just logs in (no paste), then proves the stored refresh
  token actually advanced and spends one live quote call confirming it works. Persistent
   cert under `%LOCALAPPDATA%\schwab_reauth\` is already trusted in CurrentUser\Root, so no
   TLS warning; if the script reports it regenerated the cert (they expire), re-run the
   `certutil -addstore` line it prints or the warning comes back mid-window. CAVEAT
   (cost two failed attempts on 2026-08-20): default browser is FIREFOX, which ignores
   CurrentUser\Root entirely — its "Potential Security Risk Ahead" page must be clicked
   through (Advanced -> Accept the Risk) or the redirect never reaches the listener and
   the pasted code dies in the ~30s window. Also: kill any waiting reauth process before
   a `--callback-url` exchange — schwabdev holds tokens.db locked while awaiting the
   redirect, and a second process just hits `database is locked`. Since the bash
  tool kills child trees on timeout, launch via Task Scheduler (`schtasks /create` +
  `/run`, wrapper .bat captures logs) — see work-notes/financial-data-pipeline/SESSION_NOTES_2026-08-11.md. Fallbacks:
  `--paste` (prompt) and `--callback-url "<url>"` (non-interactive, for agent sessions
  where a stdin prompt hits EOF). Tests: `tests/test_schwab_reauth.py`.
  App has Market Data API only; Trader API (positions/transactions) 401s until enabled at
  developer.schwab.com. Schwab has NO historical options — chains are snapshot-only.
- **`validate.py` printed row counts** (fixed 2026-07-26) now spot-check the
  mtime-latest raw file via `_latest_file()`. If counts ever look wrong right after a
  backfill again, check for a regression here before distrusting `curated.py` counts.
- **event_backtest.load_close()** keeps the LONGEST series across price tables so shallow
  watchlist pulls don't shadow deep history — preserve that invariant.
- Alpha Vantage: 25 req/day/key. BLS: daily quota. SEC EDGAR: ≤10 req/s + EDGAR_USER_AGENT.
- **Open-Meteo archive API's 429 is IP-level and stateful across process restarts** — confirmed
  2026-08-03 during the `open_meteo_pipeline.py --backfill` rewrite: after a request-volume spike
  (a duplicate process racing the original, then a too-fast retry pace), a freshly-restarted
  process hit 429 on its very first request even with a conservative pause and zero prior
  requests of its own. Restarting the client does NOT reset the window. If you see 429s
  immediately on a cold start, stop and wait for real idle time (duration unknown, budget 15-20min+)
  rather than tuning the pause further or restarting again — you're adding requests to an
  already-tripped window, not helping. `_date_chunks()` backfill mode has resume-skip logic
  (checks if a chunk's output file exists before re-fetching) specifically so a paused/interrupted
  backfill doesn't lose completed work — just re-run the same command later.

## Known-broken / dead ends (don't re-attempt without a new angle)

- `nasdaq_data_link_pipeline.py` — Incapsula WAF 403s everything; needs a replacement source.
  Affects `market_valuation`/`treasury_yield_curve` CATALOG tables (still NO DATA).
- USDA_NASS keys in .env return 401 (need fresh key); CENSUS_API_KEY not set.
- Baker Hughes rig count (JS SPA), AAR rail traffic (member-gated), Stooq (JS proof-of-work),
  Motley Fool transcripts (ToS prohibits scraping) — all ruled out.
- IEX_CLOUD_API_KEY is dead (service shut down 2025).
- **Finnhub free tier tightened sometime before 2026-08-01** — many endpoints that used to
  work now 403 with `"You don't have access to this resource."`, confirmed across 3
  independent pipelines: `dividend_pipeline.py` (`/stock/dividend2`, ALL 30 symbols),
  `finnhub_expansion_pipeline.py` (esg/congressional-trading/supply-chain/social-sentiment/
  earnings-quality-score/lobbying/usa-spending/uspto-patents/visa-applications/
  economic-calendar — 10 of 12 endpoints), `finnhub_fundamentals_pipeline.py`
  (`/stock/transcripts/list`, likely also eps/revenue estimates, ownership, splits,
  executives, filing-sentiment, company-news-sentiment — not individually reconfirmed).
  Only `insider_sentiment`/`sec_filings` (from expansion) still work on the free key.
  Not a code bug — needs a paid Finnhub plan to restore, or drop these CATALOG tables.
- **Tiingo corporate-actions add-on is not on the free/Power plan** — `/tiingo/
  corporate-actions/<symbol>/distributions` and `/splits` 403 "symbol likely lacks
  corporate actions add-on entitlement" for every symbol (confirmed 2026-08-01). The
  sibling `/distributions-yield` (no add-on gate) still works fine — that's the only
  live sub-table of `tiingo_corporate_actions_*`.
- Congressional trades (`congressional_trades_pipeline.py`) — both House and Senate
  disclosure aggregator endpoints 403 (confirmed again 2026-08-01, same as 07-23);
  looks like anti-bot hardening on the source site, not a URL/param bug.
- **UKMTO incident reports** (`www.ukmto.org/recent-incidents`) — Next.js SPA whose
  `/_next/data/...` route returns only an empty Sitecore layout shell; real incidents
  load from a deeper internal API. Would need a headless browser — ruled out
  2026-08-23 during piracy-pipeline vetting. Somali incident dating instead comes
  from Wikipedia (backfill) + ICC IMB markers (`piracy_pipeline.py`); EU NAVFOR
  news pages are server-rendered and remain the upgrade path for dated current
  narratives.

## Where deeper knowledge lives

- `docs/EXPERT_BRIEF.md` — the judgment layer: prioritized roadmap with reasoning, strategic
  traps, cross-repo synergy. **Read it before planning any substantial work here.**
- `docs/PROJECT_NOTES.md` — living project-state reference, updated in place (not
  append-only): active automations, verified hard API constraints/quotas, cross-repo
  dependencies. Check this before assuming a constraint is still true.
- `work-notes/financial-data-pipeline/CLAUDE_SESSION_NOTES.md` + `work-notes/financial-data-pipeline/SESSION_NOTES_2026-07-03.md` — running session log with
  per-pipeline API quirks (SimFin v3 codes, EDGAR 13F parsing, Pink Sheet URL rotation…).
- `docs/` — source research notes. `docs/FinancialDataPipeline_Future_Improvements.md` — roadmap.
- `docs/SHORT_INTEREST_SOURCES.md` — short-interest source comparison.

## Open work (as of 2026-07-06)

- `short_pressure` factor ACTIVE (2026-07-06): FINRA's biweekly CDN path 403s everything —
  the dataset moved behind the FINRA Query API, which needs registered client credentials
  from developer.finra.org (the 20-char FINRA_API_KEY in .env does NOT work for OAuth).
  `analytics/features.py` now falls back to the yfinance `short_interest` snapshot table
  (same biweekly filing, watchlist-only coverage) — keep running the pipeline daily so
  filing dates accumulate. Registering FINRA API credentials would restore full-market data.
  **Re-confirmed 2026-08-02**: the keyless `api.finra.org/data/group/otcMarket/name/
  equityShortInterest` endpoint (found 2026-08-01, used successfully for `dark_pool_volume`)
  is OTC-scoped, not NMS — verified live, AAPL/MSFT/TSLA/SPY all return 204. The NMS-scoped
  `equityMarket/equityShortInterest` (what this factor actually needs) is 401 keyless. No
  keyless path exists; registered credentials remain the only fix.
- `sentiment` factor ACTIVE (2026-07-06): `news_sentiment_pipeline.py` rewritten to local
  VADER + finance lexicon (no ANTHROPIC_API_KEY needed; deterministic, free). Backfilled
  1,235 articles. `fed_sentiment_pipeline.py` still requires ANTHROPIC_API_KEY (Claude API).
- Sentiment evaluation IN PROGRESS (2026-07-06 session 2, uncommitted): `sentiment_eval.py`
  (PIT-safe IC/spread harness) + `finnhub_pipeline.py --news-days N` (deep news backfill,
  5-day chunks). 365-day news pull was running when notes were written — see
  work-notes/financial-data-pipeline/SESSION_NOTES_2026-07-06.md session 2 for the remaining steps (score → curated → eval
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
  work-notes/financial-data-pipeline/SESSION_NOTES_2026-07-12.md (design) and work-notes/financial-data-pipeline/SESSION_NOTES_2026-07-16.md (implementation).
