# Detached ~3-week backfill for the strike-introduction study.
# Checkpointed per symbol: re-running this resumes where it stopped.
# Launch:  Start-Process powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','<this file>' -WindowStyle Hidden
# Watch:   C:\ProgramData\anaconda3\python.exe -m streamlit run scripts/massive_backfill_dashboard.py
$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$py   = "C:\ProgramData\anaconda3\python.exe"
$log  = Join-Path $repo "storage\logs\massive_listings_backfill.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
Set-Location $repo
cmd /c "`"$py`" -u massive_option_listings_pipeline.py >> `"$log`" 2>&1"
