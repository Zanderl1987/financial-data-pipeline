# Nightly catch-up of daily price bars for the full ~27.7k-symbol universe
# (symbol_universe.csv), not just the watchlist that price_history_pipeline
# covers. Registered as Windows Scheduled Task "SchwabUniverseIncrementalPrices"
# (see docs/AUTOMATION.md).
#
# Chain: Schwab token preflight -> schwab_universe_backfill.py --incremental
#        -> curated.py --table prices -> upload_huggingface.py
#
# Rebuilt 2026-09-29. The original task ran a .bat kept in %TEMP%\opencode\,
# which was cleaned away; the job had in fact never written a single
# prices_incr_batch file since it was registered 2026-08-11, so universe
# coverage stopped at 2026-07-23 with nothing reporting it.
#
# The lookback window is derived from the last run that COMPLETED (state file
# below), not from the newest file on disk: a run killed halfway still writes
# today-dated batches, and measuring from those would leave the unfetched
# symbols with a permanent hole. Missed nights therefore self-heal.

$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$py = "C:\ProgramData\anaconda3\python.exe"
$reportDir = Join-Path $repo "storage\quality_reports"
$flag = Join-Path $repo "UNIVERSE_PRICES_FAIL.txt"
$state = Join-Path $reportDir "universe_prices_last_ok.txt"
$stamp = Get-Date -Format "yyyy-MM-dd"
$report = Join-Path $reportDir "universe_prices_$stamp.txt"
$summaryLog = Join-Path $reportDir "universe_prices_summary_log.txt"
# Last date the full universe was fetched before this job was rebuilt
# (newest prices_universe_batch file, max bar date 2026-07-23).
$fallbackLastOk = [datetime]"2026-07-23"
$minDays = 14
$overlapDays = 7
# Symbols that hit network errors 3x are retried on the next night's
# overlapping window; only a large count means something is actually wrong.
$maxFailed = 500

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
Set-Location $repo

function Fail($status, $detail) {
    "Universe price run FAILED on $stamp -- $status`n$detail`nFull report: $report" |
        Out-File $flag -Encoding utf8
    Add-Content $summaryLog "$stamp | FAIL | $status"
    exit 1
}

# 1. Token preflight. An expired refresh token makes schwabdev's refresh thread
#    thrash and has hard-crashed python (GIL abort) mid-run before; stop cleanly.
cmd /c "`"$py`" schwab_auth.py > `"$report`" 2>&1"
if ($LASTEXITCODE -ne 0) {
    Fail "SCHWAB RE-AUTH REQUIRED" (((Get-Content $report) -join "`n") + "`nRun by hand: `"$py`" scripts\schwab_reauth.py")
}

# 2. Fetch. Window = days since the last completed run + overlap.
$lastOk = $fallbackLastOk
if (Test-Path $state) { $lastOk = [datetime](Get-Content $state -TotalCount 1).Trim() }
$days = [int][math]::Ceiling(((Get-Date) - $lastOk).TotalDays) + $overlapDays
if ($days -lt $minDays) { $days = $minDays }
Add-Content $report "`n== backfill: --incremental --days $days (last completed run $($lastOk.ToString('yyyy-MM-dd')))"
cmd /c "`"$py`" -u schwab_universe_backfill.py --incremental --days $days --chunk-size 250 --skip-empty-from schwab_universe_backfill_progress.json >> `"$report`" 2>&1"
$exit = $LASTEXITCODE
$totals = (Select-String -Path $report -Pattern "^Total done: (\d+), empty: (\d+), failed: (\d+)" | Select-Object -Last 1)
if ($exit -ne 0 -or -not $totals) {
    Fail "backfill crashed (exit=$exit)" "No completion line. Re-running the task today resumes where it stopped (date-stamped progress file)."
}
$failed = [int]$totals.Matches[0].Groups[3].Value
if ($failed -gt $maxFailed) {
    Fail "$failed symbols failed with network errors" $totals.Line
}

# 3. Curate, then 4. publish. Upload with defaults, exactly as run_all.py's
#    sync_huggingface() does -- the dataset is public, and passing no --private
#    against a public repo leaves it public (see CLAUDE.md on --private).
Add-Content $report "`n== curated.py --table prices"
cmd /c "`"$py`" curated.py --table prices >> `"$report`" 2>&1"
if ($LASTEXITCODE -ne 0) { Fail "curated.py --table prices failed (exit=$LASTEXITCODE)" $totals.Line }

Add-Content $report "`n== upload_huggingface.py"
cmd /c "`"$py`" upload_huggingface.py >> `"$report`" 2>&1"
# upload_huggingface.py exits 0 even when it refuses to publish, so check for
# its success line instead of the exit code.
if (-not (Select-String -Path $report -Pattern "^Done! Dataset:" -Quiet)) {
    Fail "HuggingFace upload did not complete" "Data is fetched and curated locally; only the publish failed. $($totals.Line)"
}

$stamp | Out-File $state -Encoding ascii
if (Test-Path $flag) { Remove-Item $flag -Force }
Add-Content $summaryLog "$stamp | OK | days=$days | $($totals.Line)"
exit 0
