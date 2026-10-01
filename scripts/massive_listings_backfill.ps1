# Detached ~3-week backfill for the strike-introduction study.
# Checkpointed per symbol and windowed by storage/state/massive_listings/run.json,
# so re-running resumes where it stopped with the same date range.
# Launch:  Start-Process powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','<this file>' -WindowStyle Hidden
# Watch:   C:\ProgramData\anaconda3\python.exe -m streamlit run scripts/massive_backfill_dashboard.py --server.headless true
$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$py   = "C:\ProgramData\anaconda3\python.exe"
$log  = Join-Path $repo "storage\logs\massive_listings_backfill.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
Set-Location $repo
# Retry loop: a network outage or failed symbols (exit 2) re-run after 10 min;
# completed symbols are skipped via their checkpoints. Exit 3 = another backfill
# already holds the lock, so stop. Capped so a permanent failure can't spin forever.
for ($attempt = 1; $attempt -le 30; $attempt++) {
    Add-Content -Path $log -Value "=== attempt $attempt $(Get-Date -Format s) ==="
    cmd /c "`"$py`" -u massive_option_listings_pipeline.py >> `"$log`" 2>&1"
    $code = $LASTEXITCODE
    Add-Content -Path $log -Value "=== exit $code $(Get-Date -Format s) ==="
    if ($code -eq 0 -or $code -eq 3) { break }
    Start-Sleep -Seconds 600
}
