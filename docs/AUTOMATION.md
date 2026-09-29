# Automation — financial-data-pipeline

Set up 2026-07-06 (by Claude, with Zander's approval).

## ClaudeAuto-PipelineQuality (Windows Scheduled Task)

- **What:** runs `scripts\weekly_data_quality.ps1` every Monday 9:30 AM (catches up after
  boot if the machine was off): `validate.py` over all tables, full output archived to
  `storage\quality_reports\validate_YYYY-MM-DD.txt`, one summary line per week appended
  to `storage\quality_reports\summary_log.txt`.
- **Failure signal:** `QUALITY_FAIL.txt` appears at repo root when validate.py reports
  any FAIL. Any future Claude session should check for that file. Auto-clears on the
  next clean run.
- **Also runs `coverage_audit.py --fail-on-gap`** (added 2026-09-10), archived to
  `storage\quality_reports\coverage_audit_YYYY-MM-DD.txt`, one summary line per week
  appended to `storage\quality_reports\coverage_audit_summary_log.txt`. Checks the
  commodity/FX pipelines against closed upstream catalogs FRED and Frankfurter
  publish in full (FRED's IMF PCPS "Global price of X" series, Frankfurter/ECB's
  currency list) — a gap here means a series/currency the source publishes that no
  pipeline tracks. Added after a session found (and fixed) Uranium, Dubai/APSP
  crude, a never-wired-up Silicon USGS entry, and 6 missing IMF agriculture series
  (Hides/Olive Oil/Swine/Salmon/Wool) all in one sitting — this makes that check
  recurring instead of ad-hoc. It also does a second-pass keyword search against a
  curated-table allowlist before calling something a GAP, because a different
  pipeline can cover the same commodity under a totally different series ID (this
  is how 21 of that session's first 27 "gaps" turned out to already be covered by
  `worldbank_pink_sheet.py` under different names — a source-code-only grep can't
  see that). Ambiguous matches are reported as "POSSIBLE DUPLICATE" for a human/
  Claude to judge, not silently dropped or silently flagged.
  **Failure signal:** `COVERAGE_GAP.txt` appears at repo root when a confirmed gap
  is found. Separate from `QUALITY_FAIL.txt` — a coverage gap is "we should add a
  series," not "something is broken." Auto-clears on the next clean run.

## ClaudeAuto-DailyAccumulators (Windows Scheduled Task)

- **What:** runs `scripts\daily_accumulators.ps1` every day at 9:00 AM (catches up
  after boot if the machine was off): `run_all.py --only tradingview,short_interest,
  finnhub_events`, output archived to `storage\quality_reports\accumulators_YYYY-MM-DD.txt`,
  one summary line per day appended to `storage\quality_reports\accumulators_summary_log.txt`.
  All three are keyless/unattended-safe: history only exists if pulled daily (`tv_ratings`,
  `short_interest` filing dates, `earnings_calendar`) — missed days are permanent gaps,
  no backfill recovers them (confirmed 2026-07-23: Finnhub's free tier will not return
  earnings_calendar rows older than ~1 year even with `--backfill`, so incremental daily
  accumulation is the only way this table gets deeper). Deliberately NOT included: Schwab
  pipelines (`schwab_movers` etc.) — OAuth is interactive and would hang an unattended run.
- **Failure signal:** `DAILY_ACCUMULATOR_FAIL.txt` appears at repo root when run_all.py
  reports any FAIL. Any future Claude session should check for that file. Auto-clears on
  the next clean run.

## ClaudeAuto-DailyPipelines (Windows Scheduled Task)

> Renamed 2026-08-11 from `ClaudeAuto-DailyStage1` (`scripts\daily_stage1.ps1`) when it was
> widened from stage 1 to all three stages. The old task was unregistered — it had never
> successfully run, and after the rename it pointed at a file that no longer exists.

- **What:** runs `scripts\daily_pipelines.ps1` every day at 3:00 AM (catches up after boot if
  the machine was off): `run_all.py --skip <19 names>`, all three dependency stages, output
  archived to `storage\quality_reports\daily_pipelines_YYYY-MM-DD.txt`, one summary line per
  day appended to `storage\quality_reports\daily_pipelines_summary_log.txt`.
  `ExecutionTimeLimit` is **8h** (was 6h until 2026-09-29).
- **Runtime outgrew the old limit (found 2026-09-29).** The original budget was 2.5-3h for 71
  pipelines. By late September about 104 ran, taking 4.5-6.7h of pipeline time alone, so the
  6h limit killed the wrapper on most nights. `run_all.py` survived and finished (09-29's run
  ended ~09:17 with 99 PASS / 5 FAIL), but the wrapper died before writing its summary line or
  `DAILY_PIPELINES_FAIL.txt`: the summary log has only 2 entries between 09-12 and 09-29. The
  watchdog reports this as `0x41306 terminated`. Heaviest pipelines: `fund_holdings` 45-100
  min, `yahoo_options` ~33, `etf_holdings` 20-30, `stockanalysis` ~24. An overrun also pushes
  stage 2's Schwab calls past 09:00 into `ClaudeAuto-DailyAccumulators`. If runtime creeps
  toward 8h again, trim or split the job rather than raising the limit.
- **Why it exists:** added 2026-08-11. Until then the only scheduled *fetching* was
  `ClaudeAuto-DailyAccumulators` — seven pipelines. Everything else refreshed only when a
  human happened to run `run_all.py`, and a `fetched_at` sweep found 11 of 182 curated tables
  stale in clusters dated to those manual runs (`cot` 56d, `alpha_vantage_forex`/`_technical`
  49d, `bls_*` 27d, `eia_*`/`fred_rates_gdp`/`treasury_exchange_rates` 19d, `prices` 9d).
  None of those pipelines were broken. Nothing ran them.
- **Why all three stages** (widened 2026-08-11, was `--stage 1`): stage 1 alone left 13 specs
  unreachable, and the gap was invisible because the job would have passed every night while
  doing nothing about them. Stage 3 is where it showed — it holds `signal_monitor` and
  `news_sentiment`, which feed the **active** signal-health and sentiment factors, and
  `signal_health` had gone 38 days stale unnoticed. `run_all.py` already orders the stages by
  dependency, so one job running all three beats a second job racing this one on a guessed
  start time: stage 3 reads what stages 1 and 2 just wrote.
- **Why `--skip` and not `--only <list>`:** a hand-maintained `--only` list is the same defect
  one level up — add a pipeline, forget the list, and it is silently never scheduled, which is
  exactly how six sources went dark for six weeks before `891d97d`. Stage selection picks up
  new pipelines automatically; only the skip list is hand-maintained, and it names things that
  are known bad, which changes slowly. `tests/test_catalog.py::TestScheduledJobSkipLists` fails
  the suite if a skip entry stops matching a real pipeline (`--skip` ignores unknown names
  silently, so a typo would otherwise be invisible).
- **Excluded, and why:** metered keys — `alpha_vantage` **and** `alpha_vantage_fundamentals`
  (25/day/key, reserved for `ClaudeAuto-AVEarningsPacing` and the earnings-transcript work;
  this is why stage 3's `alpha_vantage` is excluded even though stage 3 is otherwise cheap,
  locally-derived work), `bls_expansion`/`bls_oes_qcew` (BLS keyless 25/day),
  `eia`/`eia_expansion`/`eia_petng_prices`/`eia_hourly_grid`, `gas_prices`. Known-dead per
  CLAUDE.md — `nasdaq_data_link`, `usda`, `trade`, `congressional_trades` (they fail every run
  and would keep the job permanently red, masking real failures). Already daily —
  `tradingview`, `short_interest`, `finnhub_events`. Failing every night, skipped 2026-09-29
  (about 35 min saved per run): `lda_lobbying` (hangs silently to its 30-min timeout, every
  run since at least 09-01), `ibkr_borrow_fee` (IBKR FTP connect times out, WinError 10060,
  since ~09-06), `schwab_portfolio` (needs the Schwab Trader API, not enabled). Take them off
  the list once they are fixed, or they stay stale.
- **Stage 2 needs a live Schwab token.** The refresh token expires every 7 days and renewing it
  requires a human at a browser (`scripts\schwab_reauth.py`), so this job **will** go red for
  the Schwab specs whenever it lapses. That is the intended signal — a silent skip would hide
  the outage. The FAIL flag says so and names the renewal command.
- **Still not covered:** `alpha_vantage_forex` and `alpha_vantage_technical` (owned by the
  excluded `alpha_vantage` spec — they refresh only when someone spends the AV quota on them),
  and the four tables whose owners are skipped as metered: `bls_cps_demographics`, `bls_qcew`,
  `eia_petroleum_futures`, `eia_refiner_margins`. These will show as permanently stale in the
  pipeline-explorer scan **by design**; that is the cost of the quota decision, not a fault.
- **Failure signal:** `DAILY_PIPELINES_FAIL.txt` at repo root, naming the failed pipelines and
  the archived report. Auto-clears on the next clean run.
- **Note — there are now three full HuggingFace syncs per day.** `run_all.py` syncs the whole
  curated snapshot at the end of every run, and `SchwabUniverseIncrementalPrices` calls
  `upload_huggingface.py` directly, so the snapshot uploads at ~02:15 (universe job), ~06:00
  (this job) and ~09:00 (accumulators). Nothing is lost, but if bandwidth matters the ones to
  drop are the 02:15 and 09:00 uploads — this job carries by far the most new data. Note also
  that if the 22:00 universe job overruns past 03:00 the two overlap; `MultipleInstances` only
  protects a task from *itself*, not from a different task touching the same curated tree.

## HuggingFace dataset sync (`run_all.py`)

`run_all.py` automatically syncs `storage/curated/` to the public HuggingFace dataset
(`ZanderL1337/financial-data-pipeline`) at the end of every run, via `upload_huggingface.py`.
The sync is gated on all of: `HF_TOKEN`/`HUGGINGFACE_TOKEN` being set, `--no-compact` not
being passed (curated compaction must have had a chance to run this session), and at least
one pipeline having PASSed. It uploads the full curated snapshot, then verifies the upload
by listing remote files — not just the tables that ran this session. This can be disabled
per-invocation with `--no-hf-sync`. An `hf_sync` FAIL (e.g. rate limit, transient network
error, expired token) is reported in the summary table but does **not** flip `run_all.py`'s
overall exit code — it's an HF-side concern, distinct from a pipeline/data-collection
failure, so it will not trigger `DAILY_ACCUMULATOR_FAIL.txt` on its own.

**Two design questions resolved 2026-08-04:**
- *Verification depth:* existence-check (`HfApi().list_repo_files()`) only, no
  content/SHA comparison. Kept as-is — HF's `upload_folder` commits are hash-verified
  on the HF side already (git-lfs/Xet content addressing), so "file exists remotely
  but is silently corrupted" is not a realistic failure mode this needs to guard
  against. Revisit only if a real corruption incident is ever observed.
- *`daily_accumulators.ps1` and `--no-hf-sync`:* NOT added. The 3 accumulator
  pipelines (`tradingview`, `short_interest`, `finnhub_events`) are specifically
  the ones where "history only exists if pulled daily" — each run produces
  genuinely new rows (new filing dates, new calendar entries), not just a
  same-data re-upload. Syncing those to HF daily is the intended behavior, not
  spurious churn from the README's date stamp.

## ClaudeAuto-AVEarningsPacing (Windows Scheduled Task)

- **What:** runs `scripts\av_earnings_pacing.ps1` every day at 10:45 AM:
  `alpha_vantage_fundamentals_pipeline.py` (incremental, default 20-request budget) to
  pace the DJI-30 earnings-history backfill (`alpha_vantage_earnings`/
  `alpha_vantage_earnings_calendar` CATALOG tables), still at 9/30 symbols as of
  2026-08-04. Output archived to `storage\quality_reports\av_earnings_pacing_YYYY-MM-DD.txt`.
- **Cross-repo quota coordination (decided 2026-08-04):** Alpha Vantage's 25 req/day
  quota is shared **per IP, rolling-24h** (not per-key, not midnight-reset) across this
  machine's two repos — this one and `earnings_sentiment_tool`, whose own
  `ClaudeAuto-TranscriptPull` fires daily at 10:30 AM. A one-time retry at 10:05 AM
  (BEFORE 10:30) was tried on 07-31 and 08-01 and failed both times with zero progress,
  because rolling-24h means yesterday's ~10:30 usage doesn't roll off until ~10:30
  *today* — a 10:05 run still saw it as unexpired quota. Fixed by scheduling this task
  AFTER TranscriptPull's run (10:45, not 10:05) instead of before it. This will still
  legitimately report `QUOTA` status most days until `earnings_sentiment_tool`'s
  725-file transcript cache finishes (~2026-08-06 per that repo's own ETA) and its
  daily pull becomes a zero-quota no-op — at that point this task starts making real
  daily progress automatically, no further changes needed.
- **Failure signal:** `AV_EARNINGS_PACING_FAIL.txt` appears at repo root — `FAIL` status
  means the pipeline crashed, `QUOTA` status means it hit AV's rate limit with no real
  progress (expected during the coordination window above, not itself an error). Any
  future Claude session should check for that file. Auto-clears on the next OK run.

## ClaudeAuto-FundamentalsHFRefresh (Windows Scheduled Task)

- **What:** runs `scripts\fundamentals_hf_refresh.ps1` every Sunday at 8:00 AM (catches up
  after boot if the machine was off): `fundamentals_pipeline.py --full-market` (extraction
  only; the pipeline no longer pushes raw files to HF) -> `curated.py` ->
  `build_fundamentals_dataset.py` (assembles the Option-D snapshot — facts/companies/
  filings/wide-latest/metrics — and pushes all files to HF in ONE atomic commit, one
  coherent revision per run) -> `verify_hf.py --repo
  ZanderL1337/financial-fundamentals`, combined output archived to `storage\quality_reports\
  fundamentals_hf_YYYY-MM-DD.txt`, one summary line per week appended to
  `storage\quality_reports\fundamentals_hf_summary_log.txt`. Added 2026-08-04 after the
  dataset was found stale for ~7 weeks (2026-06-15 -> 08-04) with no automation catching it.
  Weekly cadence chosen deliberately: SEC filings don't change fast enough to justify a daily
  ~1.3GB EDGAR re-download, and Sunday 8:00 AM avoids overlapping `ClaudeAuto-DailyAccumulators`
  (daily 9:00 AM) and `ClaudeAuto-PipelineQuality` (Monday 9:30 AM).
- **2026-08-05 schema change:** dataset converted from two long files to the 5-file
  Option-D snapshot (foreign issuers via `ifrs-full`, forms 20-F/40-F/6-K/10-K/A/10-Q/A/8-K,
  accession-tracked restatements). Old `financials_*_latest.parquet` filenames now hold the
  WIDE latest-filing-wins tables. See `work-notes/financial-data-pipeline/SESSION_NOTES_2026-08-05.md`.
- **Failure signal:** `FUNDAMENTALS_HF_FAIL.txt` appears at repo root if any of the four
  steps fails (pipeline crash, `curated.py` failure, `build_fundamentals_dataset.py` failure,
  or `verify_hf.py` VERIFY FAIL — stale data, row-count drop, excess duplicate rate, or
  snapshot.json/actual row-count mismatch). Any future Claude session should check for
  that file. Auto-clears on the next clean run.

## SchwabUniverseIncrementalPrices (Windows Scheduled Task)

- **What:** runs `scripts\universe_prices.ps1` daily at **8:00 PM**. Keeps the full-universe
  `prices` table current. The 27,759-symbol `schwab_universe_backfill.py` full-history pull is
  a one-shot backfill, so without this job the universe's daily bars freeze while only the
  watchlist (`prices` pipeline) stays fresh.
- **Chain:** `schwab_auth.py` token preflight (stops cleanly instead of letting schwabdev
  thrash on an expired token) -> `schwab_universe_backfill.py --incremental --days N
  --chunk-size 250 --skip-empty-from schwab_universe_backfill_progress.json` (~4-5h at
  0.55s/request; one request per symbol whatever the window) -> `curated.py --table prices`
  -> `upload_huggingface.py` with default flags, the same call `run_all.py`'s
  `sync_huggingface()` makes (the dataset is public, and the default leaves it public).
- **Window N self-heals missed nights:** N = days since the last *completed* run
  (`storage\quality_reports\universe_prices_last_ok.txt`) + 7 overlap, minimum 14. It is
  measured from the last completed run, not the newest file, because a killed run still
  writes today-dated batches. Re-running the same UTC day resumes via the date-stamped
  progress file `schwab_universe_incremental_YYYY-MM-DD.json`.
- **Output:** report `storage\quality_reports\universe_prices_<date>.txt`, one line per run in
  `universe_prices_summary_log.txt`, and flag `UNIVERSE_PRICES_FAIL.txt` on a failed
  preflight, a crash, >500 network-failed symbols, a failed curate, or an upload that didn't
  print its `Done!` line (`upload_huggingface.py` exits 0 even when it refuses to publish).
- **Why 8 PM:** finishes ~1 AM, before `ClaudeAuto-DailyPipelines` (3:00 AM), which also uses
  the Schwab token DB and recompacts `prices`.
- **History:** registered 2026-08-11 pointing at `%TEMP%\opencode\schwab_universe_incr.bat`.
  That `.bat` was later cleaned out of `%TEMP%`, and the job had never written a single
  `prices_incr_batch*` file. So universe coverage stopped at 2026-07-23 and nothing reported
  it. Rebuilt into the repo 2026-09-29; the first run catches up the whole gap (~74 days).
  Never keep a scheduled job's script in `%TEMP%`.

## ClaudeAuto-TaskWatchdog (Windows Scheduled Task)

Hourly: `scripts\task_watchdog.ps1` reads every `ClaudeAuto-*` / `SchwabUniverse*` task's
`LastTaskResult` and writes **`SCHEDULED_TASK_FAIL.txt`** (repo root) listing each job whose
last run failed or whose next run is 3h+ overdue. The file is deleted once every job is
clean. Changes are logged to `storage\quality_reports\task_watchdog_log.txt`.

**Why it exists:** the other wrappers write their FAIL flag only *after* python returns. If
the wrapper process itself is killed, no flag and no summary-log row are written. That
happened 2026-09-28: after a wake from Modern Standby at 15:11:33, four catch-up tasks died
~20s later with `0xC000013A` (DailyPipelines died the same way at 03:48), and none of them
left a trace. The task's own last result is the one record that survives, so this checks
that from outside. A job stays listed until its next run succeeds. Codes are decoded in the
flag file (`0xC000013A` killed, `0x800710E0` refused to start, etc.).

## Battery gating — read this before diagnosing a task that "didn't run"

`Register-ScheduledTask` defaults `DisallowStartIfOnBatteries` and
`StopIfGoingOnBatteries` to **true**, and every task here inherited that. On a laptop that
means a scheduled job silently refuses to start whenever you happen to be unplugged, and
reports last-result **`2147946720`** (`0x800710E0`, "The operator or administrator has
refused the request") — which reads like a permissions problem and is not one. This is what
was wrong with `ClaudeAuto-PipelineExplorerScan`; the script was fine and ran clean (exit 0)
the moment it was started on AC power.

Both flags were cleared on all tasks 2026-08-11, so they now run on battery and a long job
is not killed mid-run by unplugging. To check or re-apply:

```powershell
# audit
Get-ScheduledTask | Where-Object { $_.TaskName -like "ClaudeAuto*" -or $_.TaskName -like "SchwabUniverse*" } |
  ForEach-Object { [PSCustomObject]@{ Name=$_.TaskName
                                      NoBattStart=$_.Settings.DisallowStartIfOnBatteries
                                      StopOnBatt=$_.Settings.StopIfGoingOnBatteries } }

# re-apply to all
Get-ScheduledTask | Where-Object { $_.TaskName -like "ClaudeAuto*" -or $_.TaskName -like "SchwabUniverse*" } |
  ForEach-Object { $s = $_.Settings
                   $s.DisallowStartIfOnBatteries = $false
                   $s.StopIfGoingOnBatteries = $false
                   Set-ScheduledTask -TaskName $_.TaskName -TaskPath $_.TaskPath -Settings $s }
```

A newly registered task will silently reacquire the default unless you pass
`New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries`.

## Managing

```powershell
Get-ScheduledTask ClaudeAuto-DailyPipelines | Get-ScheduledTaskInfo         # last/next run
Start-ScheduledTask ClaudeAuto-DailyPipelines                               # run now (5-6h)
Unregister-ScheduledTask ClaudeAuto-DailyPipelines -Confirm:$false          # remove

Get-ScheduledTask ClaudeAuto-PipelineQuality | Get-ScheduledTaskInfo        # last/next run
Start-ScheduledTask ClaudeAuto-PipelineQuality                              # run now
Unregister-ScheduledTask ClaudeAuto-PipelineQuality -Confirm:$false         # remove

Get-ScheduledTask ClaudeAuto-DailyAccumulators | Get-ScheduledTaskInfo      # last/next run
Start-ScheduledTask ClaudeAuto-DailyAccumulators                            # run now
Unregister-ScheduledTask ClaudeAuto-DailyAccumulators -Confirm:$false       # remove

Get-ScheduledTask SchwabUniverseIncrementalPrices | Get-ScheduledTaskInfo   # last/next run
Start-ScheduledTask SchwabUniverseIncrementalPrices                         # run now
Unregister-ScheduledTask SchwabUniverseIncrementalPrices -Confirm:$false    # remove

Get-ScheduledTask ClaudeAuto-FundamentalsHFRefresh | Get-ScheduledTaskInfo  # last/next run
Start-ScheduledTask ClaudeAuto-FundamentalsHFRefresh                       # run now
Unregister-ScheduledTask ClaudeAuto-FundamentalsHFRefresh -Confirm:$false  # remove

Get-ScheduledTask ClaudeAuto-AVEarningsPacing | Get-ScheduledTaskInfo      # last/next run
Start-ScheduledTask ClaudeAuto-AVEarningsPacing                            # run now
Unregister-ScheduledTask ClaudeAuto-AVEarningsPacing -Confirm:$false       # remove
```
