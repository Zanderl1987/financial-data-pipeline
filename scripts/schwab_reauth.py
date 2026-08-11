"""
Interactive Schwab re-authorization -- the 7-day chore, made verifiable.

Schwab refresh tokens expire every 7 days and renewing one requires a human to
log in through a browser. Schwab then redirects to the registered callback
(https://127.0.0.1:8182) carrying an authorization code that is good for only
about 30 seconds.

Two problems this script solves:

1. THE 30-SECOND WINDOW. schwabdev's stock flow asks you to copy the address
   bar out of a browser error page and paste it at a prompt. That is a human
   race against a 30s timer, and losing it looks exactly like winning (see 2).
   By default this script runs a throwaway HTTPS server on the callback
   address and catches the redirect itself, so no paste happens at all.

2. SILENT FAILURE. Constructing a Client by hand prints a wall of schwabdev
   log lines and returns normally whether or not a token was stored. On
   2026-08-11 a re-auth was believed to have succeeded while tokens.db still
   held the token issued on 2026-08-01. This script re-reads the store
   afterward and proves refresh_token_issued actually advanced, then spends
   one live API call confirming the credentials work.

The listener uses a self-signed certificate, so the browser will warn once
("Your connection is not private"). Click Advanced -> Proceed. That warning is
expected: nothing but your own machine is on the other end.

Usage (a real terminal -- the browser must be able to reach 127.0.0.1):
    cd C:\\Users\\zande\\PycharmProjects\\financial-data-pipeline
    C:\\ProgramData\\anaconda3\\python.exe scripts\\schwab_reauth.py

    --paste    skip the listener and paste the url by hand (fallback)
    --timeout  seconds to wait for the redirect (default 180)
"""

import argparse
import datetime
import http.server
import os
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import urllib.parse
import webbrowser

# Run from the repo root regardless of where the script was invoked, so the
# relative default tokens.db path can never resolve to another directory.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

import schwabdev  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from schwab_auth import check_refresh_token, token_path  # noqa: E402

load_dotenv()

BROWSER_PAGE = b"""<!doctype html>
<html><head><title>Schwab authorization received</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto;">
<h2>Authorization received</h2>
<p>The token exchange is running in your terminal. You can close this tab.</p>
</body></html>
"""


def _self_signed_cert(directory: str, host: str) -> tuple[str, str]:
    """
    Write a throwaway cert/key for `host` into `directory`.

    Schwab's callback is https, so the listener needs TLS. Nothing trusts this
    certificate and nothing should -- it exists for one redirect from the
    local browser to the local process, then it is deleted.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)

    alt_names = [x509.DNSName("localhost")]
    try:
        import ipaddress

        alt_names.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        alt_names.append(x509.DNSName(host))

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_path = os.path.join(directory, "cert.pem")
    key_path = os.path.join(directory, "key.pem")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    return cert_path, key_path


class _CallbackCatcher:
    """One-shot HTTPS listener that captures Schwab's redirect."""

    def __init__(self, callback_url: str):
        parsed = urllib.parse.urlparse(callback_url)
        self.callback_url = callback_url.rstrip("/")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 443
        self.captured: str | None = None
        self._event = threading.Event()
        self._httpd: http.server.HTTPServer | None = None
        self._tmpdir: str | None = None

    def __enter__(self):
        catcher = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib naming
                # Browsers also request /favicon.ico; only the redirect
                # carrying a code should end the wait.
                query = urllib.parse.urlparse(self.path).query
                if "code" not in urllib.parse.parse_qs(query):
                    self.send_response(204)
                    self.end_headers()
                    return
                catcher.captured = f"{catcher.callback_url}{self.path}"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(BROWSER_PAGE)))
                self.end_headers()
                self.wfile.write(BROWSER_PAGE)
                catcher._event.set()

            def log_message(self, *args):  # keep the console clean
                pass

        self._tmpdir = tempfile.mkdtemp(prefix="schwab_reauth_")
        cert, key = _self_signed_cert(self._tmpdir, self.host)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)

        self._httpd = http.server.HTTPServer((self.host, self.port), Handler)
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def wait(self, timeout: float) -> str | None:
        self._event.wait(timeout)
        return self.captured

    def __exit__(self, *exc):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        return False


