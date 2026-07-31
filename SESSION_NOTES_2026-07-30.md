# Session Notes — 2026-07-30

**Branch:** master
**Session model:** Claude Sonnet 5

## What happened

Follow-up to 2026-07-29's Alpha Vantage key rotation, plus a memory-correction pass on
this repo's staging-batch status.

## Issues found and fixed

1. **Master `.env` on the portable drive was still on the old Alpha Vantage key pair.**
   The drive wasn't mounted on 07-29 when the other three `.env` copies were rotated.
   Confirmed the drive (currently at **E:**, identified by its `Claude Main` folder, not
   the letter) was plugged in, then updated `E:\Claude Main\Projects\financial-data-pipeline\.env`'s
   `ALPHA_VANTAGE_API_KEY`/`_2` to the new values. All 4 copies (this repo,
   `earnings_sentiment_tool`, `custom_index_tool`, and the E: drive master) are now
   consistent.

2. **Stale memory: "2026-07-14 staging batch not yet applied."** Cross-session memory
   claimed the pipeline batch staged in `E:\AI_Projects\FinancialPipelineStagingUpdates`
   (reviewed 2026-07-14) was still waiting to be merged, and separately guessed a merge
   happened "sometime between 07-20 and 07-23" based on indirect evidence (CATALOG table
   count growth). Neither was right. Verified directly against git history instead of
   trusting the memory:
   - Commit `a0b78b0` (2026-07-16, "Add 15 new free/public data pipelines and fix 10 bugs
     from full-scope code review") merged all 14 smoke-tested staging pipelines
     (BLS expansion/OES-QCEW, Treasury fiscal, CoinGecko expansion, FRED macro/rates-GDP,
     EIA expansion/hourly-grid/petng-prices, SEC EDGAR, Tiingo corporate-actions/
     fundamentals, Finnhub expansion/fundamentals, Alpha Vantage fundamentals), plus the
     `finnhub_events_pipeline.py` year→`obs_year` Hive-partition fix.
   - Commit `d5dd859` (2026-07-22, "feat: index constituents/securities pipeline +
     Iceberg migration") merged the later constituents expansion (index_members,
     securities, fund_holdings, identifier_map — 4 new Iceberg tables).
   - Staging-dir cleanup items (storage/backup, storage/tmp, superseded
     `apply_*_wiring.py` scripts) were also already done.
   - **Nothing remains to apply from the staging dir.** The repo's own session notes since
     (07-26, 07-27, 07-29) describe work that supersedes it entirely (logging framework,
     an adversarial code review that caught a critical `subprocess` encoding bug, and the
     eval framework) — those docs are the current source of truth, not the staging notes.
   - Corrected `project_financial_data_pipeline.md` in cross-session memory to reflect
     this and flagged that `EXPERT_BRIEF.md`/CLAUDE.md's "Open work (as of 2026-07-06)"
     section is itself stale relative to everything that's happened since.

3. **Full test suite reran to confirm repo health after all the above:** `482 passed`,
   8 expected warnings (small synthetic date ranges in eval-framework test fixtures, not
   real issues). No regressions from anything touched this session (nothing in this repo's
   source was actually modified — only the external `.env` and cross-session memory).

## State / Next Up

- No open blockers from this session. The `.env` split across all 4 locations is fully
  reconciled.
- `oecd_pipeline.py` remains modified/uncommitted in the working tree — pre-existing,
  unrelated to this session, not touched. Still flagged for the user to decide on.
- Genuinely open items (not staging-related) per the 2026-07-17 staging notes' own TODO
  list, never picked up: OpenFIGI full backfill (~5-10K tickers), bond-ETF parser for
  AGG/LQD/HYG/TIP (different BlackRock XML shape), Phase 5 portfolio analytics on the
  Iceberg constituents tables.

## Session 2: OECD pipeline finish + cross-session merge (same day, later)

1. **`oecd_pipeline.py` uncommitted rewrite (flagged above) — finished, verified, committed
   (`df0387c`).** It was a complete rework, not a mid-edit: switched from querying each of
   the 7 KEI indicators individually against the new SDMX Data Explorer API to 2 wildcard
   queries (KEI + LFS dataflows) with local pandas filtering. Ran live: 5,240 deduped rows,
   8 indicators (7 KEI + LFS unemployment), 14 countries. `validate.py`: `oecd_macro` PASS.
   Full suite: 482 passed, no regressions.

2. **`git push` rejected — this clone was behind `origin/master` without knowing it.**
   `git status` had shown a clean "ahead 1" at session start because no `fetch` had run;
   another session pushed 2 commits earlier the same day (16:53/17:11) — `etf_holdings_pipeline.py`
   (new pipeline, SecuritiesDB) + `fund_holdings_pipeline.py` expansion (65 ETFs + 52 MFs),
   plus error-logging wiring and Iceberg metadata. Confirmed no file-path overlap with the
   local commits (session notes + `oecd_pipeline.py`) before merging — clean merge, no
   conflicts. Full suite after merge: **484 passed** (482 + 2 new tests from the ETF work),
   same 8 expected warnings. Pushed: `aed01cb..0a39b36`.
   **Fix applied**: added a note to CLAUDE.md's Environment section — always `git fetch
   origin` at the start of a session before trusting `git status` ahead/behind counts,
   since this repo is worked on from multiple sessions/devices.

## Session 3: build-item sweep (Iceberg gitignore gap, FRED IDs, bond-ETF parser, OpenFIGI)

User asked to work through 4 backlog items incrementally: the Iceberg metadata
gitignore gap, 9 broken FRED series IDs, a bond-ETF parser for AGG/LQD/HYG/TIP, and an
OpenFIGI full backfill.

### 1. Iceberg metadata gitignore gap — DONE, turned out much bigger than framed (commit `c034fbe`)

What looked like "300+ untracked metadata files" was two real bugs:

- **Snapshot-per-partition bloat**: `fund_holdings_pipeline.py`/`etf_holdings_pipeline.py`
  called `table.overwrite()` once per `fund_ticker` in a loop — each call is its own
  Iceberg snapshot + `metadata.json` version. `fund_holdings` hit 467 metadata files
  after a handful of runs. Fixed by batching all per-ticker overwrites into one
  `table.transaction()` (verified live: 2-ticker batched write → 1 new metadata.json,
  not 2). Added `iceberg_utils.expire_old_snapshots()` (30-day retention) as a backstop,
  called after every write in both pipelines — trims the logical snapshot log, though
  pyiceberg 0.11.1's Python client has no on-disk orphan-file GC, so old data/manifest
  files already written aren't reclaimed.

- **Cross-clone phantom table (the real find)**: investigating why `etf_holdings`
  showed a full snapshot history but read zero rows uncovered that its Iceberg table
  `location` was baked in at creation time to the *other* local clone
  (`C:\Users\zande\financial-data-pipeline` — see PROJECT_NOTES.md's "Two local clones"
  note). Because Iceberg metadata (small JSON/AVRO) wasn't gitignored but the actual
  parquet data was (same rule as every other `storage/` table), the table's metadata
  synced via git and merge and made this clone's catalog look fully populated while
  every query silently returned zero rows — no error either way. Recovered the real
  7,723-row dataset (119 tickers) from the other clone, deduped on natural key, dropped
  the stale catalog entry, recreated the table fresh in this clone (verified
  `table.location()` contains "PycharmProjects" before writing), removed 10 orphaned
  bootstrap files from the old broken table. `fund_holdings` and the other 5
  Iceberg-backed tables were unaffected (created from this clone originally).

  Root-cause fix: `storage/iceberg/**/metadata/` and the SQLite catalog DB
  (`constituents_catalog.db`) are now gitignored — same treatment as `storage/raw/`'s
  parquet data. The whole Iceberg warehouse is local, regenerable state per clone, not
  git-synced. This is what actually prevents the phantom-table failure mode from
  recurring on any of the 7 Iceberg tables. Untracked 202 already-committed files (kept
  on disk, just stopped tracking).

  **Process note**: lost significant time to a stray `cd` earlier in the session (to
  check the other clone's git log) that persisted across Bash tool calls — several
  "verification" commands using relative paths were silently reading the wrong clone,
  making a working fix look broken. Documented in CLAUDE.md's new Iceberg section as a
  standing gotcha: use absolute paths for Iceberg-state checks after any `cd`, or
  re-run `pwd` first.

  Verified: `validate.py` PASS for both tables, full test suite run twice (once
  mid-investigation, once after returning to the correct directory) — 484 passed both
  times, no regressions. Pushed `c034fbe`.

### 2. 9 broken FRED series IDs — already resolved, verified live (no new work needed)

Turned out commit `9813469` (2026-07-29, the same commit that did the first OECD
rewrite pass) already fixed this: 2 series got working replacement IDs
(`PCU3311103311101`→`PCU331110331110`, `PCU3272133272131`/`PCU3272153272151`→
`PCU327213327213`/`PCU327215327215`), the other 7 (gold/palladium/platinum IBA
series, `WPU1019A2S`, `PCU3272143272141`, `WPU0619`) were removed outright with
documented reasoning — FRED deleted the IBA precious-metals series in Jan 2022 and
discontinued the BLS glass/steel series in Jul 2025, no direct replacements exist.
Ran `commodity_macro_pipeline.py` live today to confirm: zero HTTP 400s across all
58 series. Two unrelated series (`PCU3259103259101`, `PCU3312223312221`) show "No
data returned" in the incremental window — checked directly against FRED's API
(200 OK, but last observations from 2017 and 2010 respectively) — these are
legitimately discontinued-but-valid series, not broken IDs, and weren't part of
the original 9. `commodities`/`macro`/`oecd_macro` all PASS in validate.py.

### 3. Bond-ETF parser for AGG/LQD/HYG/TIP — DONE (commit `40f482e`)

BlackRock's fixed-income Holdings worksheets have no Ticker column (bonds are
identified by Name only) and bond-specific fields instead of equity's Ticker/
Sector layout — confirmed by fetching AGG/LQD/HYG/TIP live and inspecting the
raw XML header row directly. Built `fetch_blackrock_bond_holdings()` +
`BOND_ETF_PID_MAP`, refactored the shared fetch/XML-parse logic out of the
equity function into `_fetch_blackrock_holdings_rows()`. Extended the
`fund_holdings` Iceberg table with 5 new nullable columns (par_value,
maturity_date, coupon_pct, duration, ytm_pct) via schema evolution — no
rewrite of the existing ~231K rows. Wired into CLI (`--skip-bonds`,
`--bond-tickers`).

Testing live end-to-end (not just unit-level) surfaced two real bugs that
would have silently corrupted this data on first use:
- `curated.py`'s dedup key `(fund_ticker, holding_ticker, snapshot_date)`
  collapsed every bond in a fund down to 1 row, since `holding_ticker` is
  always null for bonds. Verified `(holding_name, maturity_date, coupon_pct,
  par_value)` is exactly unique per position against live data (13,299/3,145/
  1,328/48 total vs. distinct, before vs. after par_value) before adopting it.
- `validate.py`'s schema had `holding_ticker` in `critical_nn` (fails if
  >50% null) — a bond-only raw file is 100% null there by design. Removed
  from `critical_nn`, kept in `required` (column should still exist).

Verified live: all 4 funds' row counts match BlackRock's own reported
security counts exactly. `validate.py` PASS, curated rebuild preserves all
17,772 bond rows, full test suite 484 passed.

### Next up in this sweep
- [ ] OpenFIGI full backfill (~5-10K tickers)
