# Weekly refresh of the public HuggingFace financial-fundamentals dataset. Without
# this, the dataset goes stale silently (it sat at 2026-06-15 for ~7 weeks before
# being caught and manually re-run on 2026-08-04). Runs a full-market EDGAR pull
# (extraction only; the pipeline no longer pushes raw files to HF), rebuilds
# curated, assembles + pushes the whole snapshot in one coherent revision via
# build_fundamentals_dataset.py, then verifies the HF push actually landed via
# verify_hf.py before declaring success. Registered as Windows Scheduled Task
# "ClaudeAuto-FundamentalsHFRefresh" (see AUTOMATION.md).

$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$py = "C:\ProgramData\anaconda3\python.exe"
$reportDir = Join-Path $repo "storage\quality_reports"
$flag = Join-Path $repo "FUNDAMENTALS_HF_FAIL.txt"
$stamp = Get-Date -Format "yyyy-MM-dd"
$report = Join-Path $reportDir "fundamentals_hf_$stamp.txt"

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
Set-Location $repo
# cmd /c does the redirection: PS 5.1 would wrap python's stderr lines in
# NativeCommandError records and pollute the report
cmd /c "`"$py`" fundamentals_pipeline.py --full-market > `"$report`" 2>&1"
$pipelineExit = $LASTEXITCODE

cmd /c "`"$py`" curated.py >> `"$report`" 2>&1"
$curatedExit = $LASTEXITCODE

cmd /c "`"$py`" build_fundamentals_dataset.py >> `"$report`" 2>&1"
$buildExit = $LASTEXITCODE

cmd /c "`"$py`" verify_hf.py --repo ZanderL1337/financial-fundamentals >> `"$report`" 2>&1"
$verifyExit = $LASTEXITCODE

$exit = [Math]::Max([Math]::Max([Math]::Max($pipelineExit, $curatedExit), $buildExit), $verifyExit)

$summary = (Select-String -Path $report -Pattern "^(VERIFY PASS|VERIFY FAIL)$" | Select-Object -Last 1).Line
if (-not $summary) { $summary = "no VERIFY line - a step crashed before verify_hf.py ran? (pipeline=$pipelineExit curated=$curatedExit build=$buildExit verify=$verifyExit)" }

if ($exit -eq 0) {
    if (Test-Path $flag) { Remove-Item $flag -Force }
    $status = "OK"
} else {
    $status = "FAIL"
    "Fundamentals HF refresh FAILED on $stamp (pipeline=$pipelineExit curated=$curatedExit build=$buildExit verify=$verifyExit).`n$summary`nFull report: $report" |
        Out-File $flag -Encoding utf8
}
Add-Content (Join-Path $reportDir "fundamentals_hf_summary_log.txt") "$stamp | $status | $summary"
