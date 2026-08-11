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


def test_temp_certificate_is_deleted(reauth):
    port = _free_port()
    with reauth._CallbackCatcher(f"https://127.0.0.1:{port}") as catcher:
        tmpdir = catcher._tmpdir
        assert os.path.exists(os.path.join(tmpdir, "cert.pem"))
        assert os.path.exists(os.path.join(tmpdir, "key.pem"))
    assert not os.path.exists(tmpdir), "private key left on disk after the flow"


def test_port_is_free_detects_a_listener(reauth):
    port = _free_port()
    assert reauth._port_is_free("127.0.0.1", port)
    with reauth._CallbackCatcher(f"https://127.0.0.1:{port}"):
        assert not reauth._port_is_free("127.0.0.1", port)


class _Args:
    def __init__(self, paste=False, timeout=1.0):
        self.paste = paste
        self.timeout = timeout


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
