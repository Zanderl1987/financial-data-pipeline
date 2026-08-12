"""
Local-capture Schwab re-auth: browser redirects to https://127.0.0.1:8182,
this HTTPS listener grabs the code instantly and exchanges it -- no chat
round-trip to blow the ~30s code window.

The auth code dies ~30s after login. Copy-paste + chat latency routinely
kills it (three failures on 2026-08-11: 2x same code, 1x fresh code all
rejected invalid_grant). Binding the exact callback URL locally captures the
redirect the moment login completes, and the exchange happens in the same
process -- well inside the window.

The cert is self-signed (generated with the installed `cryptography`) and
PERSISTED under %LOCALAPPDATA%\\schwab_reauth\\ so a single trusted-root
import makes the browser accept the redirect silently (no TLS warning).
First-run trust setup (run once):
    certutil -addstore -f -user Root "%LOCALAPPDATA%\\schwab_reauth\\schwab.crt"

Usage (real terminal):
    C:\\ProgramData\\anaconda3\\python.exe scripts\\schwab_local_reauth.py

It prints the auth URL. Log in in a browser; the redirect auto-captures
(with no TLS warning once the cert is trusted).
"""

import http.server
import ipaddress
import os
import re
import ssl
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

import schwabdev  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from schwab_auth import check_refresh_token, token_path  # noqa: E402

load_dotenv()

PORT = 8182
CAPTURE_WAIT_SECONDS = 1800

captured: dict[str, str] = {}


def _make_self_signed_cert(cert_dir: str) -> tuple[str, str]:
    """Generate a persistent self-signed cert for 127.0.0.1 into cert_dir."""
    os.makedirs(cert_dir, exist_ok=True)
    cert_path = os.path.join(cert_dir, "schwab.crt")
    key_path = os.path.join(cert_dir, "schwab.key")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    alt = x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    now = time.time()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.fromtimestamp(now - 60, tz=timezone.utc))
        .not_valid_after(datetime.fromtimestamp(now + 7 * 86400, tz=timezone.utc))
        .add_extension(alt, critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    return cert_path, key_path


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        captured["path"] = self.path
        print(f"\n[listener] captured redirect: {self.path}", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><h3>Authorization captured. Close this tab.</h3></body></html>")

    def do_HEAD(self) -> None:
        self.do_GET()

    def log_message(self, *args: object) -> None:
        pass

    def handle(self) -> None:
        """Tolerate TLS handshake failures so a bad handshake doesn't kill the accept loop."""
        try:
            super().handle()
        except (ssl.SSLError, ConnectionError, OSError) as exc:
            print(f"\n[listener] handshake failed from a client: {exc}", flush=True)


def main() -> int:
    path = os.path.abspath(token_path())
    before = check_refresh_token(path)
    print(f"Repo:        {REPO_ROOT}")
    print(f"Token store: {path}")
    print(f"Before:      {before['state'].upper()} -- {before['detail']}")

    try:
        app_key = os.environ["SCHWAB_API_KEY"]
        app_secret = os.environ["SCHWAB_APP_SECRET"]
    except KeyError as exc:
        print(f"FAILED: {exc} is not set in .env", file=sys.stderr)
        return 2

    cert_dir = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "schwab_reauth")
    cert_path, key_path = _make_self_signed_cert(cert_dir)

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Listening on https://127.0.0.1:{PORT} -- open the auth URL and log in.")
    print("The redirect auto-captures; on the TLS warning click 'Advanced' -> 'Proceed'.")
    print(f"Cert: {cert_path} (persisted; if the browser warns once, run:")
    print(f"  certutil -addstore -f -user Root {cert_path}")
    print(")")

    auth_url = (
        f"https://api.schwabapi.com/v1/oauth/authorize?client_id={app_key}"
        f"&redirect_uri={os.environ.get('SCHWAB_CALLBACK_URL', 'https://127.0.0.1:8182')}"
    )
    print(f"Auth URL: {auth_url}")
    print("=" * 70)

    start = time.time()
    while "path" not in captured and time.time() - start < CAPTURE_WAIT_SECONDS:
        time.sleep(0.2)
    server.shutdown()

    raw = captured.get("path", "")
    if not raw:
        print(f"\nFAILED: no redirect captured within {CAPTURE_WAIT_SECONDS}s.", file=sys.stderr)
        return 1

    code_match = re.search(r"[?&]code=([^&]+)", raw)
    if not code_match:
        print(f"\nFAILED: no code in captured path: {raw}", file=sys.stderr)
        return 1

    code = code_match.group(1)
    callback_url = f"https://127.0.0.1:{PORT}/?code={code}&session=captured"
    print(f"\nCaptured code (len {len(code)}), exchanging...")

    try:
        client = schwabdev.Client(
            app_key=app_key,
            app_secret=app_secret,
            callback_url=os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"),
            tokens_db=os.environ.get("SCHWAB_TOKEN_PATH", "tokens.db"),
            call_on_auth=lambda _url: callback_url,
            open_browser_for_auth=False,
        )
    except Exception as exc:
        print(f"\nFAILED: the authorization flow raised: {exc}", file=sys.stderr)
        return 1

    after = check_refresh_token(path)

    if after["state"] not in ("ok", "expiring"):
        print(f"\nFAILED: token store is still {after['state'].upper()}.", file=sys.stderr)
        print(f"  {after['detail']}", file=sys.stderr)
        return 1

    if before["issued"] is not None and after["issued"] == before["issued"]:
        print("\nFAILED: the refresh token did not change.", file=sys.stderr)
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
        print(f"\nWARNING: new token saved but live quote failed ({live_detail}).", file=sys.stderr)
        return 1

    print("Live check:   OK (quote request succeeded)")
    print("\nSUCCESS -- the Schwab pipelines can run now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
