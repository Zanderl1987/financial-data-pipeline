# Weekly data-quality report: runs validate.py, archives the full output, keeps a
# one-line-per-week rolling summary, and leaves a visible flag file on FAIL.
# Also runs coverage_audit.py (added 2026-09-10) -- checks commodity/FX pipelines
# against the closed upstream catalogs (FRED IMF PCPS, Frankfurter currencies) so
# a gap like the ones found/fixed 2026-09-10 (Uranium, Dubai/APSP crude, Silicon,
# 6 IMF agriculture series) surfaces automatically instead of needing another
# manual "check the pipelines for gaps" pass. Weekly cadence is deliberately loose
# for this one -- these catalogs change a few times a year at most -- but it rides
# along with the existing Monday task rather than earning its own schedule entry.
# Registered as Windows Scheduled Task "ClaudeAuto-PipelineQuality" (see AUTOMATION.md).

$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$py = "C:\ProgramData\anaconda3\python.exe"
$reportDir = Join-Path $repo "storage\quality_reports"
$flag = Join-Path $repo "QUALITY_FAIL.txt"
$covFlag = Join-Path $repo "COVERAGE_GAP.txt"
$stamp = Get-Date -Format "yyyy-MM-dd"
$report = Join-Path $reportDir "validate_$stamp.txt"
$covReport = Join-Path $reportDir "coverage_audit_$stamp.txt"

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

# --- coverage_audit.py: separate pass/fail signal, does not affect $flag above ---
cmd /c "`"$py`" coverage_audit.py --fail-on-gap > `"$covReport`" 2>&1"
$covExit = $LASTEXITCODE
$gapCount = (Select-String -Path $covReport -Pattern "^\d+ CONFIRMED GAP" | Select-Object -Last 1).Line
if (-not $gapCount) { $gapCount = "no gap-count line - coverage_audit.py crashed? (exit=$covExit)" }

if ($covExit -eq 0) {
    if (Test-Path $covFlag) { Remove-Item $covFlag -Force }
    $covStatus = "OK"
} else {
    $covStatus = "GAP"
    "Coverage audit found gap(s) on $stamp.`n$gapCount`nFull report: $covReport" |
        Out-File $covFlag -Encoding utf8
}
Add-Content (Join-Path $reportDir "coverage_audit_summary_log.txt") "$stamp | $covStatus | $gapCount"
