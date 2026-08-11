"""
Tests for schwab_auth -- the preflight that keeps an expired Schwab refresh
token from hanging a scheduled run on schwabdev's interactive input() prompt.
"""

import datetime
import sqlite3

import pytest

from schwab_auth import (
    EXPIRY_WARNING_WINDOW,
    REFRESH_TOKEN_LIFETIME,
    check_refresh_token,
    preflight,
)

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


def test_boundary_is_exactly_seven_days(tmp_path):
    """A token one second past 7 days is expired; one second short is not."""
    now = datetime.datetime(2026, 8, 10, tzinfo=UTC)
    one_sec = datetime.timedelta(seconds=1)
    just_expired = _token_db(tmp_path, now - REFRESH_TOKEN_LIFETIME - one_sec, "a.db")
    just_alive = _token_db(tmp_path, now - REFRESH_TOKEN_LIFETIME + one_sec, "b.db")
    assert check_refresh_token(just_expired, now=now)["state"] == "expired"
    assert check_refresh_token(just_alive, now=now)["state"] != "expired"


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
    assert "schwabdev.Client" in err  # the fix command is in the message


def test_message_is_ascii_only(tmp_path, capsys):
    """Windows cp1252 terminals crash on non-ASCII output."""
    now = datetime.datetime(2026, 8, 10, tzinfo=UTC)
    path = _token_db(tmp_path, now - REFRESH_TOKEN_LIFETIME - datetime.timedelta(days=1))
    with pytest.raises(SystemExit):
        preflight(path)
    out = capsys.readouterr()
    (out.err + out.out).encode("ascii")
