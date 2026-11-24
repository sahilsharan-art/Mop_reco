"""Audit S-1: response time must not reveal whether a username exists.

An unknown username was checked with `verify_password(pw, hash_password("x"))` - TWO scrypt runs - while a real account cost one, so a missing
username took about twice as long (measured 337 ms against 171 ms). Both paths now run exactly one."""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import auth  # noqa: E402
from mop_reco.db import get_engine, get_session, init_db  # noqa: E402


@pytest.fixture
def session(tmp_path):
    s = get_session(init_db(get_engine(tmp_path / "auth.sqlite")))
    auth.create_user(s, "real.user", "Real User", "user", "Correct-horse-9", "test", must_change=False, cfg=auth.DEFAULTS)
    return s


def _scrypt_calls(monkeypatch, fn):
    calls = []
    real = hashlib.scrypt
    monkeypatch.setattr(auth.hashlib, "scrypt", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    fn()
    return len(calls)


def test_unknown_and_wrong_password_cost_the_same_number_of_scrypt_runs(session, monkeypatch):
    auth._dummy_hash()                                                   # the one-off creation is not part of a request
    unknown = _scrypt_calls(monkeypatch, lambda: auth.authenticate(session, "nobody.here", "wrong-password-1", auth.DEFAULTS))
    wrong = _scrypt_calls(monkeypatch, lambda: auth.authenticate(session, "real.user", "wrong-password-1", auth.DEFAULTS))
    assert unknown == wrong == 1


def test_both_failures_give_the_same_message(session):
    a = auth.authenticate(session, "nobody.here", "x1y2z3w4v5", auth.DEFAULTS)
    b = auth.authenticate(session, "real.user", "x1y2z3w4v5", auth.DEFAULTS)
    assert a.user is None and b.user is None and a.message == b.message == auth.GENERIC_FAILURE


def test_the_dummy_hash_is_valid_and_not_the_hash_of_any_guessable_value():
    h = auth._dummy_hash()
    assert h.startswith("scrypt$") and h == auth._dummy_hash(), "made once"
    assert not auth.verify_password("x", h) and not auth.verify_password("", h)
