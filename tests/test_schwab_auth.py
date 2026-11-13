"""
Tests for schwab_auth -- the preflight that keeps an expired Schwab refresh
token from hanging a scheduled run on schwabdev's interactive input() prompt.
"""

import datetime
import pathlib
import re
import sqlite3

import pytest

from schwab_auth import (
    EXPIRY_WARNING_WINDOW,
    REFRESH_TOKEN_LIFETIME,
    SCHWABDEV_REAUTH_MARGIN,
    check_refresh_token,
    preflight,
    refuse_interactive_auth,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

UTC = datetime.timezone.utc


def _token_db(tmp_path, issued, name="tokens.db"):
    """A token store shaped like schwabdev's, holding only the issue time."""
    path = tmp_path / name
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE schwabdev (access_token_issued TEXT, refresh_token_issued TEXT, "
        "access_token TEXT, refresh_token TEXT, id_token TEXT, expires_in INTEGER, "
        "token_type TEXT, scope TEXT)"
    )
    con.execute(
        "INSERT INTO schwabdev VALUES (?, ?, 'a', 'r', 'i', 1800, 'Bearer', 'api')",
        (issued.isoformat(), issued.isoformat()),
    )
    con.commit()
    con.close()
    return str(path)


def test_fresh_token_is_ok(tmp_path):
    now = datetime.datetime(2026, 8, 10, tzinfo=UTC)
    path = _token_db(tmp_path, now - datetime.timedelta(days=1))
    assert check_refresh_token(path, now=now)["state"] == "ok"


def test_expired_token_is_detected(tmp_path):
    now = datetime.datetime(2026, 8, 10, tzinfo=UTC)
    path = _token_db(tmp_path, now - REFRESH_TOKEN_LIFETIME - datetime.timedelta(hours=1))
    status = check_refresh_token(path, now=now)
    assert status["state"] == "expired"
    assert status["expires"] == status["issued"] + REFRESH_TOKEN_LIFETIME


def test_token_inside_warning_window_still_runs(tmp_path):
    """Expiring is not expired -- the run must proceed, only warn."""
    now = datetime.datetime(2026, 8, 10, tzinfo=UTC)
    issued = now - REFRESH_TOKEN_LIFETIME + (EXPIRY_WARNING_WINDOW / 2)
    path = _token_db(tmp_path, issued)
    assert check_refresh_token(path, now=now)["state"] == "expiring"
    assert preflight(path, now=now)["state"] == "expiring"


def test_boundary_is_schwabdevs_reauth_margin(tmp_path):
    """
    The line is not 7 days but 7 days minus schwabdev's 3630s refresh margin:
    inside that last hour schwabdev demands a login, so a preflight that let the
    run through would hand it straight to a prompt.
    """
    now = datetime.datetime(2026, 8, 10, tzinfo=UTC)
    one_sec = datetime.timedelta(seconds=1)
    line = now - REFRESH_TOKEN_LIFETIME + SCHWABDEV_REAUTH_MARGIN
    blocked = _token_db(tmp_path, line - one_sec, "a.db")
    alive = _token_db(tmp_path, line + one_sec, "b.db")
    assert check_refresh_token(blocked, now=now)["state"] == "expired"
    assert check_refresh_token(alive, now=now)["state"] == "expiring"


def test_schwabdev_margin_matches_the_installed_library():
    """If schwabdev changes its threshold, the preflight must move with it."""
    tokens = pytest.importorskip("schwabdev.tokens")
    src = pathlib.Path(tokens.__file__).read_text(encoding="utf-8")
    m = re.search(r"refresh_threshold = datetime\.timedelta\(seconds=(\d+)\)", src)
    assert m, "schwabdev no longer defines refresh_threshold -- re-check SCHWABDEV_REAUTH_MARGIN"
    assert int(m.group(1)) == SCHWABDEV_REAUTH_MARGIN.total_seconds()


def test_missing_store_is_missing_not_a_crash(tmp_path):
    status = check_refresh_token(str(tmp_path / "nope.db"))
    assert status["state"] == "missing"


def test_unreadable_store_is_reported_not_raised(tmp_path):
    """A corrupt/foreign db must degrade to a status, never propagate."""
    path = tmp_path / "junk.db"
    path.write_bytes(b"this is not a sqlite database")
    assert check_refresh_token(str(path))["state"] == "unreadable"


def test_store_without_the_expected_row(tmp_path):
    path = tmp_path / "empty.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schwabdev (refresh_token_issued TEXT)")
    con.commit()
    con.close()
    assert check_refresh_token(str(path))["state"] == "missing"


