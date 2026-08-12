# Daily snapshot-only accumulators: history for these tables only exists if
# pulled daily (missed days are permanent holes, no backfill exists). Runs
# run_all.py --only for exactly the accumulator set, archives output, leaves
# a visible flag file on FAIL. Registered as Windows Scheduled Task
# "ClaudeAuto-DailyAccumulators" (see AUTOMATION.md).
#
# schwab_quotes/intraday/options/movers added 2026-08-02 alongside the
# S&P 500 coverage expansion (see memory/SESSION_NOTES) -- unlike the
# original 3, these depend on Schwab OAuth, which needs a fresh interactive
# login roughly every 7 days (refresh token expiry). A FAIL here with a
# "refresh token has expired" message in the archived report means Zander
# needs to re-authenticate in a real terminal, not a pipeline bug.

$repo = "C:\Users\zande\PycharmProjects\financial-data-pipeline"
$py = "C:\ProgramData\anaconda3\python.exe"
$reportDir = Join-Path $repo "storage\quality_reports"
$flag = Join-Path $repo "DAILY_ACCUMULATOR_FAIL.txt"
$stamp = Get-Date -Format "yyyy-MM-dd"
$report = Join-Path $reportDir "accumulators_$stamp.txt"

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
Set-Location $repo
# cmd /c does the redirection: PS 5.1 would wrap python's stderr lines in
# NativeCommandError records and pollute the report
cmd /c "`"$py`" run_all.py --only tradingview,short_interest,finnhub_events,schwab_quotes,schwab_intraday,schwab_options,schwab_movers > `"$report`" 2>&1"
$exit = $LASTEXITCODE

$summary = (Select-String -Path $report -Pattern "PASS.*FAIL.*SKIP" | Select-Object -Last 1).Line
if (-not $summary) { $summary = "no summary line - run_all.py crashed? (exit=$exit)" }

if ($exit -eq 0) {
    if (Test-Path $flag) { Remove-Item $flag -Force }
    $status = "OK"
} else {
    # An expired Schwab refresh token is not a pipeline bug -- it needs an
    # interactive login only Zander can do. schwab_auth.preflight() makes the
    # Schwab pipelines exit 2 with this banner instead of hanging on input()
    # until the timeout, so the flag can name the actual fix.
    $authNeeded = Select-String -Path $report -Pattern "SCHWAB AUTH REQUIRED" -Quiet
    if ($authNeeded) {
        $status = "FAIL (schwab re-auth needed)"
        # Scrape the remediation line out of the report so this stays in step with
        # schwab_auth.REAUTH_COMMAND instead of duplicating it. Match on the script
        # name; the previous pattern was "schwabdev\.Client", which silently yielded
        # an EMPTY command the moment that banner stopped naming schwabdev -- a flag
        # file whose whole purpose is to tell you what to run, telling you nothing.
        # The literal fallback means that can never happen again.
        $reauth = (Select-String -Path $report -Pattern "schwab_reauth\.py" | Select-Object -First 1).Line
        if (-not $reauth) { $reauth = "`"$py`" scripts\schwab_reauth.py" }
        @(
            "Daily accumulator run FAILED on $stamp (exit=$exit) -- SCHWAB RE-AUTH REQUIRED.",
            "The Schwab refresh token expires every 7 days and renewing it is interactive.",
            "Run this by hand in a real terminal (the auth code expires ~30s after login):",
            "",
            "  cd $repo",
            "  $reauth",
            "",
            "Then re-run: `"$py`" run_all.py --only schwab_quotes,schwab_intraday,schwab_options,schwab_movers",
            "",
            $summary,
            "Full report: $report"
        ) | Out-File $flag -Encoding utf8
    } else {
        $status = "FAIL"
        "Daily accumulator run FAILED on $stamp (exit=$exit).`n$summary`nFull report: $report" |
            Out-File $flag -Encoding utf8
    }
}
Add-Content (Join-Path $reportDir "accumulators_summary_log.txt") "$stamp | $status | $summary"
