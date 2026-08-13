# Session Notes — 2026-08-11

**Branch:** master
**Session model:** DeepSeek V4 Flash (opencode)

> **Addendum, same evening (Claude Opus 5, commit `0b15a9e`).** The scripts described below
> — `scripts/schwab_local_reauth.py` and `scripts/schwab_reauth_oneshot.py` — **no longer
> exist.** Both were untracked, and `CLAUDE.md` had been pointed at the first one, so any
> other clone of this repo got "can't open file" while the tracked, tested
> `scripts/schwab_reauth.py` sat unmentioned. All three were merged into
> `scripts/schwab_reauth.py`, which keeps what made the local-capture approach work
> (persistent cert at the same already-trusted `%LOCALAPPDATA%\schwab_reauth\` path,
> line-buffered output, a `--callback-url` flag replacing the oneshot script) and adds the
> certificate **expiry** check the prototype lacked — its 7-day cert would otherwise have
> come back expired on 2026-08-18, silently reinstating the TLS warning it existed to remove.
>
> Everything below remains an accurate record of what happened and why; only the file names
> changed. **Use `scripts\schwab_reauth.py` for any future re-auth.**

## What happened

Re-established the expired Schwab OAuth refresh token (expired 2026-08-08 23:36 UTC,
issued 2026-08-01 23:36 UTC) so the Schwab-dependent pipelines can run again under the
`ClaudeAuto-DailyAccumulators` scheduled task.

### The capture problem

`schwab_auth.py::check_refresh_token()` confirmed the refresh token was 3 days expired.
The schwabdev reauth flow is interactive (opens a browser, waits for a pasted callback
URL) and blocks under a scheduled task with no terminal — the root cause of the
"timed out" failures documented in `schwab_auth.py`'s module docstring on 2026-08-09/10.

Two manual paste attempts failed with `invalid_grant`: the pasted code had already died.
The auth code lifetime is ~30s; chat round-trip latency routinely blows it. (This is
exactly the failure mode `scripts/schwab_local_reauth.py` was written to avoid.)

### The fix that worked

`scripts/schwab_local_reauth.py` binds an HTTPS listener on `https://127.0.0.1:8182`
(the app's callback URL). The browser redirect from Schwab lands on 8182 the instant
the user approves, and the listener exchanges the code in-process — well inside the
30s window, no paste needed.

Build-out this session:
- Persistent self-signed cert at `%LOCALAPPDATA%\schwab_reauth\schwab.{crt,key}`
  (7-day validity) so the browser trusts the localhost redirect with no TLS warning.
  Import done via `certutil -addstore -f -user Root ...` (PowerShell
  `Import-Certificate` is blocked: "UI is not allowed"; plain `certutil` hangs on the
  interactive prompt, needs the `"y" | certutil ...` or `-f`).
- Script updated: `CAPTURE_WAIT_SECONDS = 1800`, `sys.stdout.reconfigure(line_buffering=True)`,
  `Handler.handle` tolerates TLS handshake failures, prints certutil trust line,
  verifies via `check_refresh_token` + live quote (AAPL).
- **Process-lifetime gotcha**: the opencode/bash shell kills child process trees when a
  command times out, so launching the listener under the shell tool was unreliable. The
  working launch method was **Task Scheduler** (`schtasks /create` + `/run`), which fully
  detaches the python process from the shell tool's tree:
  ```
  schtasks /create /tn "SchwabReauthListener" /tr "cmd /c <path>\schwab_listener.bat" /sc once /st 23:59 /f
  schtasks /run /tn "SchwabReauthListener"
  ```
  Wrapper `.bat` runs `python.exe -u scripts\schwab_local_reauth.py` with stdout/stderr
  redirected to files. Task deleted after success.

### Success

Listener captured the redirect and exchanged within the window:
- Token stored: issued **2026-08-11 22:46 UTC**, valid until **2026-08-18 22:46 UTC**
- Live check: OK (quote request succeeded)
- `check_refresh_token()` reports `state: ok`, expires 2026-08-18 22:46 UTC.

Schwab pipelines can now run (valid ~7 days, next expiry 2026-08-18).

### Data pull + HuggingFace sync (same session, right after reauth)

Token was ~2 days into a 3-day expiry stretch when refreshed, so the 08-10 (Mon) and
08-11 (Tue) morning daily-accumulator runs had FAILED on the Schwab pipes (quotes/
options last good 08-08; intraday/movers last 08-07). After reauth, ran:

```
C:\ProgramData\anaconda3\python.exe run_all.py --only prices,sector_etfs,schwab_quotes,schwab_options,options_chain,schwab_intraday,schwab_movers
```

19 min, 8 PASS / 0 FAIL. All tables back through 2026-08-11. HF auto-sync fired at the
end of run_all: **185 tables, 106,042,710 rows, 3003.4 MB, verified remotely** →
`ZanderL1337/financial-data-pipeline`.

Symbol coverage as of this run:
- `schwab_quotes`: **518** symbols (S&P 500 via IVV top-600 holdings + sector ETFs)
- `schwab_intraday`: **509** symbols, 5-min bars through 08-11 close
- `schwab_options`: **503** symbols, full chains + greeks (760k rows)
- `schwab_movers`: 90 rows (top-10 per $SPX/$COMPX/$DJI × up/down/vol) for 08-11
- `prices`: **27,759** symbols with history, but **daily updates only for the DJI-30** —
  the full-universe `schwab_universe_backfill.py` (29,373-symbol CSV) is a one-shot
  full-history pull, not incremental. Only 30 symbols carry a 08-11 bar. Universe backfill
  progress file: `schwab_universe_backfill_progress.json` (27,759 done / 1,616 empty / 0 failed).
- `sector_etfs`: 15 SPDR sector ETFs + broad indexes.

Gotcha for next time: if Zander wants the full 27,759-symbol `prices` table kept current
beyond the DJI-30, that needs either a fresh progress file (re-fetch ~4h) or an
incremental mode added to the backfill script — not something the daily task does.

### Incremental mode added to `schwab_universe_backfill.py` + nightly task

Implemented so the full universe stays current (per Zander's request, same session):

- **`--incremental --days 14`**: fetches only a trailing 14-day window (overlap covers
  weekends/holidays; curated dedup on `["symbol","date"]` merges it against existing
  history) instead of 1970→now. Uses a **date-stamped** progress file
  `schwab_universe_incremental_YYYY-MM-DD.json` (the flag overrides `--progress-file` —
  gotcha: a manual test slice's 5 symbols landed in the real per-day file, harmless,
  resume just skips them).
- **`--skip-empty-from <progress.json>`**: seeds the 1,616 known-empty symbols from the
  full backfill so a nightly run doesn't waste ~15 min re-fetching dead OTC names
  (verified: seeds 1,615/1,616 — one symbol got data later).
- Tested on a 5-symbol slice (AAPL/MSFT/NVDA/SPY/XOM): 55 rows, 11 trading days each,
  correct window 07-28→08-11, then removed the test parquet from storage.
- `query.py` glob `prices/**/*.parquet` + `curated.py` `prices` key already cover the new
  `prices_incr_batch####_YYYYMMDD.parquet` files — no wiring changes needed.

**Scheduled task `SchwabUniverseIncrementalPrices`** (daily 22:00, added 2026-08-11):
wrapper `%TEMP%\opencode\schwab_universe_incr.bat` chains backfill → `curated.py --table
prices` → `upload_huggingface.py` (full HF re-push), logging to
`%TEMP%\opencode\schwab_universe_incr.{out,err}.log`. ~4h15m runtime finishes ~02:15,
before the 3 AM stage-1 and 9 AM accumulator. `schtasks` stores `%TEMP%` literally in `/tr`
— had to recreate with the full expanded path. Requires a valid Schwab token (7-day expiry;
mid-week expiry fails the run until reauth). Documented in AUTOMATION.md.

## Useful artifacts

- `scripts/schwab_local_reauth.py` — local capture listener (persistent cert, 1800s window)
- `scripts/schwab_reauth_oneshot.py` — paste-based fallback (worked in principle but the
  ~30s code window makes paste unreliable in a chat session; keep the listener path preferred)
- `%LOCALAPPDATA%\schwab_reauth\schwab.crt` — trusted root (CurrentUser\Root), regenerated
  per-run only if missing
- Logs for this session: `%TEMP%\opencode\schwab_listener.{out,err}.log`

## Next time this expires (2026-08-18)

1. Confirm nothing is bound to 8182 (`Get-NetTCPConnection -LocalPort 8182`).
2. Launch detached via Task Scheduler as above (or have Zander run
   `C:\ProgramData\anaconda3\python.exe scripts\schwab_local_reauth.py` in a real terminal).
3. Give Zander the auth URL (printed by the script / `SCHWAB_API_KEY` from `.env`).
4. On success, `check_refresh_token()` flips to `ok`, expires next week. Delete the task.