def test_status_never_exposes_the_token(tmp_path):
    """The status dict is printed to logs -- it must not carry secrets."""
    now = datetime.datetime(2026, 8, 10, tzinfo=UTC)
    path = _token_db(tmp_path, now - datetime.timedelta(days=1))
    blob = repr(check_refresh_token(path, now=now))
    assert "'r'" not in blob and "'a'" not in blob


@pytest.mark.parametrize("state_setup", ["expired", "missing"])
def test_preflight_exits_2_without_prompting(tmp_path, state_setup, capsys):
    """
    The whole point: exit immediately with a nonzero code instead of blocking
    on schwabdev's input() until run_all.py's timeout kills the pipeline.
    """
    now = datetime.datetime(2026, 8, 10, tzinfo=UTC)
    if state_setup == "expired":
        path = _token_db(tmp_path, now - REFRESH_TOKEN_LIFETIME - datetime.timedelta(days=2))
    else:
        path = str(tmp_path / "absent.db")

    with pytest.raises(SystemExit) as exc:
        preflight(path)

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "SCHWAB AUTH REQUIRED" in err
    # The banner must name the SELF-VERIFYING script. It used to name a bare
    # schwabdev.Client(...) one-liner, which returns normally whether or not a
    # token was actually stored -- so a lost 30s code window looked exactly like
    # success. scripts/daily_accumulators.ps1 also scrapes this line into its
    # flag file, so the name here is what a human ends up reading.
    assert "scripts\\schwab_reauth.py" in err
    assert "schwabdev.Client" not in err, (
        "the banner must not send anyone back to the silent one-liner"
    )


def test_message_is_ascii_only(tmp_path, capsys):
    """Windows cp1252 terminals crash on non-ASCII output."""
    now = datetime.datetime(2026, 8, 10, tzinfo=UTC)
    path = _token_db(tmp_path, now - REFRESH_TOKEN_LIFETIME - datetime.timedelta(days=1))
    with pytest.raises(SystemExit):
        preflight(path)
    out = capsys.readouterr()
    (out.err + out.out).encode("ascii")


def test_refuse_interactive_auth_exits_2_with_banner(capsys):
    with pytest.raises(SystemExit) as exc:
        refuse_interactive_auth("https://api.schwabapi.com/v1/oauth/authorize?client_id=x")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "SCHWAB AUTH REQUIRED" in err
    assert "scripts\\schwab_reauth.py" in err
    err.encode("ascii")


def test_real_schwabdev_client_exits_instead_of_prompting(tmp_path, monkeypatch):
    """
    End to end against the installed schwabdev: an expired token plus the hook
    must exit 2 without opening a browser or reading stdin. Before 2026-10-17,
    price_history/sector_etf did both, once per pipeline, every scheduled run.
    """
    schwabdev = pytest.importorskip("schwabdev")
    import builtins
    import webbrowser

    def _fail(*a, **k):
        raise AssertionError("a pipeline client tried to start an interactive login")

    monkeypatch.setattr(builtins, "input", _fail)
    monkeypatch.setattr(webbrowser, "open", _fail)
    now = datetime.datetime.now(UTC)
    path = _token_db(tmp_path, now - REFRESH_TOKEN_LIFETIME - datetime.timedelta(days=1))

    with pytest.raises(SystemExit) as exc:
        schwabdev.Client(
            app_key="k" * 16, app_secret="s" * 32,
            callback_url="https://127.0.0.1:8182", tokens_db=path,
            call_on_auth=refuse_interactive_auth,
        )
    assert exc.value.code == 2


def _client_modules():
    """Every tracked-style module that builds a schwabdev.Client, minus re-auth itself."""
    for path in sorted(REPO_ROOT.glob("*.py")):
        if "schwabdev.Client(" in path.read_text(encoding="utf-8"):
            yield path


def test_every_pipeline_client_refuses_interactive_auth():
    """
    One login, in scripts/schwab_reauth.py, has to cover every pipeline. Any
    module that builds a Client without both guards brings back a login popup
    per pipeline per run (and a 10-minute hang holding tokens.db locked).
    """
    modules = list(_client_modules())
    assert len(modules) >= 9, [m.name for m in modules]
    for path in modules:
        if path.name == "schwab_auth.py":
            continue
        src = path.read_text(encoding="utf-8")
        n_clients = src.count("schwabdev.Client(")
        assert src.count("call_on_auth=refuse_interactive_auth") == n_clients, (
            f"{path.name}: every schwabdev.Client(...) needs call_on_auth=refuse_interactive_auth")
        assert re.search(r"^\s+preflight\(\)", src, re.M), f"{path.name}: call preflight() before the Client"
