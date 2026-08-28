"""
Shared Schwab OAuth preflight for the schwab_*/options_chain pipelines.

Why this exists: schwabdev's refresh token lasts 7 days. When it expires,
`schwabdev.Client(...)` logs "The refresh token has expired!", opens a browser
and then blocks on `input()` waiting for a pasted callback URL. Under the
"ClaudeAuto-DailyAccumulators" scheduled task there is no terminal, so it
blocks until run_all.py's per-pipeline timeout kills it. On 2026-08-09 and
2026-08-10 that burned 67 minutes a day across four pipelines and reported
only "timed out" -- the actual cause was one line in a log nobody reads.

check_refresh_token() reads the token store directly (SQLite, no network, no
Client construction) so a pipeline can fail in milliseconds with the real
reason and the exact command to fix it.

Usage:
    from schwab_auth import preflight
    preflight()                     # exits 2 if re-auth is needed
    client = schwabdev.Client(...)
"""

import datetime
import os
import sqlite3
import sys

# Schwab refresh tokens are valid for 7 days from issue. Not configurable at
# the app level -- it is a fixed property of the Schwab OAuth flow.
REFRESH_TOKEN_LIFETIME = datetime.timedelta(days=7)

# Warn (but still run) once a token is inside this much of expiry, so a run
# that is about to be the last good one says so while it can still succeed.
EXPIRY_WARNING_WINDOW = datetime.timedelta(hours=24)

# This banner is the last thing anyone reads before renewing a token, so it has
# to name the script that verifies its own work. It used to print a bare
# `schwabdev.Client(...)` one-liner, which returns normally whether or not a
# token was stored -- on 2026-08-11 a re-auth was believed to have succeeded
# while tokens.db still held the token issued ten days earlier. Losing the ~30s
# code window and winning it look identical through that one-liner.
# scripts/schwab_reauth.py re-reads the store, proves refresh_token_issued
# advanced, and spends one live quote call before claiming success.
REAUTH_COMMAND = (
    'C:\\ProgramData\\anaconda3\\python.exe scripts\\schwab_reauth.py'
)


def token_path() -> str:
    """The token store this repo's pipelines use, honoring SCHWAB_TOKEN_PATH."""
    return os.environ.get("SCHWAB_TOKEN_PATH", "tokens.db")


def check_refresh_token(path: str | None = None, now: datetime.datetime | None = None) -> dict:
    """
    Report the refresh token's state without constructing a Client.

    Returns a dict with 'state' in {'ok', 'expiring', 'expired', 'missing',
    'unreadable'} plus 'issued', 'expires' and 'detail'. Never raises and never
    returns the token value itself.
    """
    path = path or token_path()
    result = {"state": "missing", "issued": None, "expires": None, "path": path, "detail": ""}

    if not os.path.exists(path):
        result["detail"] = f"No Schwab token store at {path} -- never authorized on this machine."
        return result

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT refresh_token_issued FROM schwabdev").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        result["state"] = "unreadable"
        result["detail"] = f"Could not read {path}: {exc}"
        return result

    if not row or not row[0]:
        result["detail"] = f"{path} has no refresh_token_issued row."
        return result

    try:
        issued = datetime.datetime.fromisoformat(row[0])
    except ValueError as exc:
        result["state"] = "unreadable"
        result["detail"] = f"Unparseable refresh_token_issued in {path}: {exc}"
        return result

    expires = issued + REFRESH_TOKEN_LIFETIME
    now = now or datetime.datetime.now(issued.tzinfo)
    remaining = expires - now

    result.update(issued=issued, expires=expires)
    if remaining <= datetime.timedelta(0):
        result["state"] = "expired"
        result["detail"] = f"Refresh token expired {_ago(-remaining)} ago (issued {issued:%Y-%m-%d %H:%M} UTC)."
    elif remaining <= EXPIRY_WARNING_WINDOW:
        result["state"] = "expiring"
        result["detail"] = f"Refresh token expires in {_ago(remaining)} ({expires:%Y-%m-%d %H:%M} UTC)."
    else:
        result["state"] = "ok"
        result["detail"] = f"Refresh token valid for {_ago(remaining)} (expires {expires:%Y-%m-%d %H:%M} UTC)."
    return result


def _ago(delta: datetime.timedelta) -> str:
    """Human-readable duration, ASCII only (Windows cp1252 terminal)."""
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def preflight(
    path: str | None = None,
    exit_code: int = 2,
    now: datetime.datetime | None = None,
) -> dict:
    """
    Fail fast when Schwab needs an interactive re-auth.

    Exits with `exit_code` on any state that a non-interactive run cannot
    recover from. Returns the status dict when the run may proceed.
    """
    status = check_refresh_token(path, now=now)

    if status["state"] in ("expired", "missing", "unreadable"):
        print("SCHWAB AUTH REQUIRED -- skipping run (no data fetched).", file=sys.stderr)
        print(f"  {status['detail']}", file=sys.stderr)
        print("  Schwab re-auth is interactive and must be run by hand in a real terminal;", file=sys.stderr)
        print("  the auth code in the callback URL expires about 30 seconds after login.", file=sys.stderr)
        print(f"  Run from {os.getcwd()}:", file=sys.stderr)
        print(f"    {REAUTH_COMMAND}", file=sys.stderr)
        sys.exit(exit_code)

    if status["state"] == "expiring":
        print(f"WARNING: {status['detail']} Re-auth soon or the next run fails.")

    return status


if __name__ == "__main__":
    s = check_refresh_token()
    print(f"{s['state'].upper()}: {s['detail']}")
    sys.exit(0 if s["state"] in ("ok", "expiring") else 1)