def _port_is_free(host: str, port: int) -> bool:
    probe = socket.socket()
    try:
        return probe.connect_ex((host, port)) != 0
    finally:
        probe.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-authorize Schwab and verify it worked")
    parser.add_argument("--paste", action="store_true",
                        help="skip the listener and paste the callback url by hand")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="seconds to wait for the redirect (default 180)")
    args = parser.parse_args()

    callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
    path = os.path.abspath(token_path())
    before = check_refresh_token(path)

    print(f"Repo:        {REPO_ROOT}")
    print(f"Token store: {path}")
    print(f"Callback:    {callback_url}")
    print(f"Current:     {before['state'].upper()} -- {before['detail']}")
    print()

    try:
        client = _authorize(callback_url, before, args)
    except _AuthAborted as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return exc.code

    print("-" * 70)

    # The real check: did the stored refresh token actually advance?
    after = check_refresh_token(path)

    if after["state"] not in ("ok", "expiring"):
        print(f"\nFAILED: token store is still {after['state'].upper()}.", file=sys.stderr)
        print(f"  {after['detail']}", file=sys.stderr)
        print("  Nothing was saved -- the authorization code most likely expired.", file=sys.stderr)
        return 1

    if before["issued"] is not None and after["issued"] == before["issued"]:
        print("\nFAILED: the refresh token did not change.", file=sys.stderr)
        print(f"  Still the one issued {before['issued']:%Y-%m-%d %H:%M} UTC.", file=sys.stderr)
        return 1

    # Prove the credentials work rather than trusting the file.
    try:
        resp = client.quotes(["AAPL"])
        live, live_detail = resp.ok, f"HTTP {resp.status_code}"
    except Exception as exc:
        live, live_detail = False, str(exc)

    print(f"\nToken stored: issued {after['issued']:%Y-%m-%d %H:%M} UTC")
    print(f"Valid until:  {after['expires']:%Y-%m-%d %H:%M} UTC")

    if not live:
        print(f"\nWARNING: a token was saved but a live quote call failed ({live_detail}).",
              file=sys.stderr)
        return 1

    print("Live check:   OK (quote request succeeded)")
    print("\nSUCCESS -- the Schwab pipelines can run now.")
    return 0


class _AuthAborted(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _authorize(callback_url: str, before: dict, args) -> "schwabdev.Client":
    """Run the browser flow and return a constructed Client."""
    parsed = urllib.parse.urlparse(callback_url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 443

    use_listener = not args.paste
    if use_listener and not _port_is_free(host, port):
        print(f"NOTE: {host}:{port} is already in use -- falling back to manual paste.")
        print("      (Close whatever is holding the port to use the automatic flow.)")
        use_listener = False

    if not use_listener:
        return _authorize_by_paste(callback_url)

    print("A browser window will open. Log in and approve.")
    print("Your browser will warn about the certificate -- that is expected;")
    print("this listener is your own machine. Click Advanced, then Proceed.")
    print(f"Waiting up to {args.timeout:.0f}s for the redirect...")
    print("-" * 70)

    try:
        catcher_cm = _CallbackCatcher(callback_url)
        with catcher_cm as catcher:
            def call_on_auth(auth_url: str) -> str:
                print(f"[open] {auth_url}")
                try:
                    webbrowser.open(auth_url)
                except Exception:
                    print("Could not open a browser automatically -- open the url above.")
                captured = catcher.wait(args.timeout)
                if not captured:
                    raise _AuthAborted(
                        f"no redirect arrived within {args.timeout:.0f}s.\n"
                        "  If you did log in, the browser may have blocked the self-signed\n"
                        "  certificate outright. Re-run with --paste to do it by hand."
                    )
                print("[caught] redirect received, exchanging for tokens...")
                return captured

            return _build_client(callback_url, call_on_auth)
    except OSError as exc:
        raise _AuthAborted(
            f"could not start the local listener on {host}:{port} ({exc}).\n"
            "  Re-run with --paste to do it by hand."
        ) from exc


def _authorize_by_paste(callback_url: str) -> "schwabdev.Client":
    print("A browser window will open. Log in, approve, then copy the FULL url")
    print("from the address bar (it will look like an error page -- that is normal)")
    print("and paste it at the prompt. The code expires in ~30s.")
    print("-" * 70)
    try:
        return _build_client(callback_url, None)
    except EOFError as exc:
        # isatty() can report a terminal in environments (Git Bash, agent
        # sessions, some IDE consoles) where the read still hits EOF at once.
        raise _AuthAborted(
            "could not read the pasted url -- stdin closed immediately.\n"
            "  This shell cannot do an interactive paste. Use a real terminal:\n"
            "  Windows Terminal, PowerShell, or cmd launched from the Start menu.",
            code=2,
        ) from exc


def _build_client(callback_url: str, call_on_auth) -> "schwabdev.Client":
    try:
        return schwabdev.Client(
            app_key=os.environ["SCHWAB_API_KEY"],
            app_secret=os.environ["SCHWAB_APP_SECRET"],
            callback_url=callback_url,
            tokens_db=os.environ.get("SCHWAB_TOKEN_PATH", "tokens.db"),
            open_browser_for_auth=call_on_auth is None,
            call_on_auth=call_on_auth,
        )
    except KeyError as exc:
        raise _AuthAborted(f"{exc} is not set in .env", code=2) from exc


if __name__ == "__main__":
    sys.exit(main())
