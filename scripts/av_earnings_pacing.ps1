# AV DJI earnings-history pacing run: alpha_vantage_fundamentals_pipeline.py
# (incremental, default 20-request budget). Scheduled for 10:45am, deliberately
# AFTER earnings_sentiment_tool's ClaudeAuto-TranscriptPull fires at 10:30 -- the
# 10:05am "before" slot tried on 07-31/08-01 both failed (full quota, zero
# progress) because AV's quota is rolling-24h, not midnight-reset: yesterday's
# ~10:30 transcript-pull usage doesn't roll off until ~10:30 today, so a 10:05
# run still saw it as unexpired. Firing at 10:45 (after TranscriptPull's own
# run, not before it) is correct relative to the rolling window, though it
# still won't get real progress until earnings_sentiment_tool's 725-file
# transcript cache completes (ETA ~2026-08-06 per that repo's CLAUDE.md) and
# its daily pull becomes a zero-quota no-op -- until then this will legitimately
# keep reporting QUOTA below, which is expected, not a bug. Registered as
# recurring Windows Scheduled Task "ClaudeAuto-AVEarningsPacing" (see
# AUTOMATION.md). Archives output, leaves a visible flag file if the run hit
# AV's rate-limit response with no real progress or crashed outright.

$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$py = "C:\ProgramData\anaconda3\python.exe"
$reportDir = Join-Path $repo "storage\quality_reports"
$flag = Join-Path $repo "AV_EARNINGS_PACING_FAIL.txt"
$stamp = Get-Date -Format "yyyy-MM-dd"
$report = Join-Path $reportDir "av_earnings_pacing_$stamp.txt"

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
Set-Location $repo
# cmd /c does the redirection: PS 5.1 would wrap python's stderr lines in
# NativeCommandError records and pollute the report
cmd /c "`"$py`" alpha_vantage_fundamentals_pipeline.py > `"$report`" 2>&1"
$exit = $LASTEXITCODE

$summaryLine = (Select-String -Path $report -Pattern "request\(s\) used" | Select-Object -Last 1).Line
if (-not $summaryLine) { $summaryLine = "no summary line - pipeline crashed? (exit=$exit)" }
$rateLimited = (Select-String -Path $report -Pattern "standard API rate limit is 25 requests per day" -Quiet)

if ($exit -ne 0) {
    $status = "FAIL"
    "AV earnings pacing run CRASHED on $stamp (exit=$exit).`n$summaryLine`nFull report: $report" |
        Out-File $flag -Encoding utf8
} elseif ($rateLimited) {
    $status = "QUOTA"
    "AV earnings pacing run on $stamp hit the daily rate limit -- likely no real progress.`n$summaryLine`nFull report: $report`nExpected until earnings_sentiment_tool's transcript cache completes (~2026-08-06); retries automatically tomorrow at 10:45am." |
        Out-File $flag -Encoding utf8
} else {
    if (Test-Path $flag) { Remove-Item $flag -Force }
    $status = "OK"
}
Add-Content (Join-Path $reportDir "av_earnings_pacing_summary_log.txt") "$stamp | $status | $summaryLine"
