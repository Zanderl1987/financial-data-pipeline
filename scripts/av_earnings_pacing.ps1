# AV DJI earnings-history pacing run: alpha_vantage_fundamentals_pipeline.py
# (incremental, default 20-request budget) timed for the ~10:05-10:30am gap,
# after the previous day's AV quota rolls off but before ClaudeAuto-TranscriptPull
# fires at 10:30 and spends the day's shared-IP quota on transcripts instead.
# Archives output, leaves a visible flag file if the run hit AV's rate-limit
# response with no real progress (quota already spent) or crashed outright.
# One-time task for 2026-08-01; not yet a recurring ClaudeAuto-* automation
# (see AUTOMATION.md before promoting it to one).

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
    "AV earnings pacing run on $stamp hit the daily rate limit -- likely no real progress.`n$summaryLine`nFull report: $report`nRetry in tomorrow's ~10:05am gap." |
        Out-File $flag -Encoding utf8
} else {
    if (Test-Path $flag) { Remove-Item $flag -Force }
    $status = "OK"
}
Add-Content (Join-Path $reportDir "av_earnings_pacing_summary_log.txt") "$stamp | $status | $summaryLine"
