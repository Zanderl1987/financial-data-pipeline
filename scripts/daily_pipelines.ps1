# Daily full-pipeline refresh: all three dependency stages, minus the metered
# and known-dead sources. Archives output, keeps a rolling one-line-per-day
# summary, leaves a visible flag file on FAIL. Registered as Windows Scheduled
# Task "ClaudeAuto-DailyPipelines" (see AUTOMATION.md).
#
# WHY THIS EXISTS (2026-08-11). Before this job, the only scheduled fetching was
# ClaudeAuto-DailyAccumulators -- seven pipelines. Everything else refreshed only
# when a human happened to run run_all.py, so tables drifted stale in clusters
# dated to whenever the last manual run touched them: cot 56 days,
# alpha_vantage_forex/technical 49, bls_* 27, eia_*/fred_rates_gdp/
# treasury_exchange_rates 19. None of those pipelines were broken. Nothing ran them.
#
# WHY ALL THREE STAGES (widened 2026-08-11, was --stage 1). Stage 1 alone left 13
# specs unreachable, and the gap was invisible because the job passed every night
# while doing nothing about them. Stage 3 is where the damage showed: it holds
# signal_monitor and news_sentiment, which feed the ACTIVE sentiment and signal-
# health factors, and signal_health had gone 38 days stale with nobody watching.
# run_all.py already orders the stages by dependency, so running it without
# --stage is both simpler and more correct than a second job racing this one on a
# guessed start time -- stage 3 reads what stages 1 and 2 just wrote.
#
# WHY --skip AND NOT --only <list>. A hand-maintained --only list is the same
# defect one level up: add a pipeline, forget the list, and it is silently never
# scheduled -- exactly how six sources went dark for six weeks before 891d97d.
# Stage selection picks up new pipelines automatically. The only hand-maintained
# part is the skip list, which names things that are known bad, and that changes
# far more slowly. tests/test_catalog.py asserts every name below still matches a
# real PipelineSpec, because --skip ignores typos silently.
#
# Runtime: this used to be ~173 min for 71 pipelines. By late September 2026 it was
# ~104 pipelines taking 4.5-6.7 h of pipeline time alone, so the task's old 6 h
# ExecutionTimeLimit killed this wrapper on most nights before it wrote the
# summary line or flag (run_all.py itself survived and finished). The limit is now
# 8 h. Heaviest: fund_holdings 45-100 min, yahoo_options ~33, etf_holdings 20-30,
# stockanalysis ~24. If runs creep toward 8 h again, trim or split, don't just
# raise the limit: an overrun pushes stage 2's Schwab calls into
# DailyAccumulators (09:00), which this 03:00 slot exists to avoid.
#
# STAGE 2 NEEDS A LIVE SCHWAB TOKEN. The refresh token expires every 7 days and
# renewing it requires a human at a browser (scripts\schwab_reauth.py), so this
# job WILL go red for the Schwab specs whenever the token lapses. That is the
# intended signal, not a defect -- a silent skip would hide the outage.

$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$py = "C:\ProgramData\anaconda3\python.exe"
$reportDir = Join-Path $repo "storage\quality_reports"
$flag = Join-Path $repo "DAILY_PIPELINES_FAIL.txt"
$stamp = Get-Date -Format "yyyy-MM-dd"
$report = Join-Path $reportDir "daily_pipelines_$stamp.txt"

# Excluded from the daily run, with the reason each one is out:
#   metered keys -- a daily burn would starve the jobs that need the quota.
#     Alpha Vantage is 25 req/day/key and is reserved for ClaudeAuto-AVEarningsPacing
#     and the earnings-transcript work; that is why stage 3's alpha_vantage is
#     here despite stage 3 otherwise being cheap, locally-derived work.
#     BLS keyless is 25/day. EIA is metered per key.
#   known-dead    -- documented in CLAUDE.md "Known-broken / dead ends"; these
#     fail every run and would keep the job permanently red, hiding real failures.
#   already daily -- covered by another scheduled task; running twice would
#     double-fetch the same snapshots into the same tables.
$skip = @(
    # metered keys
    "alpha_vantage", "alpha_vantage_fundamentals", "bls_expansion", "bls_oes_qcew",
    "eia", "eia_expansion", "eia_petng_prices", "eia_hourly_grid", "gas_prices",
    # known-dead sources
    "nasdaq_data_link", "usda", "congressional_trades",
    # failing every night as of 2026-09-29:
    #   schwab_portfolio needs the Schwab Trader API, which isn't enabled
    # (lda_lobbying and ibkr_borrow_fee were here briefly, both fixed the same
    # day: lda re-paged the whole year into a 30-min timeout; IBKR's ftp3 host
    # had stopped answering, and ftp2 works.)
    "schwab_portfolio",
    # already in ClaudeAuto-DailyAccumulators
    "tradingview", "short_interest", "finnhub_events"
) -join ","

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
Set-Location $repo
# cmd /c does the redirection: PS 5.1 would wrap python's stderr lines in
# NativeCommandError records and pollute the report
cmd /c "`"$py`" run_all.py --skip $skip > `"$report`" 2>&1"
$exit = $LASTEXITCODE

$summary = (Select-String -Path $report -Pattern "PASS.*FAIL.*SKIP" | Select-Object -Last 1).Line
if (-not $summary) { $summary = "no summary line - run_all.py crashed? (exit=$exit)" }

if ($exit -eq 0) {
    if (Test-Path $flag) { Remove-Item $flag -Force }
    $status = "OK"
} else {
    $status = "FAIL"
    $failed = (Select-String -Path $report -Pattern "^\s+FAILED:" | ForEach-Object { $_.Line.Trim() }) -join "`n"
    @(
        "Daily pipeline run FAILED on $stamp (exit=$exit).",
        "",
        $summary,
        "",
        $failed,
        "",
        "If the failures are all Schwab (stage 2), the refresh token has probably",
        "lapsed -- it expires every 7 days. Renew with:",
        "  `"$py`" scripts\schwab_reauth.py",
        "",
        "Full report: $report",
        "Re-run one pipeline by hand with:  `"$py`" run_all.py --only <name>"
    ) | Out-File $flag -Encoding utf8
}
Add-Content (Join-Path $reportDir "daily_pipelines_summary_log.txt") "$stamp | $status | $summary"
