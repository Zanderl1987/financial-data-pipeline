"""
Tests for the schwab_reauth callback listener.

The listener replaces a 30-second human copy/paste race with a local HTTPS
server that catches Schwab's redirect directly. These tests drive it with real
TLS requests against a real socket -- a mocked handler would not prove the
self-signed certificate, the TLS wrap, or the port binding work, which is
where this can actually break.
"""

import importlib.util
import os
import socket
import ssl
import threading
import urllib.error
import urllib.request

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "schwab_reauth.py")

pytestmark = pytest.mark.skipif(
    not os.path.exists(SCRIPT), reason="schwab_reauth.py not present"
)


def _load_module():
    """Import the script by path -- scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("schwab_reauth", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reauth():
    return _load_module()


@pytest.fixture(autouse=True)
def _sandboxed_cert_dir(reauth, tmp_path, monkeypatch):
    """
    Never let a test touch the real %LOCALAPPDATA%\\schwab_reauth\\.

    That directory holds the certificate the user has imported into
    CurrentUser\\Root. A test run that regenerated it would silently void that
    trust and put the TLS interstitial back in front of the next real re-auth,
    inside the ~30s code window -- a test breaking production, quietly.
    """
    monkeypatch.setattr(reauth, "CERT_DIR", str(tmp_path / "cert_dir"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url: str, timeout: float = 10.0):
    """GET ignoring the self-signed cert, the way the browser will after the warning."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.urlopen(url, timeout=timeout, context=ctx)


def test_listener_captures_the_authorization_code(reauth):
    port = _free_port()
    callback = f"https://127.0.0.1:{port}"

    with reauth._CallbackCatcher(callback) as catcher:
        result = {}

        def hit():
            try:
                resp = _get(f"{callback}/?code=TESTCODE%40&session=abc")
                result["status"] = resp.status
                result["body"] = resp.read()
            except Exception as exc:  # surfaced by the assertions below
                result["error"] = exc

        t = threading.Thread(target=hit)
        t.start()
        captured = catcher.wait(15)
        t.join(15)

    assert "error" not in result, result.get("error")
    assert result["status"] == 200
    assert b"close this tab" in result["body"]
    assert captured is not None
    assert captured.startswith(callback)
    assert "code=TESTCODE" in captured
    assert "session=abc" in captured


def test_captured_url_parses_back_to_the_code(reauth):
    """
    schwabdev parses the string we hand it with urlparse + parse_qs, and
    parse_qs url-decodes -- so the %40 Schwab appends must survive as '@'.
    """
    import urllib.parse

    port = _free_port()
    callback = f"https://127.0.0.1:{port}"

    with reauth._CallbackCatcher(callback) as catcher:
        threading.Thread(target=lambda: _get(f"{callback}/?code=C0.abc%40&session=s")).start()
        captured = catcher.wait(15)

    code = urllib.parse.parse_qs(urllib.parse.urlparse(captured).query)["code"][0]
    assert code == "C0.abc@"


def test_favicon_request_does_not_end_the_wait(reauth):
    """A browser fetching /favicon.ico must not be mistaken for the redirect."""
    port = _free_port()
    callback = f"https://127.0.0.1:{port}"

    with reauth._CallbackCatcher(callback) as catcher:
        try:
            _get(f"{callback}/favicon.ico")
        except urllib.error.HTTPError:
            pass  # 204 is fine either way
        assert catcher.wait(1.5) is None
        assert catcher.captured is None


def test_server_releases_the_port_on_exit(reauth):
    """The listener must not linger and block the next run."""
    port = _free_port()
    callback = f"https://127.0.0.1:{port}"

    with reauth._CallbackCatcher(callback):
        assert not reauth._port_is_free("127.0.0.1", port)

    # Re-binding proves the socket was really closed.
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def test_ephemeral_certificate_is_deleted(reauth):
    port = _free_port()
    with reauth._CallbackCatcher(f"https://127.0.0.1:{port}", ephemeral=True) as catcher:
        tmpdir = catcher._tmpdir
        assert os.path.exists(os.path.join(tmpdir, reauth.CERT_NAME))
        assert os.path.exists(os.path.join(tmpdir, reauth.KEY_NAME))
    assert not os.path.exists(tmpdir), "private key left on disk after the flow"


def test_persistent_certificate_is_reused(reauth, tmp_path, monkeypatch):
    """
    The trust import (`certutil -addstore`) is per-certificate. If a run
    silently minted a new one, the browser would warn again and the ~30s code
    window would be spent clicking through it -- which is the failure this
    whole listener exists to avoid.
    """
    monkeypatch.setattr(reauth, "CERT_DIR", str(tmp_path / "certs"))

    first, key1, generated1 = reauth._self_signed_cert(reauth.CERT_DIR, "127.0.0.1")
    assert generated1, "first call must create the cert"
    body = open(first, "rb").read()

    second, key2, generated2 = reauth._self_signed_cert(reauth.CERT_DIR, "127.0.0.1")
    assert (second, key2) == (first, key1)
    assert not generated2, "reported a new cert when it reused the old one"
    assert open(second, "rb").read() == body, "cert bytes changed -- trust would be void"


