"""
One-shot, non-interactive Schwab re-authorization for agent sessions.

Mirrors scripts/schwab_reauth.py's verification (token advanced + live quote
call) but does NOT read the callback URL from stdin. The callback URL is passed
as a command-line argument and fed to schwabdev via the `call_on_auth` hook,
so an agent can exchange the code within the ~30s window the moment the user
pastes the redirect URL back.

Usage:
    C:\\ProgramData\\anaconda3\\python.exe scripts\\schwab_reauth_oneshot.py "<full callback url>"

Exit codes:
    0  success (new token stored, live quote OK)
    1  token not advanced, live check failed, or exchange raised
    2  usage / missing env
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

import schwabdev  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from schwab_auth import check_refresh_token, token_path  # noqa: E402

load_dotenv()


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: schwab_reauth_oneshot.py <full callback url>", file=sys.stderr)
        return 2

    callback = sys.argv[1].strip()
    if not callback.startswith("http"):
        print("ERROR: first argument does not look like a URL.", file=sys.stderr)
        return 2

    path = os.path.abspath(token_path())
    before = check_refresh_token(path)

    print(f"Repo:        {REPO_ROOT}")
    print(f"Token store: {path}")
    print(f"Before:      {before['state'].upper()} -- {before['detail']}")

    try:
        app_key = os.environ["SCHWAB_API_KEY"]
        app_secret = os.environ["SCHWAB_APP_SECRET"]
    except KeyError as exc:
        print(f"\nFAILED: {exc} is not set in .env", file=sys.stderr)
        return 2

    try:
        client = schwabdev.Client(
            app_key=app_key,
            app_secret=app_secret,
            callback_url=os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"),
            tokens_db=os.environ.get("SCHWAB_TOKEN_PATH", "tokens.db"),
            call_on_auth=lambda _url: callback,
            open_browser_for_auth=False,
        )
    except Exception as exc:
        print(f"\nFAILED: the authorization flow raised: {exc}", file=sys.stderr)
        return 1

    after = check_refresh_token(path)

    if after["state"] not in ("ok", "expiring"):
        print(f"\nFAILED: token store is still {after['state'].upper()}.", file=sys.stderr)
        print(f"  {after['detail']}", file=sys.stderr)
        print("  The ~30s code window likely closed before the exchange ran.", file=sys.stderr)
        return 1

    if before["issued"] is not None and after["issued"] == before["issued"]:
        print("\nFAILED: the refresh token did not change.", file=sys.stderr)
        print(f"  Still the one issued {before['issued']:%Y-%m-%d %H:%M} UTC.", file=sys.stderr)
        return 1

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
