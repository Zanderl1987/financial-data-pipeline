# Session Notes — 2026-07-31

**Branch:** master
**Session model:** Claude Sonnet 5

## What happened

### 1. `ClaudeAuto-DailyAccumulators` DNS outage — recovered, not a code bug

Session started with an untracked `DAILY_ACCUMULATOR_FAIL.txt` from the 9:00 AM
scheduled run (task registered 2026-07-23, not previously documented in memory —
runs `run_all.py --only tradingview,short_interest,finnhub_events` daily since
these are snapshot-only sources with no backfill path). Every external host
(Yahoo, FINRA, SEC, Finnhub, TradingView) hit DNS `getaddrinfo failed`
simultaneously at 9:00-9:11 AM — the whole machine had no network at that moment,
not a pipeline bug. Confirmed network was back (`nslookup`/`ping` both clean),
reran the same 3-pipeline set manually: **3 PASS / 0 FAIL**. Cleared the flag,
appended a manual-recovery line to `accumulators_summary_log.txt`.

### 2. OpenFIGI full backfill — resumed and completed

Last session (07-30) launched the 19,119-ticker OpenFIGI backfill in the
background and it was still running when that session ended. It turned out
**not to have survived** — `identifier_map` was still at the pre-backfill 3,056
rows (dated 07-18) and no python process was alive. A backfill launched via the
CLI tool's `run_in_background` is tied to the session that launched it, not a
persistent OS process — this is a real lesson for any future long external-API
pull that won't fit in one session (needs a scheduled task instead, or a
deliberate mid-session liveness check).

Relaunched cleanly (`openfigi_pipeline.py --backfill`), verified a real process
was alive shortly after starting, then tracked it to completion (~1h40m):
**1,912/1,912 batches, 9,450/19,119 tickers matched (49.4%)**, appended to
Iceberg (`identifier_map` 3,056 → 22,175 raw rows, 19,119 after curated
dedup-by-ticker — exactly the full universe, one row each). Verified live —
match/active row counts cross-checked against the pipeline's own log line, not
just exit code. `curated.py` full rebuild: 150 tables compacted, 9.5M duplicate
rows removed repo-wide. Full test suite: **494 passed, 0 failed**, no
regressions.

### 3. Alpha Vantage DJI earnings-history pacing — attempted, zero progress (quota)

Per `PROJECT_NOTES.md`'s in-flight initiative item 3 (earnings history stuck at
9/30 DJI symbols since 07-25), ran `alpha_vantage_fundamentals_pipeline.py`
(default 20-request incremental budget) this evening. **All 20 requests hit
AV's "standard API rate limit is 25 requests per day" response** — the day's
shared-IP quota had already been consumed by `earnings_sentiment_tool`'s 10:30
AM scheduled transcript pull (+24 transcripts that run). Only `earnings_calendar`
came through (1 upcoming date; that endpoint isn't budget-metered the same way).
Same failure pattern as the 07-28 attempt. Still 9/30 DJI symbols, 1,322 rows,
unchanged. **Next attempt should land in the ~10:05 AM gap** before the
transcript task fires, per the existing quota-discipline convention.

## Next up

- Retry the AV DJI earnings-history batch in tomorrow's ~10:05 AM quota gap.
- `fed_soma` backfill still needs a dedicated longer-timeout rerun (timed out
  at 3600s during the 07-23 stage-1 sweep, never retried).
- Recheck `congressional_trades`/`dividends`/`patents` — flagged 07-23 as
  "not yet confirmed permanent breaks," still NO DATA live as of today.
