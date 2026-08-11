# Daily stage-1 refresh: every free/public stage-1 pipeline except the metered
# and known-dead ones. Archives output, keeps a rolling one-line-per-day summary,
# leaves a visible flag file on FAIL. Registered as Windows Scheduled Task
# "ClaudeAuto-DailyStage1" (see AUTOMATION.md).
#
# WHY THIS EXISTS (2026-08-11). Before this job, the only scheduled fetching was
# ClaudeAuto-DailyAccumulators -- seven pipelines. Everything else refreshed only
# when a human happened to run run_all.py, so tables drifted stale in clusters
# dated to whenever the last manual run touched them: cot 56 days,
# alpha_vantage_forex/technical 49, bls_* 27, eia_*/fred_rates_gdp/
# treasury_exchange_rates 19. None of those pipelines were broken. Nothing ran them.
#
# WHY --stage 1 --skip AND NOT --only <list>. A hand-maintained --only list is the
# same defect one level up: add a stage-1 pipeline, forget the list, and it is
# silently never scheduled -- exactly how six sources went dark for six weeks
# before 891d97d. --stage 1 picks up new pipelines automatically. The only
# hand-maintained part is the skip list, which names things that are known bad,
# and that changes far more slowly.
#
# Runtime: a full 86-pipeline run measures ~173 min. This subset is ~59 pipelines
# after env skips, so budget 2-2.5 h. Scheduled 03:00 so it is well clear of
# DailyAccumulators (09:00), TranscriptPull (10:30) and AVEarningsPacing (10:45).

$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$py = "C:\ProgramData\anaconda3\python.exe"
$reportDir = Join-Path $repo "storage\quality_reports"
$flag = Join-Path $repo "STAGE1_FAIL.txt"
$stamp = Get-Date -Format "yyyy-MM-dd"
$report = Join-Path $reportDir "stage1_$stamp.txt"

# Excluded from the daily run, with the reason each one is out:
#   metered keys -- a daily burn would starve the jobs that need the quota.
#     Alpha Vantage is 25 req/day/key and is reserved for ClaudeAuto-AVEarningsPacing.
#     BLS keyless is 25/day. EIA is metered per key.
#   known-dead    -- documented in CLAUDE.md "Known-broken / dead ends"; these
#     fail every run and would keep the job permanently red, hiding real failures.
#   already daily -- covered by ClaudeAuto-DailyAccumulators; running twice would
#     double-fetch the same snapshots into the same tables.
$skip = @(
    # metered keys
    "alpha_vantage_fundamentals", "bls_expansion", "bls_oes_qcew",
    "eia", "eia_expansion", "eia_petng_prices", "eia_hourly_grid", "gas_prices",
    # known-dead sources
    "nasdaq_data_link", "usda", "trade", "congressional_trades",
    # already in ClaudeAuto-DailyAccumulators
    "tradingview", "short_interest", "finnhub_events"
) -join ","

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
Set-Location $repo
# cmd /c does the redirection: PS 5.1 would wrap python's stderr lines in
# NativeCommandError records and pollute the report
cmd /c "`"$py`" run_all.py --stage 1 --skip $skip > `"$report`" 2>&1"
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
        "Daily stage-1 run FAILED on $stamp (exit=$exit).",
        "",
        $summary,
        "",
        $failed,
        "",
        "Full report: $report",
        "Re-run one pipeline by hand with:  `"$py`" run_all.py --only <name>"
    ) | Out-File $flag -Encoding utf8
}
Add-Content (Join-Path $reportDir "stage1_summary_log.txt") "$stamp | $status | $summary"