def test_expired_certificate_is_regenerated(reauth, tmp_path, monkeypatch):
    """
    The previous implementation returned any existing file unconditionally, so
    a cert that aged out came back expired and the browser warned again with
    no explanation. Expiry must force a regeneration AND be reported, since
    the user has to re-run the trust import.
    """
    monkeypatch.setattr(reauth, "CERT_DIR", str(tmp_path / "certs"))
    monkeypatch.setattr(reauth, "CERT_DAYS", 0)  # expires immediately

    stale, _, _ = reauth._self_signed_cert(reauth.CERT_DIR, "127.0.0.1")
    stale_body = open(stale, "rb").read()
    assert not reauth._cert_is_usable(stale, os.path.join(reauth.CERT_DIR, reauth.KEY_NAME))

    monkeypatch.setattr(reauth, "CERT_DAYS", 825)
    fresh, _, generated = reauth._self_signed_cert(reauth.CERT_DIR, "127.0.0.1")
    assert generated, "expired cert was reused"
    assert open(fresh, "rb").read() != stale_body


def test_corrupt_certificate_is_replaced_not_fatal(reauth, tmp_path, monkeypatch):
    """A truncated cert must regenerate, not raise at TLS bind time."""
    monkeypatch.setattr(reauth, "CERT_DIR", str(tmp_path / "certs"))
    cert, key, _ = reauth._self_signed_cert(reauth.CERT_DIR, "127.0.0.1")
    with open(cert, "wb") as fh:
        fh.write(b"not a certificate")

    assert not reauth._cert_is_usable(cert, key)
    _, _, generated = reauth._self_signed_cert(reauth.CERT_DIR, "127.0.0.1")
    assert generated


def test_persistent_cert_actually_serves_tls(reauth, tmp_path, monkeypatch):
    """The reused cert must still work as a server cert, not just exist."""
    monkeypatch.setattr(reauth, "CERT_DIR", str(tmp_path / "certs"))
    reauth._self_signed_cert(reauth.CERT_DIR, "127.0.0.1")  # pre-create, force reuse

    port = _free_port()
    callback = f"https://127.0.0.1:{port}"
    with reauth._CallbackCatcher(callback) as catcher:
        assert not catcher.cert_generated, "should have reused the pre-created cert"
        threading.Thread(target=lambda: _get(f"{callback}/?code=REUSED")).start()
        assert catcher.wait(15) is not None


def test_port_is_free_detects_a_listener(reauth):
    port = _free_port()
    assert reauth._port_is_free("127.0.0.1", port)
    with reauth._CallbackCatcher(f"https://127.0.0.1:{port}", ephemeral=True):
        assert not reauth._port_is_free("127.0.0.1", port)


class _Args:
    def __init__(self, paste=False, timeout=1.0, callback_url=None, ephemeral_cert=True):
        self.paste = paste
        self.timeout = timeout
        self.callback_url = callback_url
        self.ephemeral_cert = ephemeral_cert


def test_supplied_callback_url_skips_the_listener(reauth, monkeypatch):
    """
    --callback-url is the agent/non-tty path: it must hand the url straight to
    schwabdev without binding a port or prompting. Binding would fail here
    anyway -- the point is that it never tries.
    """
    seen = {}
    monkeypatch.setattr(
        reauth.schwabdev, "Client",
        lambda **kw: seen.setdefault("url", kw["call_on_auth"]("ignored-auth-url")),
    )
    port = _free_port()
    url = "https://127.0.0.1:8182/?code=SUPPLIED%40&session=x"

    reauth._authorize(f"https://127.0.0.1:{port}", {}, _Args(callback_url=f"  {url}  "))

    assert seen["url"] == url, "url must be passed through stripped and unmodified"
    assert reauth._port_is_free("127.0.0.1", port), "listener should never have bound"


def test_timeout_aborts_with_a_usable_message(reauth, monkeypatch):
    """
    No redirect arriving must abort with guidance, not hang. Drives the real
    _authorize wiring with schwabdev stubbed, so the catcher/abort handoff is
    covered without touching a browser or Schwab.
    """
    port = _free_port()
    monkeypatch.setattr(reauth.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(
        reauth.schwabdev, "Client",
        lambda **kw: kw["call_on_auth"]("https://api.schwabapi.com/v1/oauth/authorize"),
    )

    with pytest.raises(reauth._AuthAborted) as exc:
        reauth._authorize(f"https://127.0.0.1:{port}", {}, _Args(timeout=1.0))

    assert "no redirect arrived" in str(exc.value)
    assert "--paste" in str(exc.value)  # the message names the fallback


def test_occupied_port_falls_back_to_paste(reauth, monkeypatch, capsys):
    """A port already in use must degrade to the manual flow, not crash."""
    port = _free_port()
    monkeypatch.setattr(reauth, "_build_client", lambda cb, coa: "client")

    with reauth._CallbackCatcher(f"https://127.0.0.1:{port}"):
        result = reauth._authorize(f"https://127.0.0.1:{port}", {}, _Args())

    assert result == "client"
    assert "falling back to manual paste" in capsys.readouterr().out
