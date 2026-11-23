"""The login engine (mop_reco/auth.py): passwords, lock-out, sessions, the last-administrator guard and the audit trail.

Each test uses a throw-away SQLite database - never db/reco.sqlite.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import auth
from mop_reco.db import AppSession, AppUser, AuthEvent, get_engine, get_session, init_db

PW = "Correct-horse-9"
CFG = auth.settings({"auth": {"max_failed_attempts": 3, "lockout_minutes": 10, "idle_minutes": 30, "absolute_hours": 8}})


@pytest.fixture
def session(tmp_path):
    s = get_session(init_db(get_engine(tmp_path / "t.db")))
    yield s
    s.close()


def _admin(session, name="boss"):
    return auth.create_user(session, name, f"{name.title()} Admin", "admin", PW, "test", must_change=False, cfg=CFG)


def _events(session):
    return [(e.event, e.username) for e in session.query(AuthEvent).order_by(AuthEvent.id)]


# --- passwords ---------------------------------------------------------------------------------------------------

def test_password_is_hashed_and_verified():
    h = auth.hash_password(PW)
    assert PW not in h and h.startswith("scrypt$")
    assert auth.verify_password(PW, h)
    assert not auth.verify_password(PW + "x", h)
    assert not auth.verify_password(PW, "garbage")
    assert auth.hash_password(PW) != h                      # a fresh salt every time


def test_password_policy():
    assert auth.password_problems("Abcdefgh12", "bob") == []
    assert auth.password_problems("short1", "bob")
    assert auth.password_problems("onlyletterslong", "bob")
    assert auth.password_problems("1234567890123", "bob")
    assert auth.password_problems("Xbob-secret12", "bob")   # contains the username


# --- users -------------------------------------------------------------------------------------------------------

def test_create_user_rules(session):
    _admin(session)
    with pytest.raises(auth.AuthError, match="username"):
        auth.create_user(session, "A B", "x", "user", PW, "t", cfg=CFG)
    with pytest.raises(auth.AuthError, match="already taken"):
        auth.create_user(session, "BOSS", "Another Name", "user", PW, "t", cfg=CFG)
    with pytest.raises(auth.AuthError, match="already used"):
        auth.create_user(session, "boss2", "boss admin", "user", PW, "t", cfg=CFG)      # same display name, other case
    with pytest.raises(auth.AuthError, match="role"):
        auth.create_user(session, "boss3", "B Three", "owner", PW, "t", cfg=CFG)
    with pytest.raises(auth.AuthError, match="password"):
        auth.create_user(session, "boss4", "B Four", "user", "short", "t", cfg=CFG)
    assert [u.username for u in auth.list_users(session)] == ["boss"]


def test_stored_row_holds_no_password(session):
    u = _admin(session)
    assert PW not in u.password_hash
    assert PW not in str([vars(e) for e in session.query(AuthEvent)])


# --- signing in and lock-out -------------------------------------------------------------------------------------

def test_good_login_and_case_insensitive_username(session):
    _admin(session)
    r = auth.authenticate(session, "  BOSS ", PW, CFG)
    assert r.user and r.user.username == "boss" and r.user.last_login_at is not None


def test_wrong_username_and_wrong_password_look_identical(session):
    _admin(session)
    a = auth.authenticate(session, "nobody", PW, CFG)
    b = auth.authenticate(session, "boss", "wrong-Pass-1", CFG)
    assert a.user is None and b.user is None and a.message == b.message == auth.GENERIC_FAILURE


def test_lockout_after_repeated_failures_then_expires(session):
    _admin(session)
    t0 = datetime(2026, 11, 23, 10, 0)
    for _ in range(3):
        auth.authenticate(session, "boss", "wrong-Pass-1", CFG, now=t0)
    r = auth.authenticate(session, "boss", PW, CFG, now=t0 + timedelta(minutes=1))     # the RIGHT password, still locked
    assert r.user is None and "locked" in r.message
    r = auth.authenticate(session, "boss", PW, CFG, now=t0 + timedelta(minutes=11))
    assert r.user is not None


def test_success_resets_the_failure_count(session):
    _admin(session)
    for _ in range(2):
        auth.authenticate(session, "boss", "wrong-Pass-1", CFG)
    assert auth.authenticate(session, "boss", PW, CFG).user
    for _ in range(2):
        auth.authenticate(session, "boss", "wrong-Pass-1", CFG)
    assert auth.authenticate(session, "boss", PW, CFG).user       # 2 + 2 failures never reached 3 in a row


def test_admin_can_unlock(session):
    a = _admin(session)
    for _ in range(3):
        auth.authenticate(session, "boss", "wrong-Pass-1", CFG)
    assert auth.authenticate(session, "boss", PW, CFG).user is None
    auth.unlock(session, a.id, "someone")
    assert auth.authenticate(session, "boss", PW, CFG).user


def test_deactivated_user_cannot_sign_in(session):
    _admin(session)
    u = auth.create_user(session, "ann", "Ann Smith", "user", PW, "t", must_change=False, cfg=CFG)
    auth.set_active(session, u.id, False, "boss")
    r = auth.authenticate(session, "ann", PW, CFG)
    assert r.user is None and "deactivated" in r.message


# --- sessions ----------------------------------------------------------------------------------------------------

def test_session_round_trip_and_only_the_hash_is_stored(session):
    u = _admin(session)
    token = auth.create_session(session, u, CFG)
    assert auth.resolve_session(session, token, CFG).username == "boss"
    assert session.query(AppSession).one().token_hash != token
    assert token not in str([vars(r) for r in session.query(AppSession)])
    assert auth.resolve_session(session, token + "x", CFG) is None
    assert auth.resolve_session(session, "", CFG) is None


def test_session_idle_and_absolute_expiry(session):
    u = _admin(session)
    t0 = datetime(2026, 11, 23, 9, 0)
    token = auth.create_session(session, u, CFG, now=t0)
    assert auth.resolve_session(session, token, CFG, now=t0 + timedelta(minutes=20))
    assert auth.resolve_session(session, token, CFG, now=t0 + timedelta(minutes=45))        # 25 min after last use: fine
    assert auth.resolve_session(session, token, CFG, now=t0 + timedelta(minutes=45 + 31)) is None   # idle too long
    t1 = datetime(2026, 11, 24, 9, 0)
    token2 = auth.create_session(session, u, CFG, now=t1)
    for m in range(0, 8 * 60, 20):                                                           # kept busy all day ...
        assert auth.resolve_session(session, token2, CFG, now=t1 + timedelta(minutes=m))
    assert auth.resolve_session(session, token2, CFG, now=t1 + timedelta(hours=8, minutes=1)) is None   # ... still ends


def test_logout_ends_the_session(session):
    u = _admin(session)
    token = auth.create_session(session, u, CFG)
    auth.end_session(session, token, "boss")
    assert auth.resolve_session(session, token, CFG) is None
    assert ("logout", "boss") in _events(session)


def test_deactivating_ends_open_sessions(session):
    _admin(session)
    ann = auth.create_user(session, "ann", "Ann Smith", "user", PW, "t", must_change=False, cfg=CFG)
    token = auth.create_session(session, ann, CFG)
    auth.set_active(session, ann.id, False, "boss")
    assert auth.resolve_session(session, token, CFG) is None


# --- passwords: change and reset ---------------------------------------------------------------------------------

def test_change_password_rules_and_other_sessions_end(session):
    u = _admin(session)
    mine, other = auth.create_session(session, u, CFG), auth.create_session(session, u, CFG)
    with pytest.raises(auth.AuthError, match="current password"):
        auth.change_password(session, u, "wrong-Pass-1", "Brand-new-pw-7", CFG)
    with pytest.raises(auth.AuthError, match="different"):
        auth.change_password(session, u, PW, PW, CFG)
    with pytest.raises(auth.AuthError, match="password needs"):
        auth.change_password(session, u, PW, "weak", CFG)
    auth.change_password(session, u, PW, "Brand-new-pw-7", CFG, keep_token_hash=auth.token_hash_of(mine))
    assert auth.resolve_session(session, mine, CFG) is not None
    assert auth.resolve_session(session, other, CFG) is None
    assert auth.authenticate(session, "boss", "Brand-new-pw-7", CFG).user
    assert auth.authenticate(session, "boss", PW, CFG).user is None


def test_admin_reset_forces_a_change_and_unlocks_and_ends_sessions(session):
    boss = _admin(session)
    ann = auth.create_user(session, "ann", "Ann Smith", "user", PW, "t", must_change=False, cfg=CFG)
    token = auth.create_session(session, ann, CFG)
    for _ in range(3):
        auth.authenticate(session, "ann", "wrong-Pass-1", CFG)
    auth.admin_reset_password(session, ann.id, "Temp-pass-4321", "boss", CFG)
    assert auth.resolve_session(session, token, CFG) is None
    r = auth.authenticate(session, "ann", "Temp-pass-4321", CFG)
    assert r.user and r.user.must_change_password is True
    with pytest.raises(auth.AuthError):
        auth.admin_reset_password(session, ann.id, "weak", "boss", CFG)
    assert boss.must_change_password is False


# --- the last administrator --------------------------------------------------------------------------------------

def test_the_last_admin_cannot_be_deactivated_or_demoted(session):
    boss = _admin(session)
    with pytest.raises(auth.AuthError, match="last active administrator"):
        auth.set_active(session, boss.id, False, "boss")
    with pytest.raises(auth.AuthError, match="last active administrator"):
        auth.set_role(session, boss.id, "user", "boss")
    second = auth.create_user(session, "deputy", "Deputy Admin", "admin", PW, "t", cfg=CFG)
    auth.set_role(session, boss.id, "user", "deputy")            # allowed now: another admin exists
    with pytest.raises(auth.AuthError, match="last active administrator"):
        auth.set_active(session, second.id, False, "deputy")


# --- audit -------------------------------------------------------------------------------------------------------

def test_every_step_is_audited(session):
    u = _admin(session)
    auth.authenticate(session, "boss", "wrong-Pass-1", CFG, ip="10.0.0.5")
    auth.authenticate(session, "ghost", PW, CFG)
    auth.authenticate(session, "boss", PW, CFG)
    token = auth.create_session(session, u, CFG)
    auth.end_session(session, token, "boss")
    kinds = [e for e, _ in _events(session)]
    assert kinds == ["user_created", "login_failed", "login_failed", "login_ok", "logout"]
    assert session.query(AuthEvent).filter(AuthEvent.ip == "10.0.0.5").count() == 1


# --- the development switch --------------------------------------------------------------------------------------

def test_auth_off_works_in_development_but_is_refused_in_production(monkeypatch):
    monkeypatch.delenv("MOP_REQUIRE_POSTGRES", raising=False)
    monkeypatch.setenv("MOP_AUTH", "off")
    assert auth.auth_disabled() is True
    monkeypatch.setenv("MOP_REQUIRE_POSTGRES", "1")
    assert auth.auth_disabled() is False
    monkeypatch.delenv("MOP_AUTH")
    assert auth.auth_disabled() is False
