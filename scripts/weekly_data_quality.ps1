# Weekly data-quality report: runs validate.py, archives the full output, keeps a
# one-line-per-week rolling summary, and leaves a visible flag file on FAIL.
# Registered as Windows Scheduled Task "ClaudeAuto-PipelineQuality" (see AUTOMATION.md).

$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$py = "C:\ProgramData\anaconda3\python.exe"
$reportDir = Join-Path $repo "storage\quality_reports"
$flag = Join-Path $repo "QUALITY_FAIL.txt"
$stamp = Get-Date -Format "yyyy-MM-dd"
$report = Join-Path $reportDir "validate_$stamp.txt"

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
Set-Location $repo
# cmd /c does the redirection: PS 5.1 would wrap python's stderr lines in
# NativeCommandError records and pollute the report
cmd /c "`"$py`" validate.py > `"$report`" 2>&1"
$exit = $LASTEXITCODE

# validate.py prints: "Summary: N PASS  |  N FAIL  |  N NO DATA" and exits 1 on any FAIL
$summary = (Select-String -Path $report -Pattern "^Summary:" | Select-Object -Last 1).Line
if (-not $summary) { $summary = "no Summary line - validate.py crashed? (exit=$exit)" }

if ($exit -eq 0) {
    if (Test-Path $flag) { Remove-Item $flag -Force }
    $status = "OK"
} else {
    $status = "FAIL"
    "Weekly data-quality check FAILED on $stamp (exit=$exit).`n$summary`nFull report: $report" |
        Out-File $flag -Encoding utf8
}
Add-Content (Join-Path $reportDir "summary_log.txt") "$stamp | $status | $summary"
