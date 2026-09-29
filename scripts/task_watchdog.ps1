# Watchdog for the scheduled jobs. Registered as Windows Scheduled Task
# "ClaudeAuto-TaskWatchdog", hourly (see docs/AUTOMATION.md).
#
# Why this exists: every other wrapper writes its own FAIL flag *after* python
# returns. When the wrapper process itself is killed -- e.g. 2026-09-28 15:11,
# when a wake from Modern Standby fired five catch-up tasks at once and four
# died ~20s later with 0xC000013A -- none of that code runs, so there is no
# flag, no summary-log row, and the failure is silent. The only record that
# survives is the task's LastTaskResult, so this reads it from outside.
#
# Writes SCHEDULED_TASK_FAIL.txt at the repo root while any job's last run
# failed or a job is overdue; deletes it when everything is clean. A job
# stays flagged until its next run succeeds.

$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$flag = Join-Path $repo "SCHEDULED_TASK_FAIL.txt"
$log = Join-Path $repo "storage\quality_reports\task_watchdog_log.txt"
$self = "ClaudeAuto-TaskWatchdog"
# A job whose next run is this far in the past while the machine is awake
# never fired (StartWhenAvailable normally starts it within minutes of wake).
$overdueGrace = New-TimeSpan -Hours 3

# Result codes that are not failures.
$ok = @(
    0,
    267009,  # 0x41301 task is currently running
    267011   # 0x41303 task has not run yet
)
$meaning = @{
    "1"          = "script exited 1 -- the job's own FAIL flag/report has details"
    "2"          = "script exited 2 -- usually SCHWAB AUTH REQUIRED; see the job's FAIL flag"
    "3221225786" = "0xC000013A killed -- the wrapper died before writing its flag/report. Two known causes: expired Schwab token crashing schwabdev (GIL abort, traceback in report), or a post-wake catch-up burst (report empty or just ^C)"
    "2147946720" = "0x800710E0 refused to start -- usually battery gating, see 'Battery gating' in docs/AUTOMATION.md"
    "267014"     = "0x41306 terminated (by a user, or by hitting ExecutionTimeLimit)"
    "2147750687" = "0x8004131F skipped -- previous instance still running (MultipleInstances=IgnoreNew)"
    "4294967295" = "-1 -- script crashed or was killed"
}

$now = Get-Date
$problems = @()
$tasks = Get-ScheduledTask | Where-Object {
    ($_.TaskName -like "ClaudeAuto-*" -or $_.TaskName -like "SchwabUniverse*") -and $_.TaskName -ne $self
}
foreach ($t in $tasks) {
    if ($t.State -eq "Disabled") { continue }
    $i = $t | Get-ScheduledTaskInfo
    $code = [int64]$i.LastTaskResult
    if ($code -lt 0) { $code += 4294967296 }  # show as unsigned, matching Task Scheduler UI
    if ($ok -notcontains $code) {
        $why = $meaning["$code"]  # string keys: an int64 never matches an int32 key
        if (-not $why) { $why = "exit code $code (0x{0:X})" -f $code }
        $problems += "{0}: last run {1:yyyy-MM-dd HH:mm} FAILED -- {2}" -f $t.TaskName, $i.LastRunTime, $why
    }
    if ($i.NextRunTime -and $t.State -ne "Running" -and ($now - $i.NextRunTime) -gt $overdueGrace) {
        $problems += "{0}: OVERDUE -- was due {1:yyyy-MM-dd HH:mm} and has not started" -f $t.TaskName, $i.NextRunTime
    }
}

$stamp = $now.ToString("yyyy-MM-dd HH:mm")
$hadFlag = Test-Path $flag
# Log only when the problem set changes, not every hour it persists.
$previous = @()
if ($hadFlag) { $previous = @(Get-Content $flag | Where-Object { $_ -match ": (last run|OVERDUE)" }) }
$changed = ($problems -join "`n") -ne ($previous -join "`n")
if ($problems.Count -gt 0) {
    @(
        "Scheduled-task watchdog found $($problems.Count) problem(s) at $stamp.",
        "A job stays listed until its next run succeeds.",
        ""
    ) + $problems + @(
        "",
        "Re-run a job:   Start-ScheduledTask <name>",
        "Check a job:    Get-ScheduledTask <name> | Get-ScheduledTaskInfo",
        "Reports:        $repo\storage\quality_reports\"
    ) | Out-File $flag -Encoding utf8
    if ($changed) { Add-Content $log "$stamp | FAIL | $($problems -join ' || ')" }
} elseif ($hadFlag) {
    Remove-Item $flag -Force
    Add-Content $log "$stamp | OK | all jobs clean, flag cleared"
}
