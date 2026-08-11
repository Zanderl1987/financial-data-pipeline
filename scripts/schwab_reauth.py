"""
Interactive Schwab re-authorization -- the 7-day chore, made verifiable.

Schwab refresh tokens expire every 7 days and renewing one requires a human:
schwabdev opens a browser, you log in, then paste the redirected callback URL
back. The auth code in that URL dies roughly 30 seconds after login.

The problem this script solves is not the flow itself, it is knowing whether
the flow WORKED. Constructing a Client by hand prints a wall of schwabdev log
lines and then returns whether or not a new token was stored, so a missed
paste window looks exactly like success. On 2026-08-11 a re-auth was believed
to have succeeded while tokens.db still held the token issued on 2026-08-01.

This script records the token state before, runs the flow, then re-reads the
store and proves the refresh token actually advanced -- and spends one live
API call confirming the new credentials work.

Usage (must be a REAL terminal -- it reads from stdin):
    cd C:\\Users\\zande\\PycharmProjects\\financial-data-pipeline
    C:\\ProgramData\\anaconda3\\python.exe scripts\\schwab_reauth.py
"""

import os
import sys

# Run from the repo root regardless of where the script was invoked, so the
# relative default tokens.db path can never resolve to another directory.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

import schwabdev  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from schwab_auth import check_refresh_token, token_path  # noqa: E402

load_dotenv()


def main() -> int:
    if not sys.stdin.isatty():
        print("ERROR: no terminal attached.", file=sys.stderr)
        print("This flow reads a pasted URL from stdin -- run it in a real terminal,", file=sys.stderr)
        print("not from a scheduled task, a pipe, or an agent session.", file=sys.stderr)
        return 2

    path = os.path.abspath(token_path())
    before = check_refresh_token(path)

    print(f"Repo:        {REPO_ROOT}")
    print(f"Token store: {path}")
    print(f"Current:     {before['state'].upper()} -- {before['detail']}")
    print()
    print("A browser window will open. Log in, approve, then copy the FULL url")
    print("from the address bar (it will look like an error page -- that is normal)")
    print("and paste it at the prompt. Do this promptly: the code expires in ~30s.")
    print("-" * 70)

    try:
        client = schwabdev.Client(
            app_key=os.environ["SCHWAB_API_KEY"],
            app_secret=os.environ["SCHWAB_APP_SECRET"],
            callback_url=os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"),
            tokens_db=os.environ.get("SCHWAB_TOKEN_PATH", "tokens.db"),
        )
    except KeyError as exc:
        print(f"\nFAILED: {exc} is not set in .env", file=sys.stderr)
        return 2
    except EOFError:
        # isatty() can report a terminal in environments (Git Bash, agent
        # sessions, some IDE consoles) where the read still hits EOF at once.
        print("\nFAILED: could not read the pasted url -- stdin closed immediately.", file=sys.stderr)
        print("  This shell cannot do an interactive paste. Use a real terminal:", file=sys.stderr)
        print("  Windows Terminal, PowerShell, or cmd launched from the Start menu.", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"\nFAILED: the authorization flow raised: {exc}", file=sys.stderr)
        return 1

    print("-" * 70)

    # The real check: did the stored refresh token actually advance?
    after = check_refresh_token(path)

    if after["state"] not in ("ok", "expiring"):
        print(f"\nFAILED: token store is still {after['state'].upper()}.", file=sys.stderr)
        print(f"  {after['detail']}", file=sys.stderr)
        print("  Nothing was saved. The usual cause is the ~30s code expiry --", file=sys.stderr)
        print("  have the url ready to paste and run this again.", file=sys.stderr)
        return 1

    if before["issued"] is not None and after["issued"] == before["issued"]:
        print("\nFAILED: the refresh token did not change.", file=sys.stderr)
        print(f"  Still the one issued {before['issued']:%Y-%m-%d %H:%M} UTC.", file=sys.stderr)
        print("  schwabdev reused the existing token instead of storing a new one.", file=sys.stderr)
        return 1

    # Prove the credentials actually work rather than trusting the file.
    try:
        resp = client.quotes(["AAPL"])
        live = resp.ok
        live_detail = f"HTTP {resp.status_code}"
    except Exception as exc:
        live = False
        live_detail = str(exc)

    print(f"\nToken stored: issued {after['issued']:%Y-%m-%d %H:%M} UTC")
    print(f"Valid until:  {after['expires']:%Y-%m-%d %H:%M} UTC")

    if not live:
        print(f"\nWARNING: a new token was saved but a live quote call failed ({live_detail}).", file=sys.stderr)
        return 1

    print("Live check:   OK (quote request succeeded)")
    print("\nSUCCESS -- the Schwab pipelines can run now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